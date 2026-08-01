#!/usr/bin/env python3
"""Live probe: how does Resolve place OFF-RATE media on a fixed-rate timeline?

Requires DaVinci Resolve Studio running. Montage refuses mixed-fps briefs
(``montage_edit.build_cut_list_for_brief``) because the grid-locked cutter
maps one seconds->frames conversion onto every shot. Lifting that refusal
needs one fact that cannot be read out of the API docs or inferred offline:

    when AppendToTimeline places a clip whose media rate differs from the
    timeline rate, using startFrame/endFrame in the CLIP's own numbering,
    is the resulting timeline duration
      (a) the same FRAME COUNT   -> Resolve conforms; the clip plays at a
          different speed and N source frames always cost N timeline frames, or
      (b) the same WALL-CLOCK TIME -> Resolve resamples; N source frames cost
          N * (timeline_fps / clip_fps) timeline frames.

(a) and (b) demand different arithmetic in the cutter, and guessing wrong
puts every cut after the first off-rate shot off the beat. So measure it.

Uses the same reference footage the montage-quality harness drives
(29.97fps clips + one 59.94fps clip). Never modifies source media: it only
imports and appends. Works in a disposable project, deleted at the end; the
previous project is restored.

Run: PYTHONPATH=. .venv/bin/python tests/domains/auto_edit/live_mixed_fps_probe.py
"""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

MEDIA_DIR = "/home/jon/Downloads/visdeo"
ON_RATE = os.path.join(MEDIA_DIR, "Blue sky.MP4")                      # 29.97
OFF_RATE = os.path.join(MEDIA_DIR, "DJI_20260530130257_0230_D.MP4")    # 59.94
TIMELINE_FPS = 29.97
TRIM_FRAMES = 60  # source frames requested from each clip

PILOT = f"mixed_fps_probe_{time.strftime('%H%M%S')}"
CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{' — ' + detail if detail else ''}")


def _clip_fps(clip) -> float | None:
    for key in ("FPS", "Frame Rate"):
        try:
            raw = clip.GetClipProperty(key)
        except Exception:
            continue
        m = re.search(r"\d+(?:\.\d+)?", str(raw or ""))
        if m:
            return float(m.group(0))
    return None


