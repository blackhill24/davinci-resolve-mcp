#!/usr/bin/env python3
"""Run the `tests/**/live_*.py` suite against a running DaVinci Resolve.

Before this existed, "run the live suite" was hand-assembled per session, so the
recorded baseline could not be reproduced or compared and sweep-only failures
looked like code defects. Three traps cost a wasted run each and are encoded
here rather than re-derived (issue #151):

1. **stdin eats the work list.** A harness — or its ffmpeg child — that reads
   stdin swallows the remaining harnesses if the loop feeds them from a pipe.
   Every child gets `stdin=DEVNULL`.
2. **Harnesses inherit the previous one's project.** Most create a disposable
   project and delete it, dropping Resolve onto an auto-created `Untitled
   Project` with no current timeline; the next harness then fails assertions
   that pass in isolation. The runner re-establishes a named scratch project
   with a timeline *between* harnesses.
3. **A UI parked on the Project Manager makes Resolve silently inert.** Every
   page query returns None and `ImportMedia`/`AddSubFolder` return None with no
   error, so harnesses fail in ways that read as Resolve bugs. The runner
   refuses to start in that state.

It also diffs the project list around each harness, so a disposable project left
behind is reported against the harness that leaked it instead of accumulating
unattributed (14 of 25 projects were probe leftovers when #151 was filed).

`--vitals` adds the other half of that accounting. Resolve on this box
terminates by itself ~20 harnesses into a sweep, leaving no crash dialog, no
core dump and nothing in the journal (#153), so the only evidence available is
what its resource usage was doing on the way there. With the flag, a `/proc`
sample is taken between harnesses and recorded per harness in the results file,
the summary prints the growth curve and the harnesses that grew RSS the most,
and the abort path samples one last time — which is what distinguishes "the
process exited" from "the process is up but no longer answering".

Exit codes follow `tests/preflight.py`: **0** all green, **1** at least one
harness failed, **2** the environment was not ready to start. A harness exiting
2/3 is recorded as SKIP, never as a failure — that is the contract that keeps
"Resolve isn't ready" distinguishable from "the code is broken".

Usage:
    .venv/bin/python scripts/run_live_suite.py               # warm sweep
    .venv/bin/python scripts/run_live_suite.py --cold        # Resolve QUIT first
    .venv/bin/python scripts/run_live_suite.py -k timeline   # substring filter
    .venv/bin/python scripts/run_live_suite.py --clean-leaks # delete leftovers
    .venv/bin/python scripts/run_live_suite.py --vitals      # + resource curve (#153)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for _path in (str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import resolve_vitals  # noqa: E402 — needs SCRIPTS on the path first

SCRATCH_PROJECT = "ZZ_live_suite_scratch"
SCRATCH_TIMELINE = "live_suite_scratch_tl"
# Synthetic, regenerated on demand, and deliberately NOT under the system temp
# dir — it has to outlive the run so the scratch timeline still resolves its
# media on the next sweep.
SCRATCH_MEDIA = Path.home() / ".cache" / "davinci-resolve-mcp" / "live_suite_scratch.mp4"
DEFAULT_TIMEOUT = 900  # seconds per harness; renders are the slow tail

PASS, FAIL, SKIP, TIMEOUT, ABORTED = "PASS", "FAIL", "SKIP", "TIMEOUT", "ABORTED"
# Sentinel the Resolve-touching ops return when the bridge is gone, so the
# sweep can tell "Resolve died" apart from "this op did not work".
RESOLVE_GONE = "Resolve is not responding"


# ── environment ───────────────────────────────────────────────────────────────

def build_env() -> dict:
    """Child env for every harness: scripting paths plus the flags a swept run needs."""
    from src.core.platform import get_resolve_paths

    paths = get_resolve_paths()
    env = dict(os.environ)
    env["RESOLVE_SCRIPT_API"] = paths["api_path"]
    env["RESOLVE_SCRIPT_LIB"] = paths["lib_path"]
    pythonpath = [str(ROOT), paths["modules_path"]]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    # scriptapp() resets the C locale to ASCII mid-process (#121); UTF-8 mode
    # survives that, and without it harnesses with non-ASCII output die on print.
    env["PYTHONUTF8"] = "1"
    # A sweep is exactly the disposable-empty-project case this flag was added
    # for: live_resolve20_api refuses to run against "Untitled Project", and in a
    # sweep the previous harness's cleanup lands Resolve on one every time.
    env.setdefault("RESOLVE20_LIVE_ALLOW_UNTITLED", "1")
    # A Resolve that quits mid-sweep must fail fast, not silently relaunch into a
    # different state halfway through the run.
    env["DAVINCI_MCP_NO_AUTOLAUNCH"] = "1"
    return env


# ── Resolve-touching ops, always in a child process ───────────────────────────
#
# The parent never links fusionscript: scriptapp() resets the process locale
# (#121) and a Resolve crash takes its caller with it. Isolating it means the
# runner survives both and keeps reporting.

def resolve_op(op: str, env: dict, timeout: int = 180) -> dict:
    """Run one internal op in a subprocess and return its JSON result.

    A timeout is reported as `RESOLVE_GONE`, not raised. Resolve has two ways of
    failing here and only one of them is an exit: it can also **wedge** — stay
    up, hold its scripting socket open, and never answer — in which case this
    op's child blocks in `futex_wait` until the timeout. Letting that
    `TimeoutExpired` propagate killed the whole run mid-sweep and took the
    results file with it, which is the same false-reporting family as #151:
    a sweep that hit the bug produced no report at all rather than an honest
    one. Observed in the #153 bisect, on the same harness that elsewhere makes
    Resolve exit outright.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--internal-op", op],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": RESOLVE_GONE,
                "detail": f"op {op!r} did not answer within {timeout}s — Resolve is "
                          f"wedged (still up, no longer serving the scripting API)."}
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {"ok": False, "error": f"op {op!r} produced no result "
                                  f"(rc={proc.returncode}): {proc.stderr.strip()[:400]}"}


