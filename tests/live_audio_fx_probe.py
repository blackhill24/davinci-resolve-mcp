#!/usr/bin/env python3
"""Live export-diff ground-truth harness for FairlightFX/EQ/automation (issue
#22, 3.2.2). Same method as clip volume (#14) and clip pan (3.2.1): the API has
no write path for any of these (see the "Fairlight audio levels / pan / EQ /
automation / FairlightFX" api_truth entry), so the encoding has to be recovered
by hand-editing in the Fairlight/Edit Inspector and diffing two .drt exports.

Split into phases so a human/agent can do the manual GUI edit between them while
Resolve stays up and the disposable project stays current:

  setup    - disposable project + audio clip on A2, export baseline .drt, leave
             project open and current.
  diff     - export the (by-then hand-edited) timeline again, diff against the
             baseline, print the significant delta.
  cleanup  - delete the disposable project, restore whatever was current before.

Run once per experiment (add an EQ, export/diff/cleanup; then re-run setup for
a Compressor, etc.) — each run is a fresh disposable project so experiments
don't contaminate each other.

Run: .venv/bin/python tests/live_audio_fx_probe.py setup
     ... (manual FX edit in Resolve GUI) ...
     .venv/bin/python tests/live_audio_fx_probe.py diff
     .venv/bin/python tests/live_audio_fx_probe.py cleanup
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

STATE_FILE = os.path.join(tempfile.gettempdir(), "drm-audio-fx-probe-state.json")
_DNXHR = ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]


def synth_video(media_dir: str, name: str, duration: float) -> str:
    out = os.path.join(media_dir, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size=1280x720:rate=24",
        *_DNXHR, "-an", out,
    ], check=True, capture_output=True)
    return out


def synth_music(media_dir: str, name: str, duration: float) -> str:
    out = os.path.join(media_dir, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=220:duration={duration}",
        "-c:a", "pcm_s16le", "-vn", out,
    ], check=True, capture_output=True)
    return out


def _export_drt(s, tl, path: str) -> str:
    result = s._export_timeline_checked(tl, {
        "path": path, "format": "drt",
        "require_temp_path": False, "background": False, "async_job": False,
    })
    if not result.get("success"):
        raise RuntimeError(f"drt export failed: {result.get('error')}")
    return result.get("primary_file") or path


def phase_setup(s) -> int:
    probe_name = f"fx_probe_{time.strftime('%H%M%S')}"
    media_dir = tempfile.mkdtemp(prefix="drm-fx-media-")
    scratch = tempfile.mkdtemp(prefix="drm-fx-drt-")

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — exit 2")
        return 2
    pm = r.GetProjectManager()
    previous = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None

    video = synth_video(media_dir, "pic", 8.0)
    music = synth_music(media_dir, "bed", 8.0)

    proj = pm.CreateProject(probe_name)
    if proj is None:
        print("could not create disposable project — exit 1")
        return 1

    mp = proj.GetMediaPool()
    clips = mp.ImportMedia([video, music]) or []
    if len(clips) < 2:
        print(f"import failed (got {len(clips)} clips) — exit 1")
        return 1
    vid_item = next((c for c in clips if "pic" in (c.GetName() or "")), clips[0])
    mus_item = next((c for c in clips if "bed" in (c.GetName() or "")), clips[-1])

    tl = mp.CreateTimelineFromClips("fx_probe_tl", [vid_item])
    if tl is None:
        print("timeline create failed — exit 1")
        return 1
    proj.SetCurrentTimeline(tl)
    tl.AddTrack("audio")
    start = int(tl.GetStartFrame() if hasattr(tl, "GetStartFrame") else 0)
    appended = mp.AppendToTimeline([{
        "mediaPoolItem": mus_item, "startFrame": 0, "endFrame": 191,
        "trackIndex": 2, "mediaType": 2, "recordFrame": start,
    }])
    print(f"  A2 append -> {appended}")

    baseline_path = os.path.join(scratch, "baseline.drt")
    baseline = _export_drt(s, tl, baseline_path)
    print(f"  baseline .drt: {baseline}")

    state = {
        "probe_name": probe_name, "previous": previous,
        "scratch": scratch, "baseline": baseline,
        "media_dir": media_dir,
    }
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)

    print(f"\nSetup done. Project '{probe_name}' is current in Resolve.")
    print("Audio clip is on A2 (track 2), clip index 0, 'bed.mov'.")
    print("Now: in the Resolve GUI, apply the FX/automation edit under test to "
          "that clip (e.g. drag an Audio FX from the Effects Library onto it in "
          "the Fairlight page, or add a keyframed automation point), then run:")
    print("  .venv/bin/python tests/live_audio_fx_probe.py diff")
    return 0


def phase_diff(s) -> int:
    with open(STATE_FILE) as fh:
        state = json.load(fh)
    r = s.get_resolve()
    if r is None:
        print("Resolve not available — exit 2")
        return 2
    pm = r.GetProjectManager()
    proj = pm.GetCurrentProject()
    if not proj or proj.GetName() != state["probe_name"]:
        print(f"current project is not the probe project ({state['probe_name']}) — exit 1")
        return 1
    tl = proj.GetCurrentTimeline()
    if not tl:
        print("no current timeline — exit 1")
        return 1

    automated_path = os.path.join(state["scratch"], f"edited_{int(time.time())}.drt")
    automated = _export_drt(s, tl, automated_path)
    print(f"  edited .drt: {automated}")

    delta = drt_diff.diff_containers(state["baseline"], automated, name_filter="SeqContainer")
    print(f"\n===== DRT EXPORT-DIFF (SeqContainer) — {delta.get('summary')} =====")
    changed = delta.get("changed") or []
    signal = []
    for change in changed:
        if change.get("kind") != "text":
            continue
        sig = drt_diff.significant_lines(change)
        if sig["added"] or sig["removed"]:
            signal.append({"entry": change["name"], **sig})
    print(json.dumps({"changed_entries": [c["name"] for c in changed],
                      "significant": signal}, indent=2)[:12000])
    print("==========================================\n")
    if not signal:
        print("NO significant delta — the edit did not land in the .drt "
              "(or the project-level DB is where it lives instead — check "
              "FLStudioModelBA via the fairlight tool's read_buses_from_db).")
        return 0
    print("SIGNAL FOUND — inspect the lines above for the encoding.")
    return 0


def phase_cleanup(s) -> int:
    with open(STATE_FILE) as fh:
        state = json.load(fh)
    r = s.get_resolve()
    if r is None:
        print("Resolve not available — exit 2")
        return 2
    pm = r.GetProjectManager()
    try:
        if state.get("previous"):
            pm.LoadProject(state["previous"])
        pm.DeleteProject(state["probe_name"])
        print(f"deleted project {state['probe_name']}")
    finally:
        import shutil
        shutil.rmtree(state["media_dir"], ignore_errors=True)
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass
    return 0


def main() -> int:
    import src.server as s
    phase = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if phase == "setup":
        return phase_setup(s)
    if phase == "diff":
        return phase_diff(s)
    if phase == "cleanup":
        return phase_cleanup(s)
    print(f"unknown phase {phase!r} (setup|diff|cleanup)")
    return 1


if __name__ == "__main__":
    from preflight import gate
    gate("open")
    sys.exit(main())