def run(s) -> int:
    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting")
        return 2
    for path in (ON_RATE, OFF_RATE):
        if not os.path.isfile(path):
            print(f"reference media missing: {path}")
            return 2

    pm = r.GetProjectManager()
    previous_project = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    proj = pm.CreateProject(PILOT)
    check("disposable project created", proj is not None, PILOT)
    if proj is None:
        return 2

    try:
        proj.SetSetting("timelineFrameRate", str(TIMELINE_FPS))
        mp = proj.GetMediaPool()
        ms = r.GetMediaStorage()
        added = ms.AddItemListToMediaPool([ON_RATE, OFF_RATE]) or []
        check("both reference clips imported", len(added) == 2, f"{len(added)} item(s)")
        if len(added) != 2:
            return 1

        by_name = {}
        for item in added:
            by_name[os.path.basename(str(item.GetClipProperty("File Path") or ""))] = item
        on_clip = by_name.get(os.path.basename(ON_RATE))
        off_clip = by_name.get(os.path.basename(OFF_RATE))
        check("clips resolvable by file path", on_clip is not None and off_clip is not None)
        if on_clip is None or off_clip is None:
            return 1

        on_fps, off_fps = _clip_fps(on_clip), _clip_fps(off_clip)
        check("media pool reports the two DIFFERENT rates", bool(on_fps and off_fps)
              and abs(on_fps - off_fps) > 1.0, f"on={on_fps} off={off_fps}")

        tl = mp.CreateEmptyTimeline(f"{PILOT}_tl")
        check("timeline created", tl is not None)
        if tl is None:
            return 1
        m = re.search(r"\d+(?:\.\d+)?", str(tl.GetSetting("timelineFrameRate") or ""))
        actual_tl_fps = float(m.group(0)) if m else None
        check("timeline actually runs at the requested rate",
              actual_tl_fps is not None and abs(actual_tl_fps - TIMELINE_FPS) < 0.05,
              str(actual_tl_fps))

        # Identical source-frame trims from each clip, appended back to back.
        appended = mp.AppendToTimeline([
            {"mediaPoolItem": on_clip, "startFrame": 0, "endFrame": TRIM_FRAMES},
            {"mediaPoolItem": off_clip, "startFrame": 0, "endFrame": TRIM_FRAMES},
        ])
        check("both trims appended", bool(appended) and len(appended) == 2,
              f"{len(appended or [])} item(s)")
        if not appended or len(appended) != 2:
            return 1

        items = tl.GetItemListInTrack("video", 1) or []
        check("two items on V1", len(items) == 2, f"{len(items)}")
        if len(items) != 2:
            return 1
        on_dur = int(items[0].GetDuration())
        off_dur = int(items[1].GetDuration())
        print(f"\n    on-rate  ({on_fps}fps): {TRIM_FRAMES} source frames -> "
              f"{on_dur} timeline frames ({on_dur / TIMELINE_FPS:.3f}s)")
        print(f"    off-rate ({off_fps}fps): {TRIM_FRAMES} source frames -> "
              f"{off_dur} timeline frames ({off_dur / TIMELINE_FPS:.3f}s)")

        check("on-rate trim costs exactly the frames asked for",
              abs(on_dur - TRIM_FRAMES) <= 1, f"{on_dur} vs {TRIM_FRAMES}")

        resampled = int(round(TRIM_FRAMES * TIMELINE_FPS / (off_fps or TIMELINE_FPS)))
        if abs(off_dur - TRIM_FRAMES) <= 1:
            verdict = ("(a) CONFORM — N source frames always cost N timeline frames, "
                       "so the cutter can keep one frame-count arithmetic and only the "
                       "IN-POINT (seconds -> frames) needs the clip's own rate")
        elif abs(off_dur - resampled) <= 1:
            verdict = ("(b) RESAMPLE — the clip keeps its wall-clock length, so every "
                       "source range must be converted through the clip's own rate")
        else:
            verdict = (f"NEITHER — {off_dur} frames matches neither {TRIM_FRAMES} (conform) "
                       f"nor {resampled} (resample); investigate before changing the cutter")
        print(f"\n    VERDICT: {verdict}\n")
        check("off-rate placement matches a known model",
              abs(off_dur - TRIM_FRAMES) <= 1 or abs(off_dur - resampled) <= 1, verdict)

        # The number the cutter actually needs: does the SECOND item start where
        # the first ended (no gap/overlap) — i.e. is the record cursor additive
        # in timeline frames regardless of the mix of rates?
        gap = int(items[1].GetStart()) - int(items[0].GetEnd())
        check("off-rate item butts straight up against the on-rate one", gap == 0,
              f"gap={gap} frames")

        # Now the real claim: run montage_edit's OWN arithmetic and check the
        # timeline agrees. A beat-derived record length in timeline frames plus
        # an in-point in seconds must produce source frames that cost exactly
        # that many timeline frames — which is what keeps a mixed brief on the
        # beat grid.
        from src.domains.auto_edit.utils import cut_ir
        tl2 = mp.CreateEmptyTimeline(f"{PILOT}_tl2")
        if tl2 is None:
            check("second timeline created", False)
            return 1
        record_len = 40                    # e.g. 2 beats at ~90 BPM on a 29.97 timeline
        in_point_seconds = 2.0
        rows = []
        for clip, clip_fps in ((on_clip, on_fps), (off_clip, off_fps)):
            src_start = int(round(in_point_seconds * clip_fps))
            src_end = src_start + max(1, int(round(record_len * clip_fps / TIMELINE_FPS)))
            seg = {"source_start_frame": src_start, "source_end_frame": src_end,
                   "record_length_frames": record_len}
            check(f"segment_record_length is the TIMELINE length for {clip_fps}fps media",
                  cut_ir.segment_record_length(seg) == record_len,
                  f"src span={src_end - src_start}, record={cut_ir.segment_record_length(seg)}")
            rows.append({"mediaPoolItem": clip, "startFrame": src_start, "endFrame": src_end})
        if not mp.AppendToTimeline(rows):
            check("montage-shaped rows appended", False)
            return 1
        placed = tl2.GetItemListInTrack("video", 1) or []
        check("both montage-shaped rows landed", len(placed) == 2, f"{len(placed)}")
        if len(placed) != 2:
            return 1
        for clip_fps, item in zip((on_fps, off_fps), placed):
            dur = int(item.GetDuration())
            check(f"{clip_fps}fps shot occupies the planned {record_len} timeline frames",
                  abs(dur - record_len) <= 1, f"{dur} frames")
        check("the off-rate shot starts exactly where the on-rate one ends",
              int(placed[1].GetStart()) - int(placed[0].GetEnd()) == 0,
              f"gap={int(placed[1].GetStart()) - int(placed[0].GetEnd())}")

        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
            restored = bool(previous_project and pm.LoadProject(previous_project))
            if not restored:
                pm.CreateProject(previous_project or "Untitled Project")
            pm.DeleteProject(PILOT)
        except Exception as exc:
            print(f"cleanup: {type(exc).__name__}: {exc}")


def main() -> int:
    import src.server as s
    code = run(s)
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"{passed}/{len(CHECKS)} checks passed")
    return code


if __name__ == "__main__":
    from tests.preflight import gate
    gate("idle")
    sys.exit(main())
