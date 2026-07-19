#!/usr/bin/env python3
"""Live export-diff ground-truth harness for auto_edit ducking Tier-2 (issue #14).

Tier-2 ducking writes real volume automation into the exported ``.drt`` instead
of rendering a derivative bed. The encoding for clip volume/automation is NOT yet
known — it has to be *reverse-engineered* by the codec's documented export-diff /
ground-truth method: put a music clip on A2, export a baseline ``.drt``, change
the clip's Volume in Resolve, export again, and diff the two archives to see
exactly which bytes carry the level. This script is that experiment, made
turnkey: it needs a running Resolve Studio to GENERATE the ground truth, so it
aborts cleanly (exit 2) when Resolve isn't up.

It does NOT invent api_truth entries — it PRINTS the diff for a human to inspect
and record, because a verified-on-Resolve claim must come from a real run.

RESOLVED (Resolve 21.0.2.4): the ground truth was captured via this method with
the level edit made by hand in the Inspector (SetProperty('Volume') is a no-op, so
this script's own auto-edit can't move it — it will correctly report "no delta").
The encoding is recorded in ``api_truth`` (issue #14): a non-unity clip level
writes an ``<EffectFiltersBA>`` audio-volume filter blob whose payload carries the
dB value as a little-endian float64, plus a ``2001`` flag in the clip ``<FieldsBlob>``.
Note the export also churns ``<SubType>`` (garbage int) — now stripped by
``drt_diff.significant_lines`` so it no longer reads as a false edit.

What it does when Resolve IS up:
  1. disposable project + synthetic DNxHR/PCM media (never touches sources)
  2. timeline with the music clip positioned on A2 (AddTrack first — issue #12
     probe 6: a positioned A2 append is silent without it)
  3. export baseline .drt  (Volume unchanged)
  4. set the A2 clip's Volume property to a distinct value
  5. export automated .drt
  6. drt_diff.diff_containers(baseline, automated, name_filter="SeqContainer")
     → the changed SeqContainer XML is the candidate volume encoding
  7. restore the prior project + delete the disposable one (best effort)

Run: .venv/bin/python tests/live_auto_edit_ducking_probe.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import drt_diff  # noqa: E402

PROBE = f"duck_probe_{time.strftime('%H%M%S')}"
MEDIA_DIR = tempfile.mkdtemp(prefix="drm-duck-media-")
SCRATCH = tempfile.mkdtemp(prefix="drm-duck-drt-")

# Linux Resolve cannot decode libx264/AAC; synth media must be pro intermediates.
_DNXHR = ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]

# A distinct, unmistakable level so it stands out in the diff. Resolve's item
# Volume is a linear gain (1.0 = 0 dB); 0.25 ≈ -12 dB.
DUCK_VOLUME = 0.25


def synth_video(name: str, *, duration: float) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size=1280x720:rate=24",
        *_DNXHR, "-an", out,
    ], check=True, capture_output=True)
    return out


def synth_music(name: str, *, duration: float) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=220:duration={duration}",
        "-c:a", "pcm_s16le", "-vn", out,
    ], check=True, capture_output=True)
    return out


def _export_drt(s, tl, label: str) -> str:
    path = os.path.join(SCRATCH, f"{label}.drt")
    result = s._export_timeline_checked(tl, {
        "path": path, "format": "drt",
        "require_temp_path": False, "background": False, "async_job": False,
    })
    if not result.get("success"):
        raise RuntimeError(f"drt export ({label}) failed: {result.get('error')}")
    return result.get("primary_file") or path


def run(s) -> int:
    video = synth_video("pic", duration=6.0)
    music = synth_music("bed", duration=6.0)

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting (this harness needs a live "
              "Resolve Studio to generate the ground-truth diff). exit 2")
        return 2

    pm = r.GetProjectManager()
    previous = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    proj = pm.CreateProject(PROBE)
    if proj is None:
        print("could not create disposable project — exit 1")
        return 1

    try:
        mp = proj.GetMediaPool()
        clips = mp.ImportMedia([video, music]) or []
        if len(clips) < 2:
            print(f"import failed (got {len(clips)} clips) — exit 1")
            return 1
        vid_item = next((c for c in clips if "pic" in (c.GetName() or "")), clips[0])
        mus_item = next((c for c in clips if "bed" in (c.GetName() or "")), clips[-1])

        tl = mp.CreateTimelineFromClips("duck_probe_tl", [vid_item])
        if tl is None:
            print("timeline create failed — exit 1")
            return 1
        proj.SetCurrentTimeline(tl)
        # A2 positioned append is silent without an existing track (#12 probe 6).
        tl.AddTrack("audio")
        start = int(tl.GetStartFrame() if hasattr(tl, "GetStartFrame") else 0)
        appended = mp.AppendToTimeline([{
            "mediaPoolItem": mus_item, "startFrame": 0, "endFrame": 143,
            "trackIndex": 2, "mediaType": 2, "recordFrame": start,
        }])
        print(f"  A2 append -> {appended}")

        baseline = _export_drt(s, tl, "baseline")
        print(f"  baseline .drt: {baseline}")

        # Change the music clip's Volume on A2 — the single edit we diff for.
        a2 = tl.GetItemListInTrack("audio", 2) or []
        if not a2:
            print("no clip on A2 after append — exit 1 (see #12 probe 6)")
            return 1
        set_ok = bool(a2[0].SetProperty("Volume", DUCK_VOLUME))
        print(f"  set A2 Volume={DUCK_VOLUME} -> {set_ok}")
        if not set_ok:
            # Live finding (Resolve 21): a flat clip-Volume SetProperty is a no-op
            # on this audio item type (cf. server.py:725). Tier-2 ground truth then
            # needs keyframed automation — set the level in the Fairlight mixer /
            # via a level keyframe by hand, then re-run, OR drive it once the
            # automation API route is known. The export-diff below is still emitted
            # (it should read "unchanged" here, proving the pairing works).
            print("  [note] Volume write did not apply — see harness docstring; "
                  "the diff below will show no timeline delta until a level edit "
                  "actually lands.")

        automated = _export_drt(s, tl, "automated")
        print(f"  automated .drt: {automated}")

        delta = drt_diff.diff_containers(
            baseline, automated, name_filter="SeqContainer")
        print(f"\n===== DRT EXPORT-DIFF (SeqContainer) — {delta.get('summary')} =====")

        changed = delta.get("changed") or []
        # Each .drt re-export regenerates DbId/Sequence uuids (verified live), so
        # the raw diff is noisy — surface only churn-filtered lines: those are the
        # real edit (the clip-volume field) if it landed.
        signal = []
        for change in changed:
            if change.get("kind") != "text":
                continue
            sig = drt_diff.significant_lines(change)
            if sig["added"] or sig["removed"]:
                signal.append({"entry": change["name"], **sig})
        print(json.dumps({"changed_entries": [c["name"] for c in changed],
                          "significant": signal}, indent=2)[:6000])
        print("==========================================\n")

        if not signal:
            print("NO significant SeqContainer delta — the two exports differ only "
                  "by regenerated ids, so the level edit did NOT land in the .drt "
                  f"(SetProperty('Volume')={DUCK_VOLUME} is a no-op on this item "
                  "type). Set the level via keyframed Fairlight automation, then "
                  "re-run. Nothing to record yet.")
            return 0
        print("SIGNAL FOUND: the lines above carry the level edit — that field is "
              "the clip-volume encoding. Record the confirmed finding in "
              "src/utils/api_truth.py (issue=14), then implement the Tier-2 drt "
              "writer + set music.ducking.mode=drt_automation.")
        return 0
    finally:
        try:
            if previous:
                pm.LoadProject(previous)
            pm.DeleteProject(PROBE)
        except Exception as exc:
            print(f"cleanup: {type(exc).__name__}: {exc}")
        import shutil
        shutil.rmtree(MEDIA_DIR, ignore_errors=True)


def main() -> int:
    import src.server as s
    return run(s)


if __name__ == "__main__":
    sys.exit(main())
