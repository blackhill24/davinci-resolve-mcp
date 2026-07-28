#!/usr/bin/env python3
"""Sample the vitals of a running DaVinci Resolve process (issue #153).

Resolve Studio 21.0.2.4 on Linux terminates by itself during and *after* a live
sweep — no crash dialog, no core dump, no `oom-kill` or `segfault` in the
journal, and it does not reproduce standalone. A failure that only appears
~20 harnesses into a sweep is a resource story (a slow leak, an exhausted fd
table, a wedged GPU context), and none of that is visible after the fact: once
the process is gone, so is everything that would have named the cause.

So this module does the only thing that helps — it records the resource curve
*while Resolve is alive*, at two granularities:

* between harnesses, driven by ``scripts/run_live_suite.py --vitals``, so a
  sweep that dies at harness 20 leaves 19 samples describing the approach; and
* on an idle Resolve (``--watch``), because occurrence 3 in #153 happened with
  no harness running at all — which is what rules out "some harness kills it"
  as the whole story and makes the idle curve worth having.

Everything is read from ``/proc`` and ``nvidia-smi``; nothing here talks to the
Resolve scripting API. That is deliberate: ``scriptapp()`` resets the process
locale (#121) and a Resolve that is mid-death takes its scripting caller with
it, so the sampler must survive exactly the moment it exists to describe.

Finding the process is its own trap. Resolve renames its main thread, so its
``comm`` is ``GUI Thread`` and ``pgrep -x resolve`` matches NOTHING while
Resolve is up (#111 finding 7). A loose ``pgrep -f resolve`` is the opposite
error — it matches ``systemd-resolved`` on essentially every Linux desktop. The
match here is anchored to a path boundary, the same rule
``src/core/app_control.py`` uses, but resolved to a PID rather than a boolean.

Usage:
    python3 scripts/resolve_vitals.py                    # one sample, human-readable
    python3 scripts/resolve_vitals.py --json             # one sample, JSON
    python3 scripts/resolve_vitals.py --env              # + the process environment
    python3 scripts/resolve_vitals.py --watch --interval 30 --out vitals.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

# `resolve` must start the string or follow a `/`, and end the string or be
# followed by whitespace: `/opt/resolve/bin/resolve` matches, `systemd-resolved`
# does not (its "resolve" follows a `-` and is trailed by a `d`). Same rule as
# `_LINUX_RESOLVE_CMDLINE_RE` in src/core/app_control.py, in Python syntax.
RESOLVE_CMDLINE_RE = re.compile(r"(^|/)resolve(\s|$)")

# The env vars worth pinning to the RUNNING process rather than assuming from
# the launcher. ALSA_CONFIG_PATH is the one #153 asks about: the launch shim
# (#93/#94) sets a raw-hw ALSA config, and a Fairlight wedge is a known failure
# mode on this box — but only if the variable actually reached the process.
ENV_KEYS_OF_INTEREST = (
    "ALSA_CONFIG_PATH",
    "RESOLVE_SCRIPT_API",
    "RESOLVE_SCRIPT_LIB",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)


# ── finding the process ───────────────────────────────────────────────────────

def _read_cmdline(pid: int, proc_root: str = "/proc") -> str:
    """The process command line with NULs turned into spaces, "" if unreadable."""
    try:
        raw = Path(proc_root, str(pid), "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def find_resolve_pids(proc_root: str = "/proc") -> list[int]:
    """Every PID whose command line looks like the Resolve binary, lowest first.

    Resolve is single-instance, so this is normally one PID; it returns a list
    because a still-dying old instance can briefly overlap a new one, and
    silently picking one of two would misattribute every later sample.
    """
    pids = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if RESOLVE_CMDLINE_RE.search(_read_cmdline(pid, proc_root)):
            pids.append(pid)
    return sorted(pids)


def _status_fields(pid: int, proc_root: str = "/proc") -> dict:
    """Parsed `/proc/<pid>/status` — keys verbatim, values stripped."""
    try:
        text = Path(proc_root, str(pid), "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if _:
            fields[key.strip()] = value.strip()
    return fields


def _kb(value: str | None) -> int | None:
    """`"12345 kB"` → `12345`; None for anything that is not that shape."""
    if not value:
        return None
    head = value.split()[0]
    return int(head) if head.isdigit() else None


def _descendant_pids(pid: int, proc_root: str = "/proc") -> list[int]:
    """Every descendant of `pid`, from the PPid links in `/proc/*/status`.

    Resolve's helpers (the FusionScript server, encoders, the panel) are
    children, and their fds and memory are part of the story — #153's log tail
    ends with `FusionScript Server [698607] Terminated`, so what that child was
    doing at the time is exactly what a sample wants to have recorded.
    """
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        parent = _status_fields(int(entry), proc_root).get("PPid")
        if parent and parent.isdigit():
            children.setdefault(int(parent), []).append(int(entry))
    found, queue = [], list(children.get(pid, []))
    while queue:
        current = queue.pop()
        if current in found or current == pid:
            continue
        found.append(current)
        queue.extend(children.get(current, []))
    return sorted(found)


def _fd_count(pid: int, proc_root: str = "/proc") -> int | None:
    """Open file descriptors, or None when the table cannot be read."""
    try:
        return len(os.listdir(Path(proc_root, str(pid), "fd")))
    except OSError:
        return None


def _fd_limit(pid: int, proc_root: str = "/proc") -> int | None:
    """The process's *soft* max-open-files limit, straight off the process.

    Read per-process rather than from the shell's `ulimit -n`: Resolve is
    launched from a desktop entry or the shim, which need not share this
    shell's limits, and the number only means anything next to the process's
    own fd count.
    """
    try:
        text = Path(proc_root, str(pid), "limits").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Max open files"):
            parts = line.split()
            # "Max open files  <soft>  <hard>  files"
            for token in parts[3:]:
                if token.isdigit():
                    return int(token)
    return None


def _cpu_seconds(pid: int, proc_root: str = "/proc") -> float | None:
    """utime+stime in seconds, for telling a busy Resolve from a wedged one."""
    try:
        raw = Path(proc_root, str(pid), "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # The comm field is parenthesised and may contain spaces ("(GUI Thread)"),
    # so split after the closing paren rather than on whitespace.
    _, _, rest = raw.partition(") ")
    parts = rest.split()
    if len(parts) < 13:
        return None
    try:
        ticks = int(parts[11]) + int(parts[12])  # utime, stime (fields 14,15)
    except ValueError:
        return None
    return round(ticks / (os.sysconf("SC_CLK_TCK") or 100), 2)


def read_environ(pid: int, proc_root: str = "/proc",
                 keys: tuple = ENV_KEYS_OF_INTEREST) -> dict:
    """The interesting slice of the RUNNING process's environment.

    A key present with an empty value and a key that was never set are
    different findings, so an unset key maps to None rather than being absent.
    """
    try:
        raw = Path(proc_root, str(pid), "environ").read_bytes()
    except OSError:
        return {}
    environ = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, sep, value = item.decode("utf-8", "replace").partition("=")
        if sep:
            environ[key] = value
    return {key: environ.get(key) for key in keys}


# ── GPU ───────────────────────────────────────────────────────────────────────

def gpu_memory_mib(pids: list) -> int | None:
    """Summed GPU memory of `pids`, or None when it cannot be determined.

    None is not zero: a box with no NVIDIA driver, an `nvidia-smi` that errors,
    and a Resolve holding no GPU memory are three different states, and
    flattening them would make a vanished GPU context read as normal.
    """
    if not pids:
        return None
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            check=False, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    wanted, total, matched = set(pids), 0, False
    for line in (proc.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        if int(parts[0]) in wanted and parts[1].isdigit():
            total += int(parts[1])
            matched = True
    return total if matched else 0


# ── the sample ────────────────────────────────────────────────────────────────

def sample(pid: int | None = None, proc_root: str = "/proc",
           with_gpu: bool = True, with_env: bool = False) -> dict:
    """One vitals reading. Never raises — an unreadable field comes back None.

    `alive: False` with `pid: None` is the interesting record, not an error:
    it is what a sample taken across the moment of the exit in #153 looks like,
    and the sweep's abort path needs to be able to write it down.
    """
    stamp = time.time()
    if pid is None:
        pids = find_resolve_pids(proc_root)
        pid = pids[0] if pids else None
    if pid is None:
        return {"at_epoch": stamp, "alive": False, "pid": None}

    fields = _status_fields(pid, proc_root)
    if not fields:
        return {"at_epoch": stamp, "alive": False, "pid": pid}

    descendants = _descendant_pids(pid, proc_root)
    child_fds = [_fd_count(child, proc_root) for child in descendants]
    child_rss = [_kb(_status_fields(child, proc_root).get("VmRSS")) for child in descendants]
    reading = {
        "at_epoch": stamp,
        "alive": True,
        "pid": pid,
        "rss_kb": _kb(fields.get("VmRSS")),
        "vm_kb": _kb(fields.get("VmSize")),
        "threads": int(fields["Threads"]) if fields.get("Threads", "").isdigit() else None,
        "fds": _fd_count(pid, proc_root),
        "fd_limit": _fd_limit(pid, proc_root),
        "cpu_seconds": _cpu_seconds(pid, proc_root),
        "children": len(descendants),
        # Summed separately from the parent's: a leak in the FusionScript server
        # would otherwise hide inside a flat main-process curve. A real 0 (no
        # children) is kept as 0 — only "no readable value at all" is None, so a
        # child that disappeared never reads as a child holding nothing.
        "child_fds": _sum_or_none(child_fds),
        "child_rss_kb": _sum_or_none(child_rss),
    }
    if with_gpu:
        reading["gpu_mib"] = gpu_memory_mib([pid] + descendants)
    if with_env:
        reading["environ"] = read_environ(pid, proc_root)
    return reading


def _sum_or_none(values: list) -> int | None:
    """Sum the readable entries; None only when there were none to read.

    `[]` (no children) sums to 0, which is a real reading; `[None, None]` (two
    children whose fd tables could not be read) is None, which is not.
    """
    readable = [v for v in values if v is not None]
    if not readable and values:
        return None
    return sum(readable)


# Fields whose growth across a sweep is the thing #153 is testing for.
GROWTH_FIELDS = ("rss_kb", "fds", "child_fds", "threads", "children", "gpu_mib")


def delta(first: dict, last: dict, fields: tuple = GROWTH_FIELDS) -> dict:
    """Field-by-field growth between two samples, skipping unreadable pairs."""
    out = {}
    for field in fields:
        before, after = first.get(field), last.get(field)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            out[field] = after - before
    return out


def format_sample(reading: dict) -> str:
    """One dense human-readable line — the form the sweep prints per harness."""
    if not reading.get("alive"):
        return f"resolve: GONE (pid={reading.get('pid')})"
    rss = reading.get("rss_kb")
    fds, limit = reading.get("fds"), reading.get("fd_limit")
    return (
        f"pid={reading['pid']} "
        f"rss={rss // 1024 if rss else '?'}M "
        f"fds={fds if fds is not None else '?'}"
        f"{f'/{limit}' if limit else ''} "
        f"threads={reading.get('threads') or '?'} "
        f"children={reading.get('children')} "
        f"childfds={reading.get('child_fds') or 0} "
        f"gpu={reading.get('gpu_mib') if reading.get('gpu_mib') is not None else '?'}M "
        f"cpu={reading.get('cpu_seconds')}s"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def watch(interval: float, duration: float | None, out: Path | None,
          with_gpu: bool = True) -> int:
    """Sample every `interval` seconds until Resolve exits or `duration` passes.

    Returns 0 if Resolve was still alive at the end, 1 if it vanished while
    being watched — the exit code is the point when this runs unattended after
    a sweep, which is occurrence 3 in #153.
    """
    started, first, last = time.time(), None, None
    handle = out.open("a", encoding="utf-8") if out else None
    try:
        while True:
            reading = sample(with_gpu=with_gpu)
            first = first or (reading if reading.get("alive") else None)
            if reading.get("alive"):
                last = reading
            line = json.dumps(reading)
            if handle:
                handle.write(line + "\n")
                handle.flush()  # the run this matters for ends by being killed
            elapsed = round(time.time() - started)
            print(f"[{elapsed:>6}s] {format_sample(reading)}", flush=True)
            if not reading.get("alive"):
                print("resolve vanished while being watched — "
                      "the samples above are the approach to it.")
                if first and last:
                    print(f"growth over the watch: {delta(first, last)}")
                return 1
            if duration is not None and time.time() - started >= duration:
                if first and last:
                    print(f"growth over the watch: {delta(first, last)}")
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        if first and last:
            print(f"\ngrowth over the watch: {delta(first, last)}")
        return 0
    finally:
        if handle:
            handle.close()


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pid", type=int, help="Sample this PID instead of finding Resolve.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a line.")
    parser.add_argument("--env", action="store_true",
                        help="Include the running process's environment slice.")
    parser.add_argument("--no-gpu", action="store_true", help="Skip the nvidia-smi query.")
    parser.add_argument("--watch", action="store_true",
                        help="Sample repeatedly until Resolve exits or --duration elapses.")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="Seconds between --watch samples (default 30).")
    parser.add_argument("--duration", type=float,
                        help="Stop watching after this many seconds (default: forever).")
    parser.add_argument("--out", type=Path, help="Append each --watch sample to this JSONL file.")
    args = parser.parse_args(argv)

    if args.watch:
        return watch(args.interval, args.duration, args.out, with_gpu=not args.no_gpu)

    reading = sample(pid=args.pid, with_gpu=not args.no_gpu, with_env=args.env)
    if args.json:
        print(json.dumps(reading, indent=2))
    else:
        print(format_sample(reading))
        for key, value in (reading.get("environ") or {}).items():
            print(f"  {key}={value if value is not None else '<unset>'}")
    # Not-running is a state a caller wants to branch on, not a crash.
    return 0 if reading.get("alive") else 1


if __name__ == "__main__":
    raise SystemExit(main())