def _op_status() -> dict:
    from tests.preflight import collect_status

    return {"ok": True, **collect_status()}


def _op_projects() -> dict:
    from src.core.platform import setup_environment

    setup_environment()
    import DaVinciResolveScript as dvr_script

    resolve = dvr_script.scriptapp("Resolve")
    if not resolve:
        return {"ok": False, "error": RESOLVE_GONE}
    pm = resolve.GetProjectManager()
    return {"ok": True, "projects": list(pm.GetProjectListInCurrentFolder() or [])}


def _scratch_media() -> str | None:
    """Path to the synthetic scratch clip, generating it if absent.

    The scratch timeline needs a clip on it, not just to exist: a harness like
    `live_timeline_end_frame_probe` reads frame ranges and correctly bails on an
    empty timeline, so an empty scratch state would skip it in every sweep while
    it passes standalone — the exact class of sweep-only wrongness #151 is about.
    """
    if SCRATCH_MEDIA.exists():
        return str(SCRATCH_MEDIA)
    SCRATCH_MEDIA.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=gray:s=640x360:r=24:d=3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(SCRATCH_MEDIA),
        ], check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return str(SCRATCH_MEDIA) if SCRATCH_MEDIA.exists() else None


def _op_scratch() -> dict:
    """Put Resolve into the known state every harness may assume: the scratch
    project loaded, with a current timeline that has a clip on it. Harnesses that
    create their own disposable project still work; they just no longer inherit
    whatever the previous one happened to leave behind."""
    from src.core.platform import setup_environment

    setup_environment()
    import DaVinciResolveScript as dvr_script

    resolve = dvr_script.scriptapp("Resolve")
    if not resolve:
        return {"ok": False, "error": RESOLVE_GONE}
    pm = resolve.GetProjectManager()
    existing = list(pm.GetProjectListInCurrentFolder() or [])
    project = pm.LoadProject(SCRATCH_PROJECT) if SCRATCH_PROJECT in existing else None
    if project is None:
        project = pm.CreateProject(SCRATCH_PROJECT)
    if project is None:
        return {"ok": False, "error": f"could not create or load {SCRATCH_PROJECT!r}"}

    media_pool = project.GetMediaPool()
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        for index in range(1, (project.GetTimelineCount() or 0) + 1):
            candidate = project.GetTimelineByIndex(index)
            if candidate is not None and candidate.GetName() == SCRATCH_TIMELINE:
                timeline = candidate
                break
    if timeline is None:
        # Build it from the clip rather than CreateEmptyTimeline + append: one
        # call, and the result is a timeline that ends on a clip.
        clip_path = _scratch_media()
        imported = media_pool.ImportMedia([clip_path]) if clip_path else None
        if imported:
            timeline = media_pool.CreateTimelineFromClips(SCRATCH_TIMELINE, imported)
        if timeline is None:
            timeline = media_pool.CreateEmptyTimeline(SCRATCH_TIMELINE)
    if timeline is not None:
        project.SetCurrentTimeline(timeline)
        # A scratch timeline from an earlier run (or a failed import) can exist
        # but be empty; top it up rather than leaving the thin state in place.
        if not (timeline.GetItemListInTrack("video", 1) or []):
            clip_path = _scratch_media()
            imported = media_pool.ImportMedia([clip_path]) if clip_path else None
            if imported:
                media_pool.AppendToTimeline(imported)
    # The current page is state too: a harness that finishes on Fusion or Color
    # leaves the next one somewhere it never asked to be. Park on edit so every
    # harness starts from the same page.
    resolve.OpenPage("edit")
    return {
        "ok": timeline is not None,
        "project": project.GetName(),
        "timeline": timeline.GetName() if timeline else None,
        "timeline_clips": len(timeline.GetItemListInTrack("video", 1) or []) if timeline else 0,
        "error": None if timeline else "scratch project has no timeline",
    }


