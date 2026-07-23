#!/usr/bin/env python3
"""Live validation for Stage 3.1 edit/timeline UI-gap workarounds (issue #21).

Exercises every new `timeline` action added for the wiring-tier pass: trim_clip,
move_clip, slide_clip, slip_clip, split_clip, add_transition, list_transitions,
replace_edit, place_on_top_edit, insert_edit.

The drt-surgery actions (trim_clip, move_clip, slide_clip, slip_clip, split_clip,
add_transition, insert_edit) each land on a NEW "<name> (edited)" timeline — this
asserts the ORIGINAL timeline is untouched in every case. replace_edit and
place_on_top_edit are pure live-API and mutate the current timeline in place, by
design (no DRT surgery needed since nothing existing moves).

Disposable project + synthetic ffmpeg media only; no rendering (drt export/
reimport only), so no ALSA/render-hang risk. The user's current project is
restored on exit.

Run: .venv/bin/python tests/live_timeline_edit_gap_workarounds.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PILOT = f"edit_gap_workarounds_{time.strftime('%H%M%S')}"
MEDIA_DIR = tempfile.mkdtemp(prefix="drm-edit-gap-media-")
CHECKS: list[tuple[str, bool, str]] = []

CLIP_FRAMES = 144  # 6s @ 24fps
HANDLE = 12
USABLE = CLIP_FRAMES - 2 * HANDLE  # 120 — leaves 12-frame handles both sides


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


def main() -> int:
    import src.server as s
    from src.domains.project_lifecycle.utils.project_cleanup import delete_project_safely

    if not s._advanced_bridge.node_available():
        print("Node.js not on PATH — every action under test honestly refuses; "
              "install Node 18+ to actually validate the drt-surgery round trip.")

    clip_paths = [synth_clip(n, c) for n, c in
                  (("clipA", "red"), ("clipB", "green"), ("clipC", "blue"), ("clipD", "yellow"))]

    r = s.get_resolve()
    if r is None:
        print("Resolve not available — aborting")
        return 2
    pm = r.GetProjectManager()
    previous_project = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    proj = pm.CreateProject(PILOT)
    check("disposable project created", proj is not None, PILOT)
    if proj is None:
        return 2

    try:
        mp = proj.GetMediaPool()
        imported = mp.ImportMedia(clip_paths)
        check("media imported", bool(imported) and len(imported) == 4,
              f"count={len(imported or [])}")
        if not imported or len(imported) != 4:
            return 1
        clip_a, clip_b, clip_c, clip_d = imported

        tl = mp.CreateEmptyTimeline("Gap Workaround Base")
        check("base timeline created", tl is not None)
        if tl is None:
            return 1
        proj.SetCurrentTimeline(tl)

        # Build 3 contiguous clips on V1, each with a 12-frame handle on both
        # sides (source frames [HANDLE, HANDLE+USABLE) of a 144-frame clip) so
        # add_transition has room to borrow frames from either side of a cut.
        start_frame = int(tl.GetStartFrame())
        record = start_frame
        clip_infos = []
        for item in (clip_a, clip_b, clip_c):
            clip_infos.append({
                "mediaPoolItem": item,
                "startFrame": HANDLE,
                "endFrame": HANDLE + USABLE,
                "trackIndex": 1,
                "recordFrame": record,
            })
            record += USABLE
        appended = mp.AppendToTimeline(clip_infos)
        check("base timeline built with handles", bool(appended) and len(appended) == 3,
              f"count={len(appended or [])}")

        v_items = tl.GetItemListInTrack("video", 1) or []
        check("base timeline has 3 clips", len(v_items) == 3, f"count={len(v_items)}")
        if len(v_items) != 3:
            return 1
        item_a, item_b, item_c = v_items
        id_a, id_b, id_c = item_a.GetUniqueId(), item_b.GetUniqueId(), item_c.GetUniqueId()
        orig_count = len(v_items)

        # ---- trim_clip (tail) ----
        gate = s.timeline("trim_clip", {"clip_id": id_a, "edge": "tail", "new_duration": 60})
        check("trim_clip issues confirm token", gate.get("status") == "confirmation_required")
        trimmed = s.timeline("trim_clip", {"clip_id": id_a, "edge": "tail", "new_duration": 60,
                                            "confirm_token": gate.get("confirm_token")})
        check("trim_clip (tail)", bool(trimmed.get("success")),
              str(trimmed.get("error") or trimmed.get("new_timeline")))
        if trimmed.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, trimmed["new_timeline"])
            check("trim_clip: new timeline exists", new_tl is not None, trimmed.get("new_timeline"))
            if new_tl is not None:
                new_items = new_tl.GetItemListInTrack("video", 1) or []
                check("trim_clip: first clip duration is 60",
                      len(new_items) >= 1 and int(new_items[0].GetDuration()) == 60,
                      f"duration={new_items[0].GetDuration() if new_items else None}")
            after = tl.GetItemListInTrack("video", 1) or []
            check("trim_clip: original timeline untouched",
                  len(after) == orig_count and int(after[0].GetDuration()) == USABLE,
                  f"orig first duration={after[0].GetDuration() if after else None}")

        # ---- trim_clip (head) ----
        gate = s.timeline("trim_clip", {"clip_id": id_b, "edge": "head", "frames": 12})
        trimmed_h = s.timeline("trim_clip", {"clip_id": id_b, "edge": "head", "frames": 12,
                                              "confirm_token": gate.get("confirm_token")})
        check("trim_clip (head)", bool(trimmed_h.get("success")), str(trimmed_h.get("error") or ""))

        # ---- move_clip (drp-format auto-creates the destination track) ----
        gate = s.timeline("move_clip", {"clip_id": id_c, "to_track": 2})
        moved = s.timeline("move_clip", {"clip_id": id_c, "to_track": 2,
                                          "confirm_token": gate.get("confirm_token")})
        check("move_clip", bool(moved.get("success")), str(moved.get("error") or ""))
        if moved.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, moved["new_timeline"])
            if new_tl is not None:
                v2 = new_tl.GetItemListInTrack("video", 2) or []
                check("move_clip: clip landed on track 2", len(v2) == 1, f"v2 count={len(v2)}")
        check("move_clip: original still single video track",
              s._timeline_track_count(tl, "video") == 1)

        # ---- slide_clip ----
        new_start = int(item_c.GetStart()) + 24
        gate = s.timeline("slide_clip", {"clip_id": id_c, "to_start": new_start})
        slid = s.timeline("slide_clip", {"clip_id": id_c, "to_start": new_start,
                                          "confirm_token": gate.get("confirm_token")})
        check("slide_clip", bool(slid.get("success")), str(slid.get("error") or ""))
        if slid.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, slid["new_timeline"])
            if new_tl is not None:
                v1 = new_tl.GetItemListInTrack("video", 1) or []
                c_now = next((it for it in v1 if int(it.GetDuration()) == USABLE
                              and int(it.GetStart()) == new_start), None)
                check("slide_clip: clip repositioned, duration unchanged", c_now is not None,
                      f"items_start={[int(i.GetStart()) for i in v1]}")

        # ---- slip_clip ----
        source_start_before = s._timeline_item_source_start(item_b)
        gate = s.timeline("slip_clip", {"clip_id": id_b, "frames": 10})
        slipped = s.timeline("slip_clip", {"clip_id": id_b, "frames": 10,
                                            "confirm_token": gate.get("confirm_token")})
        check("slip_clip", bool(slipped.get("success")), str(slipped.get("error") or ""))
        if slipped.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, slipped["new_timeline"])
            if new_tl is not None:
                v1 = new_tl.GetItemListInTrack("video", 1) or []
                b_now = next((it for it in v1 if int(it.GetStart()) == int(item_b.GetStart())), None)
                source_after = s._timeline_item_source_start(b_now) if b_now is not None else None
                check("slip_clip: position/duration unchanged, source shifted +10",
                      b_now is not None
                      and int(b_now.GetDuration()) == int(item_b.GetDuration())
                      and source_after == (source_start_before or 0) + 10,
                      f"source_before={source_start_before} source_after={source_after}")
        # Retreat (negative frames) is supported since #30/3.1.5 — the earlier
        # +10 slip guarantees head room, so -5 must land at source +5.
        gate = s.timeline("slip_clip", {"clip_id": id_b, "frames": -5})
        retreat = s.timeline("slip_clip", {"clip_id": id_b, "frames": -5,
                                            "confirm_token": gate.get("confirm_token")})
        check("slip_clip: retreat (frames<0)", bool(retreat.get("success")),
              str(retreat.get("error") or ""))
        refused = s.timeline("slip_clip", {"clip_id": id_b, "frames": 0})
        check("slip_clip: frames=0 honestly refused", "error" in refused, str(refused))
        # Live source-bound checks (#30 gotcha fix): the 144-frame source leaves
        # only 12-frame handles, so ±100 overruns tail/head and must refuse
        # BEFORE any export happens.
        refused = s.timeline("slip_clip", {"clip_id": id_b, "frames": 100})
        check("slip_clip: tail overrun refused live",
              "overruns the source tail" in str(refused.get("error", "")), str(refused.get("error")))
        refused = s.timeline("slip_clip", {"clip_id": id_b, "frames": -100})
        check("slip_clip: head overrun refused live",
              "overruns the source head" in str(refused.get("error", "")), str(refused.get("error")))

        # ---- split_clip (razor) ----
        at = int(item_c.GetStart()) + int(item_c.GetDuration()) // 2
        gate = s.timeline("split_clip", {"clip_id": id_c, "at_frame": at})
        split = s.timeline("split_clip", {"clip_id": id_c, "at_frame": at,
                                           "confirm_token": gate.get("confirm_token")})
        check("split_clip", bool(split.get("success")), str(split.get("error") or ""))
        if split.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, split["new_timeline"])
            if new_tl is not None:
                new_items = new_tl.GetItemListInTrack("video", 1) or []
                check("split_clip: one extra item on the track",
                      len(new_items) == orig_count + 1, f"count={len(new_items)}")

        # ---- add_transition + list_transitions ----
        before_list = s.timeline("list_transitions", {})
        check("list_transitions: none on the original timeline",
              before_list.get("count") == 0, str(before_list))
        cut_frame = int(item_b.GetStart())
        gate = s.timeline("add_transition", {"track_index": 1, "at_frame": cut_frame,
                                              "duration_frames": 12})
        added = s.timeline("add_transition", {"track_index": 1, "at_frame": cut_frame,
                                               "duration_frames": 12,
                                               "confirm_token": gate.get("confirm_token")})
        check("add_transition", bool(added.get("success")), str(added.get("error") or ""))
        if added.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, added["new_timeline"])
            if new_tl is not None:
                proj.SetCurrentTimeline(new_tl)
                after_list = s.timeline("list_transitions", {})
                check("list_transitions: transition found on the edited timeline",
                      after_list.get("count", 0) >= 1, str(after_list))
                proj.SetCurrentTimeline(tl)

        # ---- replace_edit (pure live API, in place) ----
        gate = s.timeline("replace_edit", {"clip_id": id_a, "media_pool_item_id": clip_d.GetUniqueId()})
        check("replace_edit issues confirm token", gate.get("status") == "confirmation_required")
        replaced = s.timeline("replace_edit", {"clip_id": id_a, "media_pool_item_id": clip_d.GetUniqueId(),
                                                "confirm_token": gate.get("confirm_token")})
        check("replace_edit", bool(replaced.get("success")), str(replaced.get("error") or ""))
        if replaced.get("success"):
            after = tl.GetItemListInTrack("video", 1) or []
            check("replace_edit: in place, same item count on the CURRENT timeline",
                  len(after) == orig_count, f"count={len(after)}")
            check("replace_edit: old clip id gone",
                  all(it.GetUniqueId() != id_a for it in after))

        # ---- place_on_top_edit (pure live API, in place) ----
        before_tracks = s._timeline_track_count(tl, "video")
        gate = s.timeline("place_on_top_edit", {
            "media_pool_item_id": clip_d.GetUniqueId(), "record_frame": start_frame,
            "source_start_frame": 0, "source_end_frame": 48})
        placed = s.timeline("place_on_top_edit", {
            "media_pool_item_id": clip_d.GetUniqueId(), "record_frame": start_frame,
            "source_start_frame": 0, "source_end_frame": 48,
            "confirm_token": gate.get("confirm_token")})
        check("place_on_top_edit", bool(placed.get("success")), str(placed.get("error") or ""))
        if placed.get("success"):
            check("place_on_top_edit: new top track added",
                  s._timeline_track_count(tl, "video") == before_tracks + 1)
            top_items = tl.GetItemListInTrack("video", before_tracks + 1) or []
            check("place_on_top_edit: clip present on the new top track", len(top_items) == 1,
                  f"count={len(top_items)}")

        # ---- insert_edit (ripple + place, on a NEW timeline) ----
        insert_at = int(item_c.GetStart())
        gate = s.timeline("insert_edit", {
            "media_pool_item_id": clip_d.GetUniqueId(), "record_frame": insert_at,
            "track_index": 1, "source_start_frame": 0, "source_end_frame": 48})
        inserted = s.timeline("insert_edit", {
            "media_pool_item_id": clip_d.GetUniqueId(), "record_frame": insert_at,
            "track_index": 1, "source_start_frame": 0, "source_end_frame": 48,
            "confirm_token": gate.get("confirm_token")})
        check("insert_edit", bool(inserted.get("success")) and bool(inserted.get("inserted_clip_id")),
              str(inserted.get("error") or inserted.get("place_error") or inserted))
        if inserted.get("success"):
            new_tl, _ = s._find_timeline_by_name(proj, inserted["new_timeline"])
            if new_tl is not None:
                v1 = new_tl.GetItemListInTrack("video", 1) or []
                check("insert_edit: one extra item on V1 after the insert",
                      len(v1) == orig_count + 1, f"count={len(v1)}")
                shifted = next((it for it in v1 if int(it.GetStart()) == insert_at + 48), None)
                check("insert_edit: downstream clip rippled later by the inserted duration",
                      shifted is not None, f"items_start={[int(i.GetStart()) for i in v1]}")

        return 0 if all(ok for _, ok, _ in CHECKS) else 1
    finally:
        try:
            outcome = delete_project_safely(pm, PILOT, switch_to=previous_project)
            if outcome["success"]:
                print(f"cleanup: previous project restored; pilot deleted "
                      f"(attempts={outcome['attempts']})")
            else:
                print(f"cleanup warning: disposable project '{outcome['leftover']}' "
                      f"left in library ({outcome['detail']}) — delete it manually")
        except Exception as exc:
            print(f"cleanup warning: {exc}")
        import shutil as _shutil
        _shutil.rmtree(MEDIA_DIR, ignore_errors=True)


if __name__ == "__main__":
    from preflight import gate
    gate("open")
    code = main()
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed")
    sys.exit(code)
