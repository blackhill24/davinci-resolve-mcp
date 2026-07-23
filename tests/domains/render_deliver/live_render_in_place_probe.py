#!/usr/bin/env python3
"""Live validation for 3.1.6 (issue #30): Render in Place.

`timeline render_in_place` composes the missing API feature live: single-clip
render of the clip's record range (MarkIn/MarkOut) into a REAL media dir,
import, then replace the original clip at the same position/duration.

Renders video-only by default (ExportAudio=False — dodges the headless
Fairlight/ALSA 0%-stall, see #28), so this is safe to run headless. The render
target is a real dir under ~/Videos (the queue refuses the system temp dir).

Run: .venv/bin/python tests/live_render_in_place_probe.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

PILOT = f"render_in_place_probe_{time.strftime('%H%M%S')}"
MEDIA_DIR = tempfile.mkdtemp(prefix="drm-rip-media-")
# Render target must be a REAL media dir; keep it disposable + obviously ours.
RENDER_DIR = os.path.expanduser(f"~/Videos/_mcp_rip_probe_{int(time.time())}")
CHECKS: list[tuple[str, bool, str]] = []

CLIP_FRAMES = 72  # 3s @ 24fps


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def synth_clip(name: str, color: str) -> str:
    out = os.path.join(MEDIA_DIR, f"{name}.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c={color}:s=640x360:r=24:d={CLIP_FRAMES / 24.0}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        out,
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

    os.makedirs(RENDER_DIR, exist_ok=True)
    clip_paths = [synth_clip(n, c) for n, c in (("ripA", "red"), ("ripB", "green"))]

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

        tl = mp.CreateEmptyTimeline("RIP Base")
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
        id_a = item_a.GetUniqueId()
        start_a, dur_a = int(item_a.GetStart()), int(item_a.GetDuration())
        start_b, dur_b = int(item_b.GetStart()), int(item_b.GetDuration())

        # ---- refusals ----
        refused = _run_gated(s, "render_in_place", {"clip_id": id_a})
        check("target_dir required", "error" in refused, str(refused.get("error") or ""))
        tmp_target = tempfile.mkdtemp(prefix="rip-refuse-")
        refused = _run_gated(s, "render_in_place", {"clip_id": id_a, "target_dir": tmp_target})
        check("system-temp target refused", "error" in refused, str(refused.get("error") or ""))
        shutil.rmtree(tmp_target, ignore_errors=True)

        # An overlapping clip on V2 — isolate=True (default) must disable V2 for
        # the render and restore it after.
        tl.AddTrack("video")
        mp.AppendToTimeline([{
            "mediaPoolItem": imported[1], "startFrame": 0, "endFrame": CLIP_FRAMES,
            "trackIndex": 2, "recordFrame": start_a,
        }])
        v2_before = bool(tl.GetIsTrackEnabled("video", 2))
        check("overlap clip placed on V2", len(tl.GetItemListInTrack("video", 2) or []) == 1)

        # ---- render in place ----
        result = _run_gated(s, "render_in_place", {
            "clip_id": id_a, "target_dir": RENDER_DIR, "timeout_s": 240})
        check("render_in_place", bool(result.get("success")), str(result.get("error") or ""))
        if result.get("success"):
            rendered = result.get("rendered_file") or ""
            check("rendered file exists", os.path.isfile(rendered), rendered)
            check("job completed", result.get("job_status") == "Complete",
                  str(result.get("job_status")))

            items_after = tl.GetItemListInTrack("video", 1) or []
            check("still 2 clips on V1", len(items_after) == 2, f"count={len(items_after)}")
            new_first = next((it for it in items_after if int(it.GetStart()) == start_a), None)
            check("replacement at the same position", new_first is not None)
            if new_first is not None:
                check("replacement keeps the duration",
                      int(new_first.GetDuration()) == dur_a,
                      f"duration={new_first.GetDuration()} expected={dur_a}")
                check("replacement is the rendered media",
                      "_RIP_" in str(new_first.GetName() or ""), str(new_first.GetName()))
                check("new_clip_id returned + matches",
                      result.get("new_clip_id") == new_first.GetUniqueId(),
                      str(result.get("new_clip_id")))
            untouched_b = next((it for it in items_after if int(it.GetStart()) == start_b), None)
            check("neighbor clip untouched",
                  untouched_b is not None and int(untouched_b.GetDuration()) == dur_b)
            check("render queue left clean", not (proj.GetRenderJobList() or []),
                  f"jobs={len(proj.GetRenderJobList() or [])}")
            check("V2 isolated during the render", result.get("isolated_tracks") == [2],
                  str(result.get("isolated_tracks")))
            check("no composite warning with isolation", "warning" not in result,
                  str(result.get("warning") or ""))
            check("V2 enable state restored",
                  bool(tl.GetIsTrackEnabled("video", 2)) == v2_before,
                  f"before={v2_before} after={tl.GetIsTrackEnabled('video', 2)}")

    finally:
        try:
            delete_project_safely(pm, PILOT)
            print(f"Deleted disposable project: {PILOT}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup warning: {exc}")
        shutil.rmtree(MEDIA_DIR, ignore_errors=True)
        shutil.rmtree(RENDER_DIR, ignore_errors=True)

    failures = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    from preflight import gate
    gate("idle")
    raise SystemExit(main())