def _op_delete(names: list) -> dict:
    from src.core.platform import setup_environment

    setup_environment()
    import DaVinciResolveScript as dvr_script

    resolve = dvr_script.scriptapp("Resolve")
    if not resolve:
        return {"ok": False, "error": RESOLVE_GONE}
    pm = resolve.GetProjectManager()
    current = pm.GetCurrentProject()
    # DeleteProject refuses the loaded project, so step off it first.
    if current is not None and current.GetName() in names:
        pm.LoadProject(SCRATCH_PROJECT) or pm.CreateProject(SCRATCH_PROJECT)
    deleted = [name for name in names if pm.DeleteProject(name)]
    return {"ok": True, "deleted": deleted,
            "failed": [n for n in names if n not in deleted]}


INTERNAL_OPS = {"status": _op_status, "projects": _op_projects, "scratch": _op_scratch}


# ── harness discovery ─────────────────────────────────────────────────────────

def discover(pattern: str | None) -> list:
    """Every live harness under tests/, sorted, optionally substring-filtered."""
    harnesses = sorted(ROOT.joinpath("tests").rglob("live_*.py"))
    if pattern:
        harnesses = [h for h in harnesses if pattern in str(h.relative_to(ROOT))]
    return harnesses


def is_cold(path: Path) -> bool:
    """True for the cold-launch harnesses, which need Resolve fully QUIT.

    Read off the preflight call rather than a hand-kept list so a new cold
    harness is partitioned correctly the day it lands.
    """
    try:
        return 'gate("closed")' in path.read_text(encoding="utf-8")
    except OSError:
        return False


# ── the sweep ─────────────────────────────────────────────────────────────────

def classify(returncode: int) -> str:
    """Map a harness exit code through the preflight contract. 2/3 mean the
    environment was not ready — a skip, not a failure. 1 is a real failure."""
    if returncode == 0:
        return PASS
    if returncode in (2, 3):
        return SKIP
    return FAIL


def run_harness(path: Path, env: dict, timeout: int, quiet: bool) -> dict:
    rel = str(path.relative_to(ROOT))
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            env=env,
            # A harness (or its ffmpeg child) that reads stdin would otherwise
            # eat the rest of the run — see the module docstring.
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
        )
        status, returncode = classify(proc.returncode), proc.returncode
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        status, returncode = TIMEOUT, None
        output = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
    elapsed = time.time() - started

    if not quiet and status in (FAIL, TIMEOUT):
        tail = [ln for ln in output.splitlines() if ln.strip()][-15:]
        for line in tail:
            print(f"      | {line}")
    return {"harness": rel, "status": status, "returncode": returncode,
            "seconds": round(elapsed, 1), "output": output}


