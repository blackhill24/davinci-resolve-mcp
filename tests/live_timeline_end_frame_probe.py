#!/usr/bin/env python3
"""Settle whether `Timeline.GetEndFrame()` is inclusive — #141 finding 6.

ANSWERED 2026-07-28 on Resolve Studio 21.0.2.4: it is **EXCLUSIVE**. A 48-frame
clip gives GetStartFrame()=86400 / GetEndFrame()=86448, a 100-frame clip gives
86400/86500, so the duration is `end - start` with no +1 — and the majority
convention in this repo was the wrong one. Recorded in src/core/api_truth.py and
implemented in core/timeline_lookup.timeline_frame_duration. Keep this probe for
re-measuring on a Resolve major bump.


The repo had three duration conventions for the same call: `end - start` in
core/brain_edits.py, `end - start + 1` in granular/timeline.py and in
project_lifecycle/utils/project_properties.py. At most one could be right; the
others reported a duration off by one frame. They now share
`core/timeline_lookup.timeline_frame_duration`, which is end-INCLUSIVE (the
convention two of the three sites, and both user-facing "duration" fields,
already used) — but that choice is still an assumption, not a measurement:
`Timeline.GetEndFrame()` inclusivity is not catalogued in `src/core/api_truth.py`.

This probe measures it, two ways:

- **Read-only (default).** Reads the CURRENT timeline and compares GetEndFrame()
  against the record-out of the last clip. Opens nothing, creates nothing,
  deletes nothing, changes no setting. Needs a timeline that ends on a clip.
- **`--allow-mutation`.** Builds its own scratch timeline from a synthetic clip
  of EXACTLY `--frames` frames (ffmpeg), measures against that known length, and
  deletes both again. Same opt-in convention as `live_api_probe.py`: writing into
  the user's open project is never implicit. This is the definitive form — the
  clip length is known rather than corroborated by eye.

Run (it gates through preflight itself):
    .venv/bin/python tests/live_timeline_end_frame_probe.py
    .venv/bin/python tests/live_timeline_end_frame_probe.py --allow-mutation

Exit codes follow the preflight contract: 0 measured, 2 environment not ready,
3 scripting unavailable. 1 is deliberately unused.

What it reports, per track, is the record-out of the LAST clip against
GetEndFrame(). `TimelineItem.GetEnd()` is exclusive (that is what
`_timeline_item_duration` relies on), so:

    GetEndFrame() == max(last_item.GetEnd())        -> EXCLUSIVE, drop the +1
    GetEndFrame() == max(last_item.GetEnd()) - 1    -> INCLUSIVE, keep the +1

If a future build CONTRADICTS the recorded EXCLUSIVE answer, it is one edit in
one place — `timeline_frame_duration` — because all three call sites go through
it, plus the `api_truth.py` entry.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_MEASURED = 0
EXIT_NOT_READY = 2
EXIT_NO_SCRIPTING = 3

SCRATCH_TIMELINE = "ZZ_end_frame_probe_scratch"
SCRATCH_BIN = "ZZ_end_frame_probe"


def _verdict(end: int, furthest: int) -> str:
    """Classify GetEndFrame() against the furthest clip record-out.

    `TimelineItem.GetEnd()` is EXCLUSIVE (that is what `_timeline_item_duration`
    relies on), so the last frame of the timeline is `furthest - 1`.
    """
    if end == furthest:
        return ("EXCLUSIVE — GetEndFrame() is one past the last frame. This is "
                "the 2026-07-28 measured answer and what "
                "timeline_lookup.timeline_frame_duration implements.")
    if end == furthest - 1:
        return ("INCLUSIVE — GetEndFrame() is the last frame itself. This CONTRADICTS "
                "the 2026-07-28 measurement; timeline_frame_duration would need a "
                "`+ 1` and src/core/api_truth.py is out of date for this build.")
    return (f"INCONCLUSIVE — GetEndFrame() ({end}) is neither {furthest} nor "
            f"{furthest - 1}. The timeline probably has a non-zero start "
            "offset or trailing gap; re-run with --allow-mutation, which builds "
            "a clip of known length instead.")


def resolve_current_page_is_none(_project) -> bool:
    """True when Resolve reports no current page.

    The page API returns None for every page while the UI sits on the Project
    Manager. In that state the media pool accepts nothing.
    """
    from src.core.live_connection import get_resolve

    resolve = get_resolve()
    try:
        return resolve is not None and resolve.GetCurrentPage() is None
    except Exception:
        return False


def _synthesize_clip(directory: str, frames: int, fps: int) -> str:
    """A silent test clip of EXACTLY `frames` frames. Returns its path."""
    path = os.path.join(directory, f"endframe_probe_{frames}f.mov")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size=320x180:rate={fps}",
         "-frames:v", str(frames), "-pix_fmt", "yuv420p",
         "-c:v", "mjpeg", "-q:v", "5", path],
        check=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    # Confirm the real frame count rather than trusting the encoder.
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "default=nokey=1:noprint_wrappers=1", path],
        capture_output=True, text=True, check=True, timeout=120,
        stdin=subprocess.DEVNULL,
    )
    actual = int((result.stdout or "0").strip() or 0)
    if actual != frames:
        raise RuntimeError(f"ffmpeg produced {actual} frames, wanted {frames}")
    return path


def _measure_current_timeline(timeline) -> int:
    """Read-only path: measure whatever timeline is already open."""
    start = int(timeline.GetStartFrame())
    end = int(timeline.GetEndFrame())
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
              "Open a timeline with at least one clip, or re-run with "
              "--allow-mutation to build one.")
        return EXIT_NOT_READY

    furthest = max(last_ends)
    print(f"\nfurthest clip record-out (exclusive): {furthest}")
    print(f"VERDICT: {_verdict(end, furthest)}")
    print("\nUpdate src/core/api_truth.py only if this CONTRADICTS the recorded "
          "answer (#141 finding 6).")
    return EXIT_MEASURED


def _measure_scratch_timeline(project, frames: int) -> int:
    """--allow-mutation path: build a clip of KNOWN length and measure it.

    Definitive, because the clip's frame count is asserted with ffprobe rather
    than corroborated by eye. Everything created here is deleted again.
    """
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("ffmpeg/ffprobe not on PATH — needed to synthesize a clip of known length.")
        return EXIT_NOT_READY

    fps_setting = project.GetSetting("timelineFrameRate")
    try:
        fps = int(round(float(str(fps_setting).split()[0])))
    except (TypeError, ValueError, IndexError):
        fps = 24
    print(f"project timeline frame rate: {fps_setting!r} -> {fps} fps")

    # A Resolve whose UI is parked on the PROJECT MANAGER answers every page
    # query with None and leaves the media pool inert: ImportMedia and even
    # AddSubFolder return None with no error, which reads as "Resolve imported
    # nothing" and wastes a run. Detect it up front and say so.
    if project.GetMediaPool() is None or resolve_current_page_is_none(project):
        print("Resolve's UI appears to be on the Project Manager (page API returns "
              "None), which leaves the media pool inert — imports silently fail. "
              "Open the project in the Resolve window, then re-run.")
        return EXIT_NOT_READY

    media_pool = project.GetMediaPool()
    scratch_dir = tempfile.mkdtemp(prefix="drm-endframe-probe-")
    timeline = None
    imported = []
    root = media_pool.GetRootFolder()
    original_folder = root
    scratch_folder = None
    try:
        clip_path = _synthesize_clip(scratch_dir, frames, fps)
        print(f"synthesized {frames}-frame clip: {clip_path}")

        # Keep the import out of the user's root folder.
        scratch_folder = media_pool.AddSubFolder(root, SCRATCH_BIN)
        if scratch_folder:
            media_pool.SetCurrentFolder(scratch_folder)
        imported = media_pool.ImportMedia([clip_path]) or []
        if not imported:
            print("Resolve imported nothing — cannot measure.")
            return EXIT_NOT_READY
        clip = imported[0]
        print(f"imported: {clip.GetName()} "
              f"(Frames property = {clip.GetClipProperty('Frames')})")

        timeline = media_pool.CreateTimelineFromClips(SCRATCH_TIMELINE, [clip])
        if not timeline:
            print("could not create the scratch timeline — cannot measure.")
            return EXIT_NOT_READY
        print(f"created scratch timeline {SCRATCH_TIMELINE!r} from that one clip")

        start = int(timeline.GetStartFrame())
        end = int(timeline.GetEndFrame())
        items = timeline.GetItemListInTrack("video", 1) or []
        print(f"\nGetStartFrame() = {start}")
        print(f"GetEndFrame()   = {end}")
        print(f"end - start     = {end - start}")
        print(f"end - start + 1 = {end - start + 1}")
        print(f"KNOWN clip length = {frames} frames")
        if items:
            item = items[0]
            print(f"item GetStart()={item.GetStart()} GetEnd()={item.GetEnd()} "
                  f"GetDuration()={item.GetDuration()}")

        # The decisive comparison: which arithmetic reproduces the known length?
        if end - start == frames:
            verdict = ("EXCLUSIVE — `end - start` equals the known clip length, so "
                       "GetEndFrame() is one past the last frame. This MATCHES the "
                       "2026-07-28 measurement and what "
                       "timeline_lookup.timeline_frame_duration implements.")
        elif end - start + 1 == frames:
            verdict = ("INCLUSIVE — `end - start + 1` equals the known clip length. "
                       "This CONTRADICTS the 2026-07-28 measurement: "
                       "timeline_frame_duration would need a `+ 1` and "
                       "src/core/api_truth.py is out of date for this build.")
        else:
            verdict = (f"INCONCLUSIVE — neither {end - start} nor {end - start + 1} "
                       f"equals the known {frames}. Resolve may have conformed the clip "
                       "to a different frame rate; check the fps line above.")
        print(f"\nVERDICT: {verdict}")
        print("\nUpdate src/core/api_truth.py only if this CONTRADICTS the recorded "
              "answer (#141 finding 6).")
        return EXIT_MEASURED
    finally:
        # Tear down in reverse order, reporting anything left behind rather
        # than failing silently.
        if timeline is not None:
            try:
                if not media_pool.DeleteTimelines([timeline]):
                    print(f"WARNING: could not remove scratch timeline "
                          f"{SCRATCH_TIMELINE!r} — delete it by hand")
            except Exception as exc:
                print(f"WARNING: removing scratch timeline raised {exc!r}")
        if imported:
            try:
                if not media_pool.DeleteClips(list(imported)):
                    print("WARNING: could not remove the imported probe clip — "
                          "delete it by hand")
            except Exception as exc:
                print(f"WARNING: removing the probe clip raised {exc!r}")
        try:
            media_pool.SetCurrentFolder(original_folder)
        except Exception:
            pass
        if scratch_folder is not None:
            try:
                if not media_pool.DeleteFolders([scratch_folder]):
                    print(f"WARNING: could not remove bin {SCRATCH_BIN!r} — "
                          "delete it by hand")
            except Exception as exc:
                print(f"WARNING: removing bin {SCRATCH_BIN!r} raised {exc!r}")
        shutil.rmtree(scratch_dir, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--allow-mutation", action="store_true",
        help="build a scratch timeline from a synthetic clip of known length, "
             "then delete both. Writes into the OPEN project.")
    parser.add_argument(
        "--frames", type=int, default=48,
        help="frame count of the synthetic clip (--allow-mutation only). Default 48.")
    args = parser.parse_args(argv)

    from tests.preflight import gate

    # Per tests/GUARDS.md every live_* __main__ gates through preflight, which
    # prints the status line, exits 2/3 (never 1) when the environment is not
    # ready, and sets DAVINCI_MCP_NO_AUTOLAUNCH so nothing is launched. The
    # mutation path builds its own timeline, so it only needs a project.
    gate("project" if args.allow_mutation else "timeline")

    from src.core.live_connection import get_resolve

    resolve = get_resolve()
    if resolve is None:
        print("Resolve not reachable — start Resolve Studio and enable external scripting.")
        return EXIT_NOT_READY

    print(f"{resolve.GetProductName()} {resolve.GetVersionString()}")
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    if project is None:
        print("No project open. Open one, then re-run.")
        return EXIT_NOT_READY
    print(f"project:  {project.GetName()}")

    if args.allow_mutation:
        if args.frames < 2:
            print("--frames must be at least 2.")
            return EXIT_NOT_READY
        return _measure_scratch_timeline(project, args.frames)

    timeline = project.GetCurrentTimeline()
    if timeline is None:
        print(f"Project {project.GetName()!r} has no current timeline. Open one, "
              "or re-run with --allow-mutation.")
        return EXIT_NOT_READY
    return _measure_current_timeline(timeline)


if __name__ == "__main__":
    raise SystemExit(main())
