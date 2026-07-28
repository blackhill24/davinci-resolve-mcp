#!/usr/bin/env python3
"""Settle whether `Timeline.GetEndFrame()` is inclusive — #141 finding 6.

The repo had three duration conventions for the same call: `end - start` in
core/brain_edits.py, `end - start + 1` in granular/timeline.py and in
project_lifecycle/utils/project_properties.py. At most one could be right; the
others reported a duration off by one frame. They now share
`core/timeline_lookup.timeline_frame_duration`, which is end-INCLUSIVE (the
convention two of the three sites, and both user-facing "duration" fields,
already used) — but that choice is still an assumption, not a measurement:
`Timeline.GetEndFrame()` inclusivity is not catalogued in `src/core/api_truth.py`.

This probe measures it. It is READ-ONLY: it opens nothing, creates nothing,
deletes nothing and changes no setting. It needs a project with a CURRENT
TIMELINE whose length you can corroborate, so it cannot run unattended — hence a
probe rather than a `live_*` assertion harness.

Run (it gates through preflight itself):
    .venv/bin/python tests/live_timeline_end_frame_probe.py

Exit codes follow the preflight contract: 0 measured, 2 environment not ready,
3 scripting unavailable. 1 is deliberately unused.

What it reports, per track, is the record-out of the LAST clip against
GetEndFrame(). `TimelineItem.GetEnd()` is exclusive (that is what
`_timeline_item_duration` relies on), so:

    GetEndFrame() == max(last_item.GetEnd())        -> EXCLUSIVE, drop the +1
    GetEndFrame() == max(last_item.GetEnd()) - 1    -> INCLUSIVE, keep the +1

Record the answer in `src/core/api_truth.py` and, if it comes out exclusive,
change the single `+ 1` in `timeline_frame_duration` — one edit, because all
three call sites now go through it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_MEASURED = 0
EXIT_NOT_READY = 2
EXIT_NO_SCRIPTING = 3


def main() -> int:
    from tests.preflight import gate

    # Per tests/GUARDS.md every live_* __main__ gates through preflight, which
    # prints the status line, exits 2/3 (never 1) when the environment is not
    # ready, and sets DAVINCI_MCP_NO_AUTOLAUNCH so nothing is launched.
    gate("timeline")

    from src.core.live_connection import get_resolve

    resolve = get_resolve()
    if resolve is None:
        print("Resolve not reachable — start Resolve Studio and enable external scripting.")
        return EXIT_NOT_READY

    print(f"{resolve.GetProductName()} {resolve.GetVersionString()}")
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    if project is None:
        print("No project open. Open one with a timeline, then re-run.")
        return EXIT_NOT_READY
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        print(f"Project {project.GetName()!r} has no current timeline. Open one, then re-run.")
        return EXIT_NOT_READY

    start = int(timeline.GetStartFrame())
    end = int(timeline.GetEndFrame())
    print(f"project:  {project.GetName()}")
    print(f"timeline: {timeline.GetName()}")
    print(f"GetStartFrame() = {start}")
    print(f"GetEndFrame()   = {end}")
    print(f"end - start     = {end - start}")
    print(f"end - start + 1 = {end - start + 1}")

    last_ends = []
    for track_type in ("video", "audio", "subtitle"):
        count = int(timeline.GetTrackCount(track_type) or 0)
        for track_index in range(1, count + 1):
            items = timeline.GetItemListInTrack(track_type, track_index) or []
            if not items:
                continue
            item_end = int(items[-1].GetEnd())
            last_ends.append(item_end)
            print(f"  {track_type}{track_index}: {len(items)} item(s), "
                  f"last GetEnd() = {item_end}")

    if not last_ends:
        print("\nTimeline is empty — no clip record-out to compare against. "
              "Open a timeline with at least one clip.")
        return EXIT_NOT_READY

    furthest = max(last_ends)
    print(f"\nfurthest clip record-out (exclusive): {furthest}")
    if end == furthest:
        verdict = ("EXCLUSIVE — GetEndFrame() is one past the last frame. "
                   "Drop the `+ 1` in timeline_lookup.timeline_frame_duration.")
    elif end == furthest - 1:
        verdict = ("INCLUSIVE — GetEndFrame() is the last frame itself. "
                   "The current `+ 1` is correct.")
    else:
        verdict = (f"INCONCLUSIVE — GetEndFrame() ({end}) is neither {furthest} nor "
                   f"{furthest - 1}. The timeline probably has a non-zero start "
                   "offset or trailing gap; re-run on a timeline that ends on a clip.")
    print(f"VERDICT: {verdict}")
    print("\nRecord the answer in src/core/api_truth.py (#141 finding 6).")
    return EXIT_MEASURED


if __name__ == "__main__":
    raise SystemExit(main())