def take_vitals(enabled: bool) -> dict | None:
    """One `/proc` sample of the Resolve process, or None when not asked for.

    Read from `/proc`, never through the scripting API: the sample has to
    survive the moment it exists to describe (#153), and a bridge call into a
    dying Resolve takes its caller down with it.
    """
    if not enabled:
        return None
    return resolve_vitals.sample()


def sweep(harnesses: list, env: dict, args) -> tuple:
    results = []
    before_all = resolve_op("projects", env).get("projects", []) if not args.cold else []
    baseline = take_vitals(args.vitals)
    if baseline:
        print(f"[vitals] baseline  {resolve_vitals.format_sample(baseline)}", flush=True)

    for index, path in enumerate(harnesses, 1):
        rel = str(path.relative_to(ROOT))
        print(f"[{index}/{len(harnesses)}] {rel}", flush=True)

        if not args.cold:
            scratch = resolve_op("scratch", env)
            if not scratch.get("ok"):
                print(f"      ! could not re-establish scratch state: {scratch.get('error')}")
            if scratch.get("error") == RESOLVE_GONE:
                # Resolve died mid-sweep. Every remaining harness would exit 2 and
                # be recorded as SKIP, so the run would end "0 FAIL" while having
                # validated nothing. Stop and say so instead.
                print(f"      ! Resolve is no longer responding — aborting with "
                      f"{len(harnesses) - index + 1} harness(es) unrun.")
                # The sample at the abort point is the one that matters: it says
                # whether the process is gone outright or still up but no longer
                # answering, which are different bugs (#153) — and it is always
                # taken here, not only under --vitals, because one `/proc` read
                # is free and the distinction is the whole diagnosis.
                dying = take_vitals(True)
                mode = ("EXITED — no Resolve process remains"
                        if not dying.get("alive")
                        else "WEDGED — the process is still up but no longer "
                             "serving the scripting API")
                print(f"      ! Resolve {mode}")
                print(f"      ! vitals at abort: {resolve_vitals.format_sample(dying)}")
                results.append({"harness": rel, "status": ABORTED, "returncode": None,
                                "seconds": 0.0, "vitals": dying,
                                "output": f"Resolve stopped responding before this "
                                          f"harness ran. {mode}. "
                                          f"{scratch.get('detail', '')}"})
                break
            before = resolve_op("projects", env).get("projects", [])
        else:
            before = []

        result = run_harness(path, env, args.timeout, args.quiet)

        died_after = False
        if not args.cold:
            after_op = resolve_op("projects", env)
            # Resolve can go down *during* the harness that just ran — which is
            # exactly what #153 does, and the harness still exits 0 because its
            # assertions had already passed and only its cleanup hit the dead
            # bridge. Catch it here rather than on the next iteration, or the
            # last harness in a run would end the sweep looking green.
            died_after = after_op.get("error") == RESOLVE_GONE
            after = after_op.get("projects", [])
            leaked = [p for p in after if p not in before and p != SCRATCH_PROJECT]
            result["leaked_projects"] = leaked
            if leaked:
                print(f"      ! leaked projects: {', '.join(leaked)}")
        result["vitals"] = take_vitals(args.vitals or died_after)
        results.append(result)
        print(f"      {result['status']} in {result['seconds']}s", flush=True)
        if result["vitals"] and args.vitals:
            growth = resolve_vitals.delta(baseline or {}, result["vitals"])
            print(f"      vitals {resolve_vitals.format_sample(result['vitals'])}"
                  f"{f'  since baseline: {growth}' if growth else ''}", flush=True)

        if died_after:
            alive = (result["vitals"] or {}).get("alive")
            mode = ("WEDGED — still up, no longer serving the scripting API"
                    if alive else "EXITED — no Resolve process remains")
            print(f"      ! Resolve went down DURING this harness: {mode}")
            print(f"      ! aborting with {len(harnesses) - index} harness(es) unrun.")
            result["resolve_went_down"] = mode
            results.append({"harness": "(sweep aborted)", "status": ABORTED,
                            "returncode": None, "seconds": 0.0,
                            "vitals": result["vitals"],
                            "output": f"Resolve went down during {rel}. {mode}."})
            break

    if not args.cold and args.clean_leaks:
        after_all = resolve_op("projects", env).get("projects", [])
        leftovers = [p for p in after_all if p not in before_all and p != SCRATCH_PROJECT]
        if leftovers:
            deleted = resolve_op("delete:" + ",".join(leftovers), env)
            print(f"Deleted {len(deleted.get('deleted', []))} leaked project(s): "
                  f"{', '.join(deleted.get('deleted', []))}")
    return results, baseline


