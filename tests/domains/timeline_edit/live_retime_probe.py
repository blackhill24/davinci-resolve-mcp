#!/usr/bin/env python3
"""Live validation for 3.1.5 (issue #30): clip speed / retime + fit-to-fill.

Exercises the new `timeline` actions on a running Resolve 21 Studio:
  - set_clip_speed 0.5x  -> record duration doubles on the NEW "(edited)" timeline
  - set_clip_speed ramp  -> variable-speed keyframes land (explicit new_duration)
  - fit_to_fill_edit     -> clip fills exactly target_duration frames
  - slip_clip retreat    -> covered in live_timeline_edit_gap_workarounds.py

All are drt-surgery actions: the ORIGINAL timeline must stay untouched, results
land on a NEW "<name> (edited)" timeline. Disposable project + synthetic ffmpeg
media only; no rendering, so no ALSA/render-hang risk.

Run: .venv/bin/python tests/live_retime_probe.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

PILOT = f"retime_probe_{time.strftime('%H%M%S')}"
MEDIA_DIR = tempfile.mkdtemp(prefix="drm-retime-media-")
CHECKS: list[tuple[str, bool, str]] = []

CLIP_FRAMES = 144  # 6s @ 24fps


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def synth_clip(name: str, color: str) -> str:
    # A/V clip so AppendToTimeline creates a LINKED audio item (needed by the
    # retime_linked_audio check).
    out = os.path.join(MEDIA_DIR, f"{name}.mov")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c={color}:s=640x360:r=24:d={CLIP_FRAMES / 24.0}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={CLIP_FRAMES / 24.0}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le", out,
    ], check=True, capture_output=True)
    return out


def _run_gated(s, action: str, params: dict) -> dict:
    gate = s.timeline(action, params)
    if gate.get("status") == "confirmation_required":
        return s.timeline(action, {**params, "confirm_token": gate.get("confirm_token")})
    return gate


def main() -> int:
    import src.server as s
    from src.domains.project_lifecycle.utils.project_cleanup import delete_project_safely

    if not s._advanced_bridge.node_available():
        print("Node.js not on PATH — retime actions can only refuse; install Node 18+.")

    clip_paths = [synth_clip(n, c) for n, c in (("clipA", "red"), ("clipB", "green"))]

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
        imported = mp.ImportMedia(clip_paths)
        check("media imported", bool(imported) and len(imported) == 2)
        if not imported or len(imported) != 2:
            return 1

        tl = mp.CreateEmptyTimeline("Retime Base")
        check("base timeline created", tl is not None)
        if tl is None:
            return 1
        proj.SetCurrentTimeline(tl)
        appended = mp.AppendToTimeline(list(imported))
        check("clips appended", bool(appended) and len(appended) == 2)

        v_items = tl.GetItemListInTrack("video", 1) or []
        if len(v_items) != 2:
            check("2 clips on V1", False, f"count={len(v_items)}")
            return 1
        item_a, item_b = v_items
        id_a, id_b = item_a.GetUniqueId(), item_b.GetUniqueId()
        dur_a = int(item_a.GetDuration())
        start_b = int(item_b.GetStart())

        # ---- set_clip_speed 0.5x (+ ripple) ----
        slowed = _run_gated(s, "set_clip_speed", {"clip_id": id_a, "speed": 0.5, "ripple": True})
        check("set_clip_speed 0.5x", bool(slowed.get("success")), str(slowed.get("error") or ""))
        if slowed.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, slowed["new_timeline"])
            check("set_clip_speed: new timeline exists", new_tl is not None)
            if new_tl is not None:
                items = new_tl.GetItemListInTrack("video", 1) or []
                d0 = int(items[0].GetDuration()) if items else None
                check("set_clip_speed: duration doubled", d0 == dur_a * 2,
                      f"duration={d0} expected={dur_a * 2}")
                s1 = int(items[1].GetStart()) if len(items) > 1 else None
                check("set_clip_speed: ripple shifted the next clip",
                      s1 == start_b + dur_a, f"start={s1} expected={start_b + dur_a}")
            after = tl.GetItemListInTrack("video", 1) or []
            check("set_clip_speed: original untouched",
                  len(after) == 2 and int(after[0].GetDuration()) == dur_a)

        # ---- set_clip_speed variable ramp (new_duration AUTO-DERIVED from
        # last keyframe record_sec x fps: 4.5s @ 24fps = 108) ----
        kfs = [{"record_sec": 2.0, "source_sec": 1.0},
               {"record_sec": 4.5, "source_sec": 6.0}]
        ramp = _run_gated(s, "set_clip_speed", {"clip_id": id_b, "keyframes": kfs})
        check("set_clip_speed ramp (auto duration)", bool(ramp.get("success")),
              str(ramp.get("error") or ""))
        if ramp.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, ramp["new_timeline"])
            if new_tl is not None:
                items = new_tl.GetItemListInTrack("video", 1) or []
                target = next((it for it in items if int(it.GetStart()) == start_b), None)
                d = int(target.GetDuration()) if target is not None else None
                check("set_clip_speed ramp: auto-derived duration = 108", d == 108,
                      f"duration={d}")

        # ---- retime_linked_audio: video + linked audio shrink together ----
        linked = _run_gated(s, "set_clip_speed", {
            "clip_id": id_a, "speed": 2, "retime_linked_audio": True})
        check("set_clip_speed retime_linked_audio", bool(linked.get("success")),
              str(linked.get("error") or ""))
        if linked.get("success"):
            check("linked audio op count = 1", linked.get("linked_audio_retimed") == 1,
                  str(linked.get("linked_audio_retimed")))
            new_tl, _ = s._find_timeline_by_name(proj, linked["new_timeline"])
            if new_tl is not None:
                v = new_tl.GetItemListInTrack("video", 1) or []
                a = new_tl.GetItemListInTrack("audio", 1) or []
                dv = int(v[0].GetDuration()) if v else None
                da = int(a[0].GetDuration()) if a else None
                check("linked: video halved", dv == dur_a // 2, f"video={dv}")
                check("linked: audio matches video", da == dv, f"audio={da} video={dv}")

        # ---- fit_to_fill_edit ----
        fit = _run_gated(s, "fit_to_fill_edit", {"clip_id": id_a, "target_duration": 100})
        check("fit_to_fill_edit", bool(fit.get("success")), str(fit.get("error") or ""))
        if fit.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, fit["new_timeline"])
            if new_tl is not None:
                items = new_tl.GetItemListInTrack("video", 1) or []
                d0 = int(items[0].GetDuration()) if items else None
                check("fit_to_fill_edit: duration == target", d0 == 100, f"duration={d0}")

        # ---- reset to 1x round-trip ----
        if slowed.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, slowed["new_timeline"])
            if new_tl is not None:
                proj.SetCurrentTimeline(new_tl)
                items = new_tl.GetItemListInTrack("video", 1) or []
                rid = items[0].GetUniqueId() if items else None
                if rid:
                    reset = _run_gated(s, "set_clip_speed", {"clip_id": rid, "speed": 1})
                    check("set_clip_speed reset to 1x", bool(reset.get("success")),
                          str(reset.get("error") or ""))
                    if reset.get("success"):
                        rt, _ = s._find_timeline_by_name(proj, reset["new_timeline"])
                        if rt is not None:
                            ritems = rt.GetItemListInTrack("video", 1) or []
                            d0 = int(ritems[0].GetDuration()) if ritems else None
                            check("reset: duration back to source length", d0 == dur_a,
                                  f"duration={d0} expected={dur_a}")
                proj.SetCurrentTimeline(tl)

    finally:
        try:
            delete_project_safely(pm, PILOT)
            print(f"Deleted disposable project: {PILOT}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup warning: {exc}")
        import shutil
        shutil.rmtree(MEDIA_DIR, ignore_errors=True)

    failures = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    from tests.preflight import gate
    gate("open")
    raise SystemExit(main())
