#!/usr/bin/env python3
"""3.2.7 (issue #30): live sweep of the shipped Stage-3.2 helpers on 21.0.2.4.

The productized 3.2 surface is `timeline set_clip_volume` + `timeline
set_clip_pan` (3.2.1, commit 39e62f0) — 3.2.2/3.2.3 closed as
investigation-only, 3.2.4/3.2.5/3.2.6 are covered by the import_srt probe.

Asserts each action lands on a NEW "(edited)" timeline, the original stays
untouched, and the edited timeline's re-export carries a non-empty
EffectFiltersBA on the target audio clip (the volume/pan automation blob the
API cannot write).

Run: .venv/bin/python tests/live_audio_clip_levels_sweep.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

PILOT = f"audio_levels_sweep_{time.strftime('%H%M%S')}"
WORK = tempfile.mkdtemp(prefix="drm-audio-sweep-")
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def synth_av_clip() -> str:
    out = os.path.join(WORK, "tone.mov")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=24:d=4",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p",
        "-c:a", "pcm_s16le", out,
    ], check=True)
    return out


def _run_gated(s, action: str, params: dict) -> dict:
    gate = s.timeline(action, params)
    if gate.get("status") == "confirmation_required":
        return s.timeline(action, {**params, "confirm_token": gate.get("confirm_token")})
    return gate


def _audio_eff_blobs(s, tl) -> list[str]:
    """Export `tl` and return the EffectFiltersBA payloads of its audio clips."""
    path = os.path.join(WORK, f"check_{int(time.time() * 1000)}.drt")
    exp = s._export_timeline_checked(tl, {
        "path": path, "format": "drt",
        "require_temp_path": False, "background": False, "async_job": False})
    if not exp.get("success"):
        return []
    with zipfile.ZipFile(exp.get("primary_file") or path) as z:
        seq_name = next(n for n in z.namelist()
                        if re.search(r"(^|/)SeqContainer(/|\d*\.xml)", n) and n.endswith(".xml"))
        xml = z.read(seq_name).decode("utf-8")
    si, sj = xml.find("<AudioTrackVec>"), xml.find("</AudioTrackVec>")
    block = xml[si:sj] if si >= 0 else ""
    return re.findall(r"<Sm2TiAudioClip[\s\S]*?<EffectFiltersBA>([0-9A-Fa-f]*)"
                      r"</EffectFiltersBA>", block)


def main() -> int:
    import src.server as s
    from src.domains.project_lifecycle.utils.project_cleanup import delete_project_safely

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting")
        return 2
    pm = r.GetProjectManager()
    proj = pm.CreateProject(PILOT)
    check("disposable project created", proj is not None, PILOT)
    if proj is None:
        return 2

    try:
        mp = proj.GetMediaPool()
        imported = mp.ImportMedia([synth_av_clip()])
        check("A/V media imported", bool(imported))
        tl = mp.CreateEmptyTimeline("Audio Sweep Base")
        check("base timeline created", tl is not None)
        if tl is None:
            return 1
        proj.SetCurrentTimeline(tl)
        mp.AppendToTimeline(list(imported or []))
        a_items = tl.GetItemListInTrack("audio", 1) or []
        check("audio clip on A1", len(a_items) == 1, f"count={len(a_items)}")
        if not a_items:
            return 1
        aid = a_items[0].GetUniqueId()
        baseline_blobs = _audio_eff_blobs(s, tl)
        check("baseline audio EffectFiltersBA empty", all(not b for b in baseline_blobs),
              str([len(b) for b in baseline_blobs]))

        # ---- set_clip_volume ----
        vol = _run_gated(s, "set_clip_volume", {"clip_id": aid, "volume_db": -12})
        check("set_clip_volume", bool(vol.get("success")), str(vol.get("error") or ""))
        if vol.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, vol["new_timeline"])
            check("volume: new timeline exists", new_tl is not None)
            if new_tl is not None:
                blobs = _audio_eff_blobs(s, new_tl)
                check("volume: audio clip carries the automation blob",
                      any(b for b in blobs), str([len(b) for b in blobs]))

        # ---- set_clip_pan ----
        pan = _run_gated(s, "set_clip_pan", {"clip_id": aid, "pan_value": -50})
        check("set_clip_pan", bool(pan.get("success")), str(pan.get("error") or ""))
        if pan.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, pan["new_timeline"])
            check("pan: new timeline exists", new_tl is not None)
            if new_tl is not None:
                blobs = _audio_eff_blobs(s, new_tl)
                check("pan: audio clip carries the automation blob",
                      any(b for b in blobs), str([len(b) for b in blobs]))

        # original untouched throughout
        after_blobs = _audio_eff_blobs(s, tl)
        check("original timeline still clean", all(not b for b in after_blobs),
              str([len(b) for b in after_blobs]))

    finally:
        try:
            delete_project_safely(pm, PILOT)
            print(f"Deleted disposable project: {PILOT}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup warning: {exc}")
        import shutil
        shutil.rmtree(WORK, ignore_errors=True)

    failures = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    from preflight import gate
    gate("open")
    raise SystemExit(main())
