#!/usr/bin/env python3
"""Live export-diff + round-trip harness for SUBTITLE text + timing
(issue #22, 3.2.4 — per-subtitle text/timing read/write).

The live scripting API cannot read or set subtitle text/timing at all (a
TimelineItem on a subtitle track exposes only the 21 transform/composite
properties; item.GetName() sometimes surfaces the text but there is no setter).
See the three subtitle entries in docs/reference/api-limitations.md. The only
possible workaround is file surgery: export a container, edit the cue, reimport.

Two containers can plausibly carry subtitles:
  - .drt  — Resolve's native timeline archive (most likely to round-trip)
  - .fcpxml — FCPXML 1.10 <caption> elements

PRE-VERIFIED (this file's investigation, 2026-07-20, Resolve 21.0.2.4): hand
-authored FCPXML <caption> elements do NOT import as subtitle tracks — 6
variants (roles iTT/Subtitle/captions/cea608 x nested-in-clip and spine-level)
all reimported with GetTrackCount("subtitle") == 0. So blind FCPXML authoring is
out; the open question is whether a *Resolve-authored* subtitle round-trips
through .drt (and, secondarily, whether Resolve emits captions into .fcpxml at
all — the read path).

Phases (human/agent adds the subtitles in the GUI between setup and diff):

  setup    - disposable project + a video clip + timeline, export baseline
             .drt + .fcpxml, leave project open+current. Then the operator adds
             a subtitle track with 2 distinct cues (text + timing) in the GUI.
  diff     - re-export .drt + .fcpxml and diff each vs baseline; also dump the
             raw subtitle-bearing regions so the encoding is visible.
  roundtrip- (optional, run after diff) re-import the just-exported .drt and read
             the subtitle track back via the live API to confirm reimport keeps
             the cues; proves the export->edit->reimport path is viable at all.
  cleanup  - delete disposable project, restore previous current project.

Run: .venv/bin/python tests/live_subtitle_probe.py setup
     ... (manual: add subtitle track + 2 cues in Resolve GUI) ...
     .venv/bin/python tests/live_subtitle_probe.py diff
     .venv/bin/python tests/live_subtitle_probe.py roundtrip
     .venv/bin/python tests/live_subtitle_probe.py cleanup

Env: RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB / PYTHONPATH must point at the
Resolve scripting install (pytest is not in .venv; run directly).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.domains.timeline_conform_interchange.utils import drt_diff  # noqa: E402

STATE_FILE = os.path.join(tempfile.gettempdir(), "drm-subtitle-probe-state.json")
_DNXHR = ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]


def synth_video(media_dir: str, name: str, duration: float) -> str:
    out = os.path.join(media_dir, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size=1280x720:rate=24",
        *_DNXHR, "-an", out,
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


def _export_fcpxml(s, tl, path: str) -> str:
    result = s._export_timeline_checked(tl, {
        "path": path, "format": "fcpxml",
        "require_temp_path": False, "background": False, "async_job": False,
    })
    if not result.get("success"):
        raise RuntimeError(f"fcpxml export failed: {result.get('error')}")
    return result.get("primary_file") or path


def phase_setup(s) -> int:
    probe_name = f"subtitle_probe_{time.strftime('%H%M%S')}"
    media_dir = tempfile.mkdtemp(prefix="drm-sub-media-")
    scratch = tempfile.mkdtemp(prefix="drm-sub-out-")

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — exit 2")
        return 2
    pm = r.GetProjectManager()
    previous = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None

    video = synth_video(media_dir, "pic", 12.0)

    proj = pm.CreateProject(probe_name)
    if proj is None:
        print("could not create disposable project — exit 1")
        return 1

    mp = proj.GetMediaPool()
    clips = mp.ImportMedia([video]) or []
    if not clips:
        print("import failed — exit 1")
        return 1
    tl = mp.CreateTimelineFromClips("subtitle_tl", clips)
    if tl is None:
        print("timeline create failed — exit 1")
        return 1
    proj.SetCurrentTimeline(tl)

    base_drt = _export_drt(s, tl, os.path.join(scratch, "baseline.drt"))
    base_fcp = _export_fcpxml(s, tl, os.path.join(scratch, "baseline.fcpxml"))
    print(f"  baseline .drt:    {base_drt}")
    print(f"  baseline .fcpxml: {base_fcp}")

    with open(STATE_FILE, "w") as fh:
        json.dump({
            "probe_name": probe_name, "previous": previous, "scratch": scratch,
            "base_drt": base_drt, "base_fcp": base_fcp, "media_dir": media_dir,
        }, fh)

    print(f"\nSetup done. Project '{probe_name}' current in Resolve.")
    print("Now in the GUI:")
    print("  1. Timeline menu (or right-click the track header) > Add Subtitle Track.")
    print("  2. Move the playhead ~1s in, click the '+' to add a subtitle, type a")
    print("     DISTINCT phrase, e.g. 'HELLO ALPHA CUE'. Set its duration ~2s.")
    print("  3. Add a second subtitle ~5s in: 'SECOND BRAVO CUE', duration ~1s.")
    print("Then run:")
    print("  .venv/bin/python tests/live_subtitle_probe.py diff")
    return 0


def _dump_subtitle_regions(container: str, keyword_hits=("subtitle", "caption",
                           "HELLO ALPHA", "SECOND BRAVO", "text")):
    """Best-effort raw peek: print entries whose name/body mentions subtitles."""
    try:
        entries = drt_diff._read_entries(container)
    except Exception as exc:  # pragma: no cover - diagnostic only
        print(f"  (could not load {container}: {exc})")
        return
    for name, body in entries.items():
        try:
            text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        except Exception:
            text = ""
        low = (name + "\n" + text).lower()
        if any(k.lower() in low for k in keyword_hits):
            snippet = text[:1500] if text.strip() else "(binary/opaque)"
            print(f"\n  --- entry {name} ---\n{snippet}")


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

    print(f"  live subtitle track count: {tl.GetTrackCount('subtitle')}")

    var_drt = _export_drt(s, tl, os.path.join(state["scratch"], "variant.drt"))
    var_fcp = _export_fcpxml(s, tl, os.path.join(state["scratch"], "variant.fcpxml"))

    for label, base, variant in (("DRT", state["base_drt"], var_drt),
                                 ("FCPXML", state["base_fcp"], var_fcp)):
        try:
            delta = drt_diff.diff_containers(base, variant, name_filter=None)
            print(f"\n===== {label} EXPORT-DIFF — {delta.get('summary')} =====")
            print(json.dumps({
                "added_entries": delta.get("added"),
                "removed_entries": delta.get("removed"),
                "changed_entries": [c.get("name") for c in delta.get("changed") or []],
            }, indent=2)[:4000])
        except Exception as exc:
            print(f"\n===== {label} diff failed: {exc} =====")
        print(f"  --- {label} raw subtitle-bearing regions ---")
        _dump_subtitle_regions(variant)

    print("\nIf the cue text ('HELLO ALPHA CUE' / 'SECOND BRAVO CUE') appears in a")
    print("readable region above, that container carries subtitles and is editable.")
    print("Next: tests/live_subtitle_probe.py roundtrip")
    return 0


def phase_roundtrip(s) -> int:
    """Reimport the exported .drt unchanged and confirm the cues survive."""
    with open(STATE_FILE) as fh:
        state = json.load(fh)
    r = s.get_resolve()
    if r is None:
        print("Resolve not available — exit 2")
        return 2
    pm = r.GetProjectManager()
    proj = pm.GetCurrentProject()
    mp = proj.GetMediaPool()
    var_drt = os.path.join(state["scratch"], "variant.drt")
    if not os.path.exists(var_drt):
        print("run `diff` first (need variant.drt) — exit 1")
        return 1
    opts = {"timelineName": f"sub_rt_{time.strftime('%H%M%S')}",
            "importSourceClips": False}
    tl = mp.ImportTimelineFromFile(var_drt, opts)
    if tl is None:
        print("reimport failed — exit 1")
        return 1
    proj.SetCurrentTimeline(tl)
    tr = s._timeline_transcript(tl, with_timecodes=True)
    print(f"reimported '{tl.GetName()}' subtitle tracks: "
          f"{tl.GetTrackCount('subtitle')}")
    print("transcript:", json.dumps(tr, indent=2))
    if tr.get("cue_count"):
        print("\nROUND-TRIP OK — .drt export->reimport preserves subtitle cues; "
              "editing the cue text/timing in the .drt is a viable workaround.")
    else:
        print("\nNO cues after reimport — .drt does not round-trip subtitles.")
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
    dispatch = {
        "setup": phase_setup, "diff": phase_diff,
        "roundtrip": phase_roundtrip, "cleanup": phase_cleanup,
    }
    fn = dispatch.get(phase)
    if fn is None:
        print(f"unknown phase {phase!r} (setup|diff|roundtrip|cleanup)")
        return 1
    return fn(s)


if __name__ == "__main__":
    from tests.preflight import gate
    gate("open")
    sys.exit(main())
