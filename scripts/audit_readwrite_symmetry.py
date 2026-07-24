#!/usr/bin/env python3
"""Audit read/write symmetry across the compound server's action surface.

For every mutating action (set_/add_/create_/...) it checks whether a matching
read action (get_/list_/...) exists on the same tool, and reports the
asymmetries. The goal is to find write-without-read gaps before users have to —
the repeatable feature-discovery method behind R5.

Reads the `_unknown(action, [...])` lists that enumerate every action a tool
accepts. The compound action surface lives in `src/domains/*/actions.py` (moved
there in the #52 restructure); `src/server.py` still hosts a few. Scans BOTH.

Exit code is meaningful (#110 finding 13): the `set_`-without-`get_` gaps are
compared against BASELINE_HIGH_SIGNAL_GAPS below. A NEW gap not in the baseline
fails the audit (returncode 1) so it fails CI. Closing a gap that is still
listed in the baseline also fails, forcing the baseline to shrink with the
surface. Update BASELINE_HIGH_SIGNAL_GAPS deliberately when you add or fix one.
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

READ_PREFIXES = ("get_", "list_", "probe_", "is_", "has_", "find_")
# `set_` is the high-signal class: a set with no get is a genuine readback gap.
# create_/add_/insert_/import_ are inherently writes that usually have no paired
# read of the same noun, so they're reported separately as low-signal.
HIGH_SIGNAL = ("set_",)
LOW_SIGNAL = ("add_", "create_", "insert_", "apply_", "import_")

# Known `set_`-without-`get_` gaps accepted at audit time. Each is a write whose
# state is either not round-trippable through the API or read under a different
# noun. A NEW gap outside this set fails the audit; fixing one listed here also
# fails until it is removed from this baseline (keeps the list honest).
BASELINE_HIGH_SIGNAL_GAPS = frozenset({
    "set_cache",                 # fusion_comp: cache toggle, no paired reader
    "set_caps_preset",           # capability preset apply, read via probe
    "set_cdl",                   # timeline_item_color: CDL write, no GetCDL
    "set_clip_super_scale",      # media_pool_item: write-only super-scale
    "set_high_priority",         # resolve_control: process priority, no reader
    "set_keyframe_interpolation",  # keyframe interp write, no paired reader
    "set_mcp_update_policy",     # setup: update policy write, read via status
    "set_name",                  # rename verbs; the noun is read via list/get_*
    "set_node_enabled",          # graph: node enable write, no GetNodeEnabled
    "set_super_scale",           # media_pool_item: write-only super-scale
})


def _source_files():
    files = sorted(glob.glob(os.path.join(ROOT, "src", "domains", "*", "actions.py")))
    files.append(os.path.join(ROOT, "src", "server.py"))
    return files


def _action_lists(src):
    """Yield lists of action-name string literals from each _unknown(action, [...])."""
    for m in re.finditer(r"_unknown\(action,\s*\[(.*?)\]\)", src, re.DOTALL):
        names = re.findall(r'"([a-z][a-z0-9_]*)"', m.group(1))
        if names:
            yield names


def _has_read(stem: str, aset: set) -> bool:
    # Match get_<stem>, list_<stem>, and plural get_<stem>s (e.g. add_keyframe -> get_keyframes).
    candidates = {rp + stem for rp in READ_PREFIXES}
    candidates |= {rp + stem + "s" for rp in READ_PREFIXES}
    candidates |= {rp + stem.rstrip("s") for rp in READ_PREFIXES}
    return bool(candidates & aset)


def audit_src(src):
    """Audit a single Python source string (the _unknown(action, [...]) lists
    it contains). Returns (total, covered, high, low). Unit-testable without
    touching the filesystem."""
    high, low = set(), set()
    covered, total = 0, 0
    for actions in _action_lists(src):
        aset = set(actions)
        for a in actions:
            for wp in HIGH_SIGNAL + LOW_SIGNAL:
                if a.startswith(wp):
                    total += 1
                    stem = a[len(wp):]
                    if _has_read(stem, aset):
                        covered += 1
                    elif wp in HIGH_SIGNAL:
                        high.add(a)
                    else:
                        low.add(a)
                    break
    return total, covered, sorted(high), sorted(low)


def audit(files):
    high, low = set(), set()
    covered, total = 0, 0
    for path in files:
        with open(path, encoding="utf-8") as handle:
            src = handle.read()
        t, c, h, lo = audit_src(src)
        total += t
        covered += c
        high |= set(h)
        low |= set(lo)
    return total, covered, sorted(high), sorted(low)


def main():
    files = _source_files()
    total, covered, high, low = audit(files)
    print("# Read/Write Symmetry Audit\n")
    print(f"- source files scanned: **{len(files)}**")
    print(f"- write-style actions scanned: **{total}**")
    print(f"- with a matching read: **{covered}**")
    print(f"- `set_` actions with no `get_`/`list_` (real readback gaps): **{len(high)}**\n")
    if high:
        print("## High-signal gaps — `set_` with no read counterpart\n")
        for a in sorted(high):
            marker = "" if a in BASELINE_HIGH_SIGNAL_GAPS else "  ⚠ NEW"
            print(f"- `{a}`{marker}")
    print(f"\n## Low-signal (create/add/insert/import — usually expected): {len(low)}\n")
    print(", ".join(f"`{a}`" for a in sorted(low)))

    new_gaps = set(high) - BASELINE_HIGH_SIGNAL_GAPS
    stale_baseline = BASELINE_HIGH_SIGNAL_GAPS - set(high)
    status = 0
    if new_gaps:
        print("\n---\n")
        print("**AUDIT FAILED** — new `set_` action(s) with no read counterpart:")
        for a in sorted(new_gaps):
            print(f"  - `{a}` — add a paired `get_`/`list_`, or add it to BASELINE_HIGH_SIGNAL_GAPS with a reason.")
        status = 1
    if stale_baseline:
        print("\n**AUDIT FAILED** — baseline lists gap(s) that no longer exist:")
        for a in sorted(stale_baseline):
            print(f"  - `{a}` — remove it from BASELINE_HIGH_SIGNAL_GAPS.")
        status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