# ── reporting ─────────────────────────────────────────────────────────────────

def vitals_report(results: list, baseline: dict | None) -> str:
    """Resolve's resource curve across the sweep, or "" when --vitals was off.

    Prints the growth from the baseline to the last live sample plus the three
    harnesses that grew RSS the most, because #153's question is not "did it
    grow" — a video app's RSS always grows — but "does anything grow without
    bound, and which harness is doing it".
    """
    samples = [(r["harness"], r["vitals"]) for r in results if r.get("vitals")]
    if baseline is None and not samples:
        return ""
    lines = ["  Resolve vitals:\n"]
    # The baseline counts as a sample for liveness: a sweep whose every
    # per-harness reading is dead but whose baseline was alive is the exit
    # happening during harness 1, not "Resolve was never there".
    live = [(harness, v) for harness, v in samples if v.get("alive")]
    if not live and not (baseline and baseline.get("alive")):
        lines.append("    no live sample — Resolve was already gone when the "
                     "sweep started sampling.\n")
        return "".join(lines)
    first = baseline if baseline and baseline.get("alive") else live[0][1]
    if not live:
        live = [("(baseline)", first)]
    lines.append(f"    first  {resolve_vitals.format_sample(first)}\n")
    lines.append(f"    last   {resolve_vitals.format_sample(live[-1][1])}\n")
    lines.append(f"    growth {resolve_vitals.delta(first, live[-1][1])}\n")
    fds, limit = live[-1][1].get("fds"), live[-1][1].get("fd_limit")
    if fds and limit and fds > limit * 0.8:
        lines.append(f"    ! fd table is {round(100 * fds / limit)}% of the soft "
                     f"limit — exhaustion is a live theory for this run.\n")
    steps, previous = [], first
    for harness, reading in live:
        grew = resolve_vitals.delta(previous, reading).get("rss_kb", 0)
        steps.append((grew, harness))
        previous = reading
    for grew, harness in sorted(steps, reverse=True)[:3]:
        if grew > 0:
            lines.append(f"    +{grew // 1024}M RSS  {harness}\n")
    if any(not v.get("alive") for _, v in samples):
        lines.append("    ! a sample found no Resolve process at all — it exited, "
                     "it did not merely stop answering.\n")
    return "".join(lines)


