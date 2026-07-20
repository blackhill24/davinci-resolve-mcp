#!/usr/bin/env python3
"""Live export-diff ground-truth harness for per-clip audio CHANNEL FORMAT
(issue #22, 3.2.3 — Clip Attributes > Audio, mono<->stereo).

Same method as the volume (issue #14) and pan (3.2.1) probes, but the value under
test is a *source-clip* attribute (MediaPoolItem Clip Attributes > Audio > Format
/ channel mapping), not a timeline-item property. So this probe exports BOTH the
project (.drp — where media-pool-item attributes live) and the timeline (.drt),
and diffs each: if the format override round-trips anywhere our file surgery can
author, it shows up in one of the two deltas.

Phases (human/agent does the manual Clip-Attributes edit between setup and diff):

  setup    - disposable project + a STEREO audio clip in the media pool, dump the
             clip's full GetClipProperty() vocabulary, export baseline .drp+.drt,
             leave project open+current.
  diff     - re-export .drp+.drt (after the manual edit) and diff each vs baseline.
  cleanup  - delete disposable project, restore previous current project.

Run: .venv/bin/python tests/live_channel_format_probe.py setup
     ... (manual Clip Attributes > Audio format change in Resolve GUI) ...
     .venv/bin/python tests/live_channel_format_probe.py diff
     .venv/bin/python tests/live_channel_format_probe.py cleanup
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

STATE_FILE = os.path.join(tempfile.gettempdir(), "drm-chanfmt-probe-state.json")
_DNXHR = ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]


def synth_video(media_dir: str, name: str, duration: float) -> str:
    out = os.path.join(media_dir, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size=1280x720:rate=24",
        *_DNXHR, "-an", out,
    ], check=True, capture_output=True)
    return out


def synth_stereo(media_dir: str, name: str, duration: float) -> str:
    """A genuine 2-channel wav: L=220Hz, R=440Hz (distinct channels)."""
    out = os.path.join(media_dir, f"{name}.wav")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]",
        "-map", "[a]", "-c:a", "pcm_s16le", out,
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


def _export_drp(pm, name: str, path: str) -> str:
    ok = pm.ExportProject(name, path, False)
    if not ok:
        raise RuntimeError(f"drp export failed for {name}")
    return path


def _audio_props(item):
    props = item.GetClipProperty() or {}
    keys = sorted(k for k in props
                  if any(t in k.lower()
                         for t in ("audio", "chan", "format", "track", "sync")))
    return props, {k: props.get(k) for k in keys}


def phase_setup(s) -> int:
    probe_name = f"chanfmt_probe_{time.strftime('%H%M%S')}"
    media_dir = tempfile.mkdtemp(prefix="drm-chanfmt-media-")
    scratch = tempfile.mkdtemp(prefix="drm-chanfmt-out-")

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — exit 2")
        return 2
    pm = r.GetProjectManager()
    previous = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None

    video = synth_video(media_dir, "pic", 8.0)
    stereo = synth_stereo(media_dir, "stereo_bed", 8.0)

    proj = pm.CreateProject(probe_name)
    if proj is None:
        print("could not create disposable project — exit 1")
        return 1

    mp = proj.GetMediaPool()
    clips = mp.ImportMedia([video, stereo]) or []
    if len(clips) < 2:
        print(f"import failed (got {len(clips)} clips) — exit 1")
        return 1
    vid_item = next((c for c in clips if "pic" in (c.GetName() or "")), clips[0])
    aud_item = next((c for c in clips if "stereo" in (c.GetName() or "")), clips[-1])

    all_props, aud_view = _audio_props(aud_item)
    print(f"\n=== stereo clip '{aud_item.GetName()}' audio-ish props ===")
    print(json.dumps(aud_view, indent=2))
    print(f"(total {len(all_props)} clip properties)\n")

    # Put it on a timeline too, so the .drt carries the clip reference.
    tl = mp.CreateTimelineFromClips("chanfmt_tl", [vid_item])
    if tl is None:
        print("timeline create failed — exit 1")
        return 1
    proj.SetCurrentTimeline(tl)
    tl.AddTrack("audio")
    start = int(tl.GetStartFrame() if hasattr(tl, "GetStartFrame") else 0)
    mp.AppendToTimeline([{
        "mediaPoolItem": aud_item, "startFrame": 0, "endFrame": 191,
        "trackIndex": 2, "mediaType": 2, "recordFrame": start,
    }])

    base_drt = _export_drt(s, tl, os.path.join(scratch, "baseline.drt"))
    base_drp = _export_drp(pm, probe_name, os.path.join(scratch, "baseline.drp"))
    print(f"  baseline .drt: {base_drt}")
    print(f"  baseline .drp: {base_drp}")

    with open(STATE_FILE, "w") as fh:
        json.dump({
            "probe_name": probe_name, "previous": previous, "scratch": scratch,
            "base_drt": base_drt, "base_drp": base_drp, "media_dir": media_dir,
        }, fh)

    print(f"\nSetup done. Project '{probe_name}' current in Resolve.")
    print("Stereo clip 'stereo_bed.wav' is in the media pool (and on A2).")
    print("Now in the GUI: right-click the clip in the Media Pool > Clip "
          "Attributes > Audio tab, change the Format (e.g. Stereo -> Mono, or "
          "remap channels), OK. Then run:")
    print("  .venv/bin/python tests/live_channel_format_probe.py diff")
    return 0


def _diff_report(label, base, variant, name_filter):
    delta = drt_diff.diff_containers(base, variant, name_filter=name_filter)
    print(f"\n===== {label} EXPORT-DIFF ({name_filter or 'ALL'}) — "
          f"{delta.get('summary')} =====")
    signal = []
    for change in delta.get("changed") or []:
        if change.get("kind") != "text":
            continue
        sig = drt_diff.significant_lines(change)
        if sig["added"] or sig["removed"]:
            signal.append({"entry": change["name"], **sig})
    print(json.dumps({
        "added_entries": delta.get("added"),
        "removed_entries": delta.get("removed"),
        "significant": signal,
    }, indent=2)[:12000])
    print("=" * 42)
    return signal


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
        print(f"current project is not the probe project "
              f"({state['probe_name']}) — exit 1")
        return 1
    tl = proj.GetCurrentTimeline()

    var_drt = _export_drt(s, tl, os.path.join(state["scratch"], "variant.drt"))
    var_drp = _export_drp(pm, state["probe_name"],
                          os.path.join(state["scratch"], "variant.drp"))

    sig_drt = _diff_report("DRT", state["base_drt"], var_drt, None)
    sig_drp = _diff_report("DRP", state["base_drp"], var_drp, None)

    if not sig_drt and not sig_drp:
        print("\nNO significant delta in EITHER .drt or .drp — the channel-format "
              "edit did not land in any exported container (DB-internal only).")
    else:
        print("\nSIGNAL FOUND — inspect lines above for the channel-format encoding.")
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