def summarize(results: list, out_path: Path, started_at: float,
              baseline: dict | None = None) -> int:
    counts = {status: sum(1 for r in results if r["status"] == status)
              for status in (PASS, FAIL, SKIP, TIMEOUT, ABORTED)}
    print()
    print("=" * 72)
    for result in results:
        if result["status"] != PASS:
            print(f"  [{result['status']}] {result['harness']}")
    leaks = {r["harness"]: r["leaked_projects"] for r in results if r.get("leaked_projects")}
    if leaks:
        print("  Leaked disposable projects (harness → project):")
        for harness, projects in leaks.items():
            print(f"    {harness} → {', '.join(projects)}")
    print(f"  {counts[PASS]} PASS / {counts[SKIP]} SKIP / {counts[FAIL]} FAIL"
          f"{f' / {counts[TIMEOUT]} TIMEOUT' if counts[TIMEOUT] else ''}"
          f"  ({round(time.time() - started_at)}s)")
    if counts[ABORTED]:
        print("  RUN INCOMPLETE — Resolve stopped responding; the harnesses after "
              "the abort did not run.")
    print(vitals_report(results, baseline), end="")
    print("=" * 72)

    payload = {
        "started_at_epoch": started_at,
        "duration_seconds": round(time.time() - started_at),
        "counts": counts,
        "leaked_projects": leaks,
        # Only present with --vitals; the per-harness readings ride along inside
        # each result, so a sweep that aborted still carries the whole curve.
        "vitals_baseline": baseline,
        # The full harness output is the point of a results file: a sweep-only
        # failure is only diagnosable from what the harness printed at the time.
        "results": results,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Results written to {out_path}")
    # An aborted run is not a pass: it validated nothing after the abort point,
    # and reporting 0 is exactly the false green #151 is about.
    return 1 if counts[FAIL] or counts[TIMEOUT] or counts[ABORTED] else 0


# ── entry point ───────────────────────────────────────────────────────────────

def preflight(env: dict, cold: bool) -> int:
    """Refuse to start a sweep Resolve cannot serve. Returns an exit code, or 0."""
    status = resolve_op("status", env)
    state = status.get("state")
    if cold:
        if state != "closed":
            print(f"[runner] NOT READY — --cold needs Resolve fully QUIT, found {state!r}.")
            return 2
        print("[runner] Resolve is closed — running the cold-launch harnesses.")
        return 0

    if state == "scripting_unavailable":
        print(f"[runner] NOT READY — {status.get('detail')}")
        return 3
    if state == "closed":
        print("[runner] NOT READY — start DaVinci Resolve (Studio) first.")
        return 2
    if status.get("page") is None:
        # The trap that reads as a Resolve bug: with only the Project Manager
        # window up, every page query returns None and the media pool goes inert
        # — ImportMedia and AddSubFolder return None with no error. Loading a
        # real project usually opens the project window and revives the page API,
        # so try that before refusing; what does NOT recover is the auto-created
        # "Untitled Project", which is never persisted and cannot be re-loaded.
        print("[runner] Resolve's UI is parked on the Project Manager (page is "
              "None) — loading the scratch project to revive it.")
        resolve_op("scratch", env)
        status = resolve_op("status", env)
    if status.get("page") is None:
        print("[runner] NOT READY — the page API is still dead, so the media pool "
              "is inert and every harness would fail misleadingly.")
        print("[runner] Open a project in the UI (double-click it), then re-run.")
        return 2
    if status.get("rendering"):
        print("[runner] NOT READY — a render is in progress (possibly wedged).")
        return 2
    print(f"[runner] {status.get('product')} {status.get('version')} — "
          f"project {status.get('project')!r}, page {status.get('page')!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--internal-op", help=argparse.SUPPRESS)
    parser.add_argument("-k", "--filter", dest="pattern",
                        help="Run only harnesses whose path contains this substring.")
    parser.add_argument("--cold", action="store_true",
                        help="Run ONLY the cold-launch harnesses, which need Resolve quit.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Per-harness timeout in seconds (default {DEFAULT_TIMEOUT}).")
    parser.add_argument("--clean-leaks", action="store_true",
                        help="Delete disposable projects the sweep left behind (asks nothing).")
    parser.add_argument("--vitals", action="store_true",
                        help="Sample Resolve's RSS/fds/threads/GPU between harnesses "
                             "and report the growth curve (issue #153).")
    parser.add_argument("--quiet", action="store_true",
                        help="Do not print the output tail of failing harnesses.")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "tests" / "live_suite_results.json",
                        help="Where to write the JSON results file.")
    parser.add_argument("--list", action="store_true",
                        help="List the harnesses that would run and exit.")
    args = parser.parse_args()

    if args.internal_op:
        op = args.internal_op
        try:
            result = (_op_delete(op[len("delete:"):].split(","))
                      if op.startswith("delete:") else INTERNAL_OPS[op]())
        except Exception as exc:  # noqa: BLE001 — the parent reads this as data
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, default=str))
        return 0

    harnesses = [h for h in discover(args.pattern) if is_cold(h) == args.cold]
    if not harnesses:
        print("No harnesses matched.")
        return 2
    if args.list:
        for path in harnesses:
            print(path.relative_to(ROOT))
        return 0

    env = build_env()
    not_ready = preflight(env, args.cold)
    if not_ready:
        return not_ready

    started_at = time.time()
    print(f"[runner] {len(harnesses)} harness(es), {args.timeout}s timeout each")
    results, baseline = sweep(harnesses, env, args)
    return summarize(results, args.out, started_at, baseline)


if __name__ == "__main__":
    raise SystemExit(main())
