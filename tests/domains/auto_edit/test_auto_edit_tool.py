"""Offline tests for the auto_edit compound tool (src/server.py).

No Resolve required: offline actions run against an explicit analysis_root;
the build-row assembler is pure. The live end-to-end path is validated by
tests/live_auto_edit_validation.py per the release process.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest

from tests._error_envelope_helpers import assert_error_mentions

import src.server as s
import src.domains.auto_edit.actions as _dom_auto_edit
from src.domains.auto_edit.utils import auto_edit, cut_ir, edit_engine


def run(coro):
    return asyncio.run(coro)


def make_plan(root, *, music=False, titles=False, punch_in=False, overlays=False):
    segments = [
        cut_ir.make_cut_list_segment(
            role="speech", clip_id="clip-a", clip_uuid="uuid-a",
            source_start_frame=0, source_end_frame=48,
            audio_track_indices=[1], transcript_excerpt="hello"),
        cut_ir.make_cut_list_segment(
            role="speech", clip_id="clip-a", clip_uuid="uuid-a",
            source_start_frame=58, source_end_frame=96,
            audio_track_indices=[1], jumpcut_smoothing="punch_in" if punch_in else None,
            punch_in={"zoom": 1.12} if punch_in else None),
    ]
    plan = cut_ir.make_cut_list(
        segments=segments, fps=24.0,
        titles=[{"text": "T", "role": "intro", "at_frame": 0, "duration_frames": 96}] if titles else [],
        overlays=[{
            "clip_uuid": "uuid-b", "source_start_frame": 120, "source_end_frame": 168,
            "duration_frames": 48, "track_index": 2, "over_segment_index": 1,
        }] if overlays else [],
        music={"path": "/media/song.wav", "track_index": 2,
               "gain_db": -11.7} if music else None,
    )
    auto_edit._assign_record_frames(plan)
    return edit_engine.save_plan(root, plan)


class BuildRowsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-tool-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_speech_rows_mirror_audio_half_open(self):
        plan = make_plan(self.root)
        rows = s._auto_edit_build_rows(plan)
        video = [r for r in rows if r["media_type"] == 1]
        audio = [r for r in rows if r["media_type"] == 2]
        self.assertEqual(len(video), 2)
        self.assertEqual(len(audio), 2)  # one mirror per segment
        # Half-open source ranges pass through verbatim; record walks the cursor.
        self.assertEqual((video[0]["start_frame"], video[0]["end_frame"]), (0, 48))
        self.assertEqual(video[0]["record_frame"], 0)
        self.assertEqual((video[1]["start_frame"], video[1]["end_frame"]), (58, 96))
        self.assertEqual(video[1]["record_frame"], 48)  # duration 48, no gap
        for v, a in zip(video, audio):
            self.assertEqual(a["start_frame"], v["start_frame"])
            self.assertEqual(a["end_frame"], v["end_frame"])
            self.assertEqual(a["record_frame"], v["record_frame"])
            self.assertEqual(a["track_index"], 1)

    def test_record_offset_shifts_all_rows(self):
        plan = make_plan(self.root, music=True)
        rows = s._auto_edit_build_rows(plan, record_offset=96)
        self.assertTrue(all(r["record_frame"] >= 96 for r in rows))
        self.assertEqual(rows[0]["record_frame"], 96)

    def test_overlay_lands_on_v2(self):
        plan = make_plan(self.root, overlays=True)
        rows = s._auto_edit_build_rows(plan)
        broll = [r for r in rows if r["role"] == "broll"]
        self.assertEqual(len(broll), 1)
        self.assertEqual(broll[0]["track_index"], 2)
        self.assertEqual(broll[0]["media_type"], 1)
        # Overlay covers the head of segment 1 (record 48).
        self.assertEqual(broll[0]["record_frame"], 48)

    def test_music_trimmed_to_cut_on_a2(self):
        plan = make_plan(self.root, music=True)
        rows = s._auto_edit_build_rows(plan)
        music = [r for r in rows if r["role"] == "music"]
        self.assertEqual(len(music), 1)
        row = music[0]
        self.assertEqual(row["media_type"], 2)
        self.assertEqual(row["track_index"], 2)
        # Total cut = 48 + 38 = 86 frames; music source 0..86, record 0.
        self.assertEqual((row["start_frame"], row["end_frame"]), (0, 86))
        self.assertEqual(row["record_frame"], 0)
        self.assertEqual(row["clip_path"], "/media/song.wav")

    def test_bed_path_wins_when_rendered(self):
        plan = make_plan(self.root, music=True)
        plan["music"]["bed_path"] = "/root/analysis/bed.wav"
        rows = s._auto_edit_build_rows(plan)
        music = [r for r in rows if r["role"] == "music"]
        self.assertEqual(music[0]["clip_path"], "/root/analysis/bed.wav")

    def test_punch_in_carried_on_video_row_only(self):
        plan = make_plan(self.root, punch_in=True)
        rows = s._auto_edit_build_rows(plan)
        video = [r for r in rows if r["media_type"] == 1]
        self.assertIsNone(video[0].get("punch_in"))
        self.assertEqual(video[1]["punch_in"]["zoom"], 1.12)
        self.assertTrue(all("punch_in" not in r or r["media_type"] == 1 for r in rows))


class BeatAlignmentTest(unittest.TestCase):
    """_compute_beat_alignment (issue #181, phase 6/6 of the montage-quality
    epic): the built-timeline-level check that would have caught the
    original drift — every item's actual start frame must match exactly
    what its plan segment says, not just land somewhere in the grid."""

    @staticmethod
    def _segs(*record_start_frames):
        return [{"record_start_frame": f} for f in record_start_frames]

    def test_perfectly_aligned_build_has_no_deviations(self):
        segments = self._segs(0, 48, 96, 168)
        out = s._compute_beat_alignment(segments, [0, 48, 96, 168])
        self.assertEqual(out["checked"], 4)
        self.assertEqual(out["deviations"], [])

    def test_a_single_off_grid_item_is_reported_precisely(self):
        segments = self._segs(0, 48, 96)
        out = s._compute_beat_alignment(segments, [0, 50, 96])  # item 1 is 2 frames late
        self.assertEqual(out["checked"], 3)
        self.assertEqual(len(out["deviations"]), 1)
        dev = out["deviations"][0]
        self.assertEqual(dev["segment"], 1)
        self.assertEqual(dev["expected"], 48)
        self.assertEqual(dev["actual"], 50)
        self.assertEqual(dev["deviation_frames"], 2)

    def test_item_offset_skips_the_intro_title_item(self):
        # item 0 is the title itself; item 1 corresponds to segment 0, etc.
        segments = self._segs(0, 48)
        out = s._compute_beat_alignment(
            segments, [0, 100, 148], item_offset=1, record_offset=100)
        self.assertEqual(out["checked"], 2)  # title item (index 0) never compared
        self.assertEqual(out["deviations"], [])

    def test_record_offset_shifts_every_expected_position(self):
        segments = self._segs(0, 48)
        out = s._compute_beat_alignment(segments, [100, 148], record_offset=100)
        self.assertEqual(out["deviations"], [])

    def test_extra_items_beyond_the_plans_segments_are_not_compared(self):
        segments = self._segs(0, 48)
        out = s._compute_beat_alignment(segments, [0, 48, 96, 144])
        self.assertEqual(out["checked"], 2)
        self.assertEqual(out["deviations"], [])


class ApproveCutActionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-approve-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _approve(self, plan_id, extra=None):
        params = {"plan_id": plan_id, "analysis_root": self.root}
        params.update(extra or {})
        return run(s.auto_edit("approve_cut", params))

    def test_checkpoint_token_round_trip_with_consent(self):
        plan = make_plan(self.root, music=True)
        first = self._approve(plan["plan_id"], {"music_bed_consent": True})
        self.assertEqual(first.get("status"), "confirmation_required")
        preview = first["preview"]
        self.assertIn(auto_edit.MUSIC_BED_CONSENT_LINE, preview["music_bed_consent_line"])
        self.assertTrue(preview["music_bed_consent_requested"])
        self.assertIn("summary_markdown", preview)
        second = self._approve(plan["plan_id"], {
            "music_bed_consent": True, "confirm_token": first["confirm_token"]})
        self.assertTrue(second.get("success"), second)
        self.assertTrue(second["music_bed_consent"])
        stored = edit_engine.load_plan(self.root, plan["plan_id"])
        self.assertEqual(stored["music"]["ducking"]["mode"], "rendered_bed")
        self.assertTrue(stored["music"]["ducking"]["user_approved_render"])
        self.assertIsNotNone(stored.get("approved_at"))

    def test_no_consent_keeps_static_bed(self):
        plan = make_plan(self.root, music=True)
        first = self._approve(plan["plan_id"])
        second = self._approve(plan["plan_id"], {"confirm_token": first["confirm_token"]})
        self.assertTrue(second.get("success"), second)
        stored = edit_engine.load_plan(self.root, plan["plan_id"])
        self.assertEqual(stored["music"]["ducking"]["mode"], "static")
        self.assertFalse(stored["music"]["ducking"]["user_approved_render"])

    def test_prefer_drt_ducking_selects_drt_automation_without_consent(self):
        # Tier-2 (issue #14): derivative-free, so no consent line and no rendered bed.
        plan = make_plan(self.root, music=True)
        first = self._approve(plan["plan_id"], {"prefer_drt_ducking": True})
        self.assertEqual(first.get("status"), "confirmation_required")
        self.assertTrue(first["preview"]["prefer_drt_ducking"])
        self.assertNotIn("music_bed_consent_line", first["preview"])
        second = self._approve(plan["plan_id"], {
            "prefer_drt_ducking": True, "confirm_token": first["confirm_token"]})
        self.assertTrue(second.get("success"), second)
        self.assertEqual(second["ducking_mode"], "drt_automation")
        stored = edit_engine.load_plan(self.root, plan["plan_id"])
        self.assertEqual(stored["music"]["ducking"]["mode"], "drt_automation")
        self.assertFalse(stored["music"]["ducking"]["user_approved_render"])

    def test_unknown_plan_errors(self):
        out = self._approve("nope")
        assert_error_mentions(self, out, 'cut list not found')

    def test_build_timeline_requires_approval_before_token(self):
        # The approval gate fires BEFORE any confirm-token ceremony — an
        # unapproved plan never even reaches the token stage. Runs offline
        # because the gate check precedes the need for a live project only in
        # ordering of *our* checks; without Resolve the context errors first,
        # so exercise the gate directly.
        plan = make_plan(self.root)
        gate = auto_edit.require_approved_plan(self.root, plan["plan_id"])
        self.assertFalse(gate["success"])
        self.assertIn("not approved", gate["error"])


class SummaryAndListTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-summary-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_get_cut_summary_markdown_and_json(self):
        plan = make_plan(self.root, music=True, titles=True)
        md = run(s.auto_edit("get_cut_summary", {
            "plan_id": plan["plan_id"], "analysis_root": self.root}))
        self.assertTrue(md.get("success"), md)
        self.assertIn("Cut list", md["summary"])
        as_json = run(s.auto_edit("get_cut_summary", {
            "plan_id": plan["plan_id"], "analysis_root": self.root, "format": "json"}))
        self.assertEqual(as_json["plan"]["plan_id"], plan["plan_id"])

    def test_list_briefs_filters_kinds(self):
        make_plan(self.root)  # a CutList, not a brief
        created = auto_edit.create_brief(self.root, files=["/media/a.mp4"])
        out = run(s.auto_edit("list_briefs", {"analysis_root": self.root}))
        self.assertTrue(out.get("success"), out)
        ids = [b["plan_id"] for b in out["briefs"]]
        self.assertEqual(ids, [created["brief_id"]])

    def test_unknown_action_lists_actions(self):
        out = run(s.auto_edit("explode", {"analysis_root": self.root}))
        self.assertIn("error", out)
        for name in ("start_brief", "plan_cut", "approve_cut", "build_timeline", "finish"):
            self.assertIn(name, str(out))


class ReviseCutActionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-revise-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_revise_via_brief_latest_plan(self):
        created = auto_edit.create_brief(self.root, files=["/media/a.mp4"])
        plan = make_plan(self.root)
        auto_edit.advance_brief(self.root, created["brief_id"], "ready")
        auto_edit.advance_brief(self.root, created["brief_id"], "planned",
                                latest_plan_id=plan["plan_id"])
        out = run(s.auto_edit("revise_cut", {
            "brief_id": created["brief_id"], "analysis_root": self.root,
            "notes": "tighter", "edits": [{"op": "drop", "index": 1}]}))
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["revision"], 1)
        self.assertEqual(len(out["plan"]["segments"]), 1)
        brief = auto_edit.load_brief(self.root, created["brief_id"])
        self.assertEqual(brief["latest_plan_id"], out["plan_id"])


class FinishActionTest(unittest.TestCase):
    """finish() against a mocked project: gates, then render path reporting."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-finish-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _mock_project(self, timeline_name="TL"):
        from unittest import mock
        tl = mock.Mock()
        tl.GetName.return_value = timeline_name
        tl.GetUniqueId.return_value = f"uid-{timeline_name}"
        tl.GetItemListInTrack.return_value = []
        proj = mock.Mock()
        proj.GetTimelineCount.return_value = 1
        proj.GetTimelineByIndex.return_value = tl

        # #113 Tier 1: finish() now verifies the current-timeline switch by
        # reading it back before it grades/renders, so the stub must model the
        # switch rather than leaving GetCurrentTimeline as a bare auto-Mock
        # (whose GetUniqueId would never match the timeline being switched to).
        def _switch(target):
            proj.GetCurrentTimeline.return_value = target
            return True

        proj.SetCurrentTimeline.side_effect = _switch
        proj.GetCurrentTimeline.return_value = tl

        proj.SetRenderSettings.return_value = True
        proj.AddRenderJob.return_value = "job-1"
        proj.StartRendering.return_value = True
        proj.IsRenderingInProgress.return_value = False
        proj.GetRenderJobStatus.return_value = {"JobStatus": "Complete", "CompletionPercentage": 100}
        return proj, tl

    def _finish(self, proj, params):
        from unittest import mock
        with mock.patch.object(_dom_auto_edit, "_destructive_versioning_provider",
            return_value=(None, proj, self.root, "P"),
        ):
            return run(s.auto_edit("finish", params))

    def test_requires_built_timeline(self):
        plan = make_plan(self.root)
        auto_edit.mark_approved(self.root, plan["plan_id"])
        proj, _tl = self._mock_project()
        out = self._finish(proj, {"plan_id": plan["plan_id"]})
        self.assertIn("no built timeline", out.get("error", {}).get("message", str(out)))

    def test_render_reports_existing_output_path(self):
        import os
        plan = make_plan(self.root)
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        target_dir = tempfile.mkdtemp(prefix="auto-edit-render-")
        self.addCleanup(shutil.rmtree, target_dir, True)
        custom_name = "final_cut"
        with open(os.path.join(target_dir, custom_name + ".mov"), "wb") as handle:
            handle.write(b"\x00")
        params = {
            "plan_id": plan["plan_id"],
            "render": {"target_dir": target_dir, "custom_name": custom_name},
        }
        gate = self._finish(proj, params)
        self.assertEqual(gate.get("status"), "confirmation_required")
        done = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(done.get("success"), done)
        render = done["render"]
        self.assertTrue(render["success"], render)
        self.assertEqual(render["job_id"], "job-1")
        self.assertEqual(render["output_path"],
                         os.path.join(target_dir, custom_name + ".mov"))

    def test_render_failure_when_no_output_appears(self):
        plan = make_plan(self.root)
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        target_dir = tempfile.mkdtemp(prefix="auto-edit-render-empty-")
        self.addCleanup(shutil.rmtree, target_dir, True)
        params = {
            "plan_id": plan["plan_id"],
            "render": {"target_dir": target_dir, "custom_name": "ghost"},
        }
        gate = self._finish(proj, params)
        done = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertFalse(done.get("success"))
        self.assertFalse(done["render"]["success"])
        self.assertIn("no output file", done["render"]["error"])


class FinishTargetTest(unittest.TestCase):
    """finish(target=...) — polish_timeline is the ONLY place transitions and
    speed ramps can be authored (the scripting API has neither), and it writes a
    separate "(polished)" timeline. Without a selector that work could never
    reach a render."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-target-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _mock_project(self, names=("TL", "TL (polished)")):
        from unittest import mock
        timelines = []
        for name in names:
            tl = mock.Mock()
            tl.GetName.return_value = name
            tl.GetUniqueId.return_value = f"uid-{name}"
            tl.GetItemListInTrack.return_value = []
            timelines.append(tl)
        proj = mock.Mock()
        proj.GetTimelineCount.return_value = len(timelines)
        proj.GetTimelineByIndex.side_effect = lambda i: timelines[i - 1]

        def _switch(target):
            proj.GetCurrentTimeline.return_value = target
            return True

        proj.SetCurrentTimeline.side_effect = _switch
        proj.GetCurrentTimeline.return_value = timelines[0]
        return proj, timelines

    def _finish(self, proj, params):
        from unittest import mock
        with mock.patch.object(_dom_auto_edit, "_destructive_versioning_provider",
            return_value=(None, proj, self.root, "P"),
        ):
            return run(s.auto_edit("finish", params))

    def _approved_built_plan(self, *, polished=None):
        plan = make_plan(self.root)
        auto_edit.mark_approved(self.root, plan["plan_id"])
        summary = {"timeline_name": "TL"}
        if polished:
            summary["polished"] = {"timeline_name": polished}
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], summary)
        return plan

    def test_default_target_is_the_built_timeline(self):
        plan = self._approved_built_plan(polished="TL (polished)")
        proj, _tls = self._mock_project()
        gate = self._finish(proj, {"plan_id": plan["plan_id"]})
        done = self._finish(proj, {"plan_id": plan["plan_id"],
                                   "confirm_token": gate["confirm_token"]})
        self.assertEqual(done["timeline"], "TL")
        self.assertEqual(done["target"], "built")

    def test_polished_target_selects_the_polished_timeline(self):
        plan = self._approved_built_plan(polished="TL (polished)")
        proj, timelines = self._mock_project()
        params = {"plan_id": plan["plan_id"], "target": "polished"}
        gate = self._finish(proj, params)
        done = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertEqual(done["timeline"], "TL (polished)")
        self.assertEqual(done["target"], "polished")
        # and it actually switched Resolve to that one before grading/rendering
        proj.SetCurrentTimeline.assert_called_with(timelines[1])

    def test_polished_target_without_a_polish_pass_refuses(self):
        plan = self._approved_built_plan()  # no polished timeline recorded
        proj, _tls = self._mock_project()
        out = self._finish(proj, {"plan_id": plan["plan_id"], "target": "polished"})
        self.assertIn("no polished timeline",
                      out.get("error", {}).get("message", str(out)))

    def test_unknown_target_is_rejected(self):
        plan = self._approved_built_plan(polished="TL (polished)")
        proj, _tls = self._mock_project()
        out = self._finish(proj, {"plan_id": plan["plan_id"], "target": "final"})
        self.assertIn("target must be", out.get("error", {}).get("message", str(out)))

    def test_confirm_preview_names_the_target(self):
        plan = self._approved_built_plan(polished="TL (polished)")
        proj, _tls = self._mock_project()
        gate = self._finish(proj, {"plan_id": plan["plan_id"], "target": "polished"})
        self.assertEqual(gate.get("status"), "confirmation_required")
        preview = gate.get("preview") or {}
        self.assertEqual(preview.get("target"), "polished")
        self.assertEqual(preview.get("timeline"), "TL (polished)")


class GradeActionTest(unittest.TestCase):
    """finish()'s grade branch (issue #179, phase 4/6 of the montage-quality
    epic): the new per-bucket `match` stage is purely additive — the
    existing uniform lut/cdl/drx paths must keep working byte-identically."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-grade-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _mock_project(self, *, item_count, timeline_name="TL"):
        from unittest import mock
        tl = mock.Mock()
        tl.GetName.return_value = timeline_name
        tl.GetUniqueId.return_value = f"uid-{timeline_name}"
        items = []
        for i in range(item_count):
            item = mock.Mock()
            item.SetCDL.return_value = True
            graph = mock.Mock()
            graph.SetLUT.return_value = True
            graph.ApplyGradeFromDRX.return_value = True
            item.GetNodeGraph.return_value = graph
            items.append(item)
        tl.GetItemListInTrack.return_value = items
        proj = mock.Mock()
        proj.GetTimelineCount.return_value = 1
        proj.GetTimelineByIndex.return_value = tl

        def _switch(target):
            proj.GetCurrentTimeline.return_value = target
            return True

        proj.SetCurrentTimeline.side_effect = _switch
        proj.GetCurrentTimeline.return_value = tl
        return proj, tl, items

    def _finish(self, proj, params):
        from unittest import mock
        with mock.patch.object(_dom_auto_edit, "_destructive_versioning_provider",
            return_value=(None, proj, self.root, "P"),
        ):
            return run(s.auto_edit("finish", params))

    def _plan_with_buckets(self, buckets):
        segments = [
            cut_ir.make_cut_list_segment(
                role="montage_hook" if i == 0 else "montage",
                clip_id=f"clip-{i}", clip_uuid=f"uuid-{i}",
                source_start_frame=i * 48, source_end_frame=i * 48 + 48)
            for i in range(len(buckets))
        ]
        for seg, bucket in zip(segments, buckets):
            seg["look_bucket"] = bucket
        plan = cut_ir.make_cut_list(segments=segments, fps=24.0)
        auto_edit._assign_record_frames(plan)
        return edit_engine.save_plan(self.root, plan)

    def test_uniform_cdl_lut_drx_unchanged(self):
        # The pre-existing behaviour: one CDL/LUT/DRX applied to every item,
        # regardless of any bucket — no `grade["match"]` key at all.
        plan = self._plan_with_buckets(["a", "b", "c"])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items = self._mock_project(item_count=3)
        params = {
            "plan_id": plan["plan_id"],
            "grade": {"cdl": {"Slope": [1.0, 1.0, 1.0]}, "lut_path": "/luts/look.cube",
                      "drx_path": "/grades/look.drx"},
        }
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        graded = out["grade"]
        self.assertEqual(graded["cdl"], {"applied": 3, "of": 3})
        self.assertEqual(graded["lut"], {"applied": 3, "of": 3})
        self.assertEqual(graded["drx"], {"applied": 3, "of": 3})
        self.assertNotIn("match", graded)
        # every item got the SAME normalized CDL — no per-bucket differentiation
        calls = [c.args[0] for c in items[0].SetCDL.call_args_list]
        for item in items:
            self.assertEqual(item.SetCDL.call_args.args[0], calls[0])

    def test_match_applies_different_cdl_per_bucket(self):
        plan = self._plan_with_buckets(["warm_bright", "cool_dark", "warm_bright"])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items = self._mock_project(item_count=3)
        match = {
            "warm_bright": {"cdl": {"Slope": [0.95, 1.0, 1.05], "Offset": [-0.2, -0.2, -0.2]}},
            "cool_dark": {"cdl": {"Slope": [1.05, 1.0, 0.95], "Offset": [0.2, 0.2, 0.2]}},
        }
        params = {"plan_id": plan["plan_id"], "grade": {"match": match}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        graded = out["grade"]["match"]
        self.assertEqual(graded["applied"], 3)
        self.assertEqual(graded["of"], 3)
        self.assertEqual(graded["by_bucket"], {"warm_bright": 2, "cool_dark": 1})
        # item 0 and item 2 (both warm_bright) got the SAME cdl; item 1 (cool_dark) differs
        self.assertEqual(items[0].SetCDL.call_args.args[0], items[2].SetCDL.call_args.args[0])
        self.assertNotEqual(items[0].SetCDL.call_args.args[0], items[1].SetCDL.call_args.args[0])
        self.assertIn("-0.2", items[0].SetCDL.call_args.args[0]["Offset"])

    def test_match_honors_intro_title_record_offset(self):
        # item 0 on V1 is the intro title itself when titles were built —
        # segment[0]'s bucket must map to item 1, not item 0.
        plan = self._plan_with_buckets(["warm_bright", "cool_dark"])
        plan["execution_summary"] = {"timeline_name": "TL", "title": {"record_offset": 48}}
        edit_engine.save_plan(self.root, plan)
        auto_edit.mark_approved(self.root, plan["plan_id"])
        proj, _tl, items = self._mock_project(item_count=3)  # title + 2 segments
        match = {
            "warm_bright": {"cdl": {"Slope": [0.9, 1.0, 1.1]}},
            "cool_dark": {"cdl": {"Slope": [1.1, 1.0, 0.9]}},
        }
        params = {"plan_id": plan["plan_id"], "grade": {"match": match}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        # the title item (index 0) never gets a match CDL
        items[0].SetCDL.assert_not_called()
        items[1].SetCDL.assert_called_once()
        items[2].SetCDL.assert_called_once()
        self.assertEqual(out["grade"]["match"]["applied"], 2)

    def test_match_accepts_the_plans_own_look_buckets_verbatim(self):
        # The plan hands back {bucket: <raw CDL>} (montage_edit.compute_match_cdls)
        # while the documented param is {bucket: {"cdl": ...}}. Feeding the tool
        # its own suggestion is the obvious call, so BOTH shapes must apply —
        # requiring the re-wrap made the obvious call a silent no-op.
        from src.domains.auto_edit.utils import montage_edit
        look_buckets = montage_edit.compute_match_cdls(
            {"uuid-0": {"brightness": 0.7, "tone": "warm"},
             "uuid-1": {"brightness": 0.2, "tone": "cool"}},
            {"uuid-0": "warm_bright", "uuid-1": "cool_dark"})
        self.assertNotIn("cdl", look_buckets["warm_bright"])  # raw, unwrapped
        plan = self._plan_with_buckets(["warm_bright", "cool_dark"])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items = self._mock_project(item_count=2)
        params = {"plan_id": plan["plan_id"], "grade": {"match": look_buckets}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["grade"]["match"]["applied"], 2)
        self.assertEqual(out["grade"]["match"]["by_bucket"],
                         {"warm_bright": 1, "cool_dark": 1})
        self.assertNotEqual(items[0].SetCDL.call_args.args[0],
                            items[1].SetCDL.call_args.args[0])

    def test_match_with_no_bucket_data_reports_zero_and_never_blocks(self):
        plan = self._plan_with_buckets([None, None])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items = self._mock_project(item_count=2)
        params = {"plan_id": plan["plan_id"],
                  "grade": {"match": {"warm_bright": {"cdl": {"Slope": [1, 1, 1]}}}}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["grade"]["match"], {"applied": 0, "of": 2, "by_bucket": {}})
        for item in items:
            item.SetCDL.assert_not_called()


class MotionActionTest(unittest.TestCase):
    """finish()'s motion branch (issue #180, phase 5/6 of the
    montage-quality epic): beat-locked Fusion motion, flash, and retime,
    applied per-segment via a small per-clip Fusion comp. Opt-in via the
    `motion` param — omitting it changes nothing (grade/subtitles/render
    tests above never pass it)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-motion-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _mock_project(self, *, item_count):
        from unittest import mock
        tl = mock.Mock()
        tl.GetName.return_value = "TL"
        tl.GetUniqueId.return_value = "uid-TL"
        items = []
        comps = []
        for _ in range(item_count):
            comp = mock.MagicMock()
            comp.FindTool.return_value = None  # nothing exists yet -> AddTool path
            # SetInput/GetInput readback: _fusion_input_set_ok verifies via
            # GetInput rather than trusting SetInput's own (live-unreliable)
            # return, so the mock must actually round-trip a value per input
            # name for that verification to mean anything here.
            tool_mock = comp.AddTool.return_value
            input_store = {}
            tool_mock.SetInput.side_effect = (
                lambda name, value, *a, **kw: input_store.__setitem__(name, value))
            tool_mock.GetInput.side_effect = lambda name, *a, **kw: input_store.get(name)
            comps.append(comp)
            item = mock.Mock()
            item.GetFusionCompCount.return_value = 0
            item.AddFusionComp.return_value = comp
            item.SetProperty.return_value = True
            items.append(item)
        tl.GetItemListInTrack.return_value = items
        proj = mock.Mock()
        proj.GetTimelineCount.return_value = 1
        proj.GetTimelineByIndex.return_value = tl

        def _switch(target):
            proj.GetCurrentTimeline.return_value = target
            return True

        proj.SetCurrentTimeline.side_effect = _switch
        proj.GetCurrentTimeline.return_value = tl
        return proj, tl, items, comps

    def _finish(self, proj, params):
        from unittest import mock
        with mock.patch.object(_dom_auto_edit, "_destructive_versioning_provider",
            return_value=(None, proj, self.root, "P"),
        ):
            return run(s.auto_edit("finish", params))

    def _plan_with_directives(self, directives):
        segments = []
        for i, d in enumerate(directives):
            seg = cut_ir.make_cut_list_segment(
                role="montage_hook" if i == 0 else "montage",
                clip_id=f"clip-{i}", clip_uuid=f"uuid-{i}",
                source_start_frame=0, source_end_frame=48)
            seg["motion"] = d.get("motion")
            seg["flash"] = d.get("flash", False)
            seg["retime"] = d.get("retime", False)
            segments.append(seg)
        plan = cut_ir.make_cut_list(segments=segments, fps=24.0)
        auto_edit._assign_record_frames(plan)  # sequential accumulate walk: 0, 48, 96, ...
        return edit_engine.save_plan(self.root, plan)

    def _motion_directive(self):
        return {"zoom_start": 1.0, "zoom_end": 1.05, "amp": 0.05, "beat_seconds": 0.5}

    def test_omitting_motion_param_applies_nothing(self):
        plan = self._plan_with_directives([{"motion": self._motion_directive()}])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items, _comps = self._mock_project(item_count=1)
        out = run(s.auto_edit("finish", {"plan_id": plan["plan_id"]}))
        # No confirm-token gate is even relevant here — the point is that the
        # motion machinery never touches the item without an explicit param.
        items[0].GetFusionCompCount.assert_not_called()
        self.assertNotIn("motion", out)

    def test_motion_directive_adds_a_transform_with_zoom_expression(self):
        plan = self._plan_with_directives([{"motion": self._motion_directive()}])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items, comps = self._mock_project(item_count=1)
        params = {"plan_id": plan["plan_id"], "motion": {}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["motion"]["applied"], 1)
        items[0].AddFusionComp.assert_called_once()
        comps[0].AddTool.assert_any_call("Transform", -1, -1)
        transform = comps[0].AddTool.return_value
        # the Size INPUT (transform["Size"]) gets the expression, not the tool itself
        size_input = transform.__getitem__("Size")
        size_input.SetExpression.assert_called_once()
        expr = size_input.SetExpression.call_args.args[0]
        self.assertIn("fmod", expr)
        self.assertIn("exp", expr)

    def test_flash_flag_adds_brightness_contrast_with_gain_expression(self):
        plan = self._plan_with_directives([{"flash": True}])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items, comps = self._mock_project(item_count=1)
        params = {"plan_id": plan["plan_id"], "motion": {}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["flash"]["applied"], 1)
        comps[0].AddTool.assert_any_call("BrightnessContrast", -1, -1)

    def test_retime_flag_sets_process_explicitly(self):
        # look defaults on and also touches SetProperty (Crop*) — disable it
        # here to isolate the retime assertion.
        plan = self._plan_with_directives([{"retime": True}])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items, _comps = self._mock_project(item_count=1)
        params = {"plan_id": plan["plan_id"], "motion": {"look": False}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["retime"]["applied"], 1)
        self.assertEqual(out["retime"]["process"], "optical_flow")
        items[0].SetProperty.assert_called_once_with("RetimeProcess", 3)
        # never left at the project default (0)
        self.assertNotEqual(items[0].SetProperty.call_args.args[1], 0)

    def test_no_directives_on_any_segment_applies_nothing_with_look_disabled(self):
        plan = self._plan_with_directives([{}, {}])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items, _comps = self._mock_project(item_count=2)
        params = {"plan_id": plan["plan_id"], "motion": {"look": False}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["motion"]["applied"], 0)
        self.assertEqual(out["flash"]["applied"], 0)
        self.assertEqual(out["retime"]["applied"], 0)
        self.assertEqual(out["look"]["applied"], 0)
        for item in items:
            item.GetFusionCompCount.assert_not_called()

    def test_look_defaults_on_and_applies_vignette_grain_letterbox_to_every_clip(self):
        # No motion/flash/retime directives at all — the look pass (vignette
        # + grain + letterbox) still applies to every clip by default,
        # since it is genre-wide, not conditional on beat-grid confidence.
        plan = self._plan_with_directives([{}, {}])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items, comps = self._mock_project(item_count=2)
        params = {"plan_id": plan["plan_id"], "motion": {}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["look"]["applied"], 2)
        self.assertEqual(out["look"]["letterbox_applied"], 2)
        for item in items:
            item.AddFusionComp.assert_called_once()
            item.SetProperty.assert_any_call("CropTop", 0.06)
            item.SetProperty.assert_any_call("CropBottom", 0.06)
        for comp in comps:
            comp.AddTool.assert_any_call("EllipseMask", -1, -1)
            comp.AddTool.assert_any_call("FastNoise", -1, -1)
            comp.AddTool.assert_any_call("Merge", -1, -1)
            # issue #201: EllipseMask defaults to a small ellipse with a hard
            # (SoftEdge=0) edge — a matte, not a vignette. The mask geometry
            # must be set explicitly and verified by readback like every
            # other Fusion input in this pass.
            tool_mock = comp.AddTool.return_value
            tool_mock.SetInput.assert_any_call("SoftEdge", 0.5)
            tool_mock.SetInput.assert_any_call("Width", 1.35)
            tool_mock.SetInput.assert_any_call("Height", 1.35)
            tool_mock.SetInput.assert_any_call("Center", [0.5, 0.5])
            tool_mock.SetInput.assert_any_call("Gain", 0.4)

    def test_vignette_mask_geometry_readback_failure_is_surfaced(self):
        # A failed SoftEdge/Width/Height set must show up in `errors` and
        # must not count toward `look.applied` — same doctrine as
        # test_failed_set_expression_is_surfaced_not_swallowed, applied to
        # SetInput readback instead of SetExpression readback.
        plan = self._plan_with_directives([{}])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items, comps = self._mock_project(item_count=1)
        tool_mock = comps[0].AddTool.return_value
        real_get_input = tool_mock.GetInput.side_effect

        def _bad_soft_edge(name, *a, **kw):
            if name == "SoftEdge":
                return 0.0  # readback doesn't match the value that was set
            return real_get_input(name, *a, **kw)

        tool_mock.GetInput.side_effect = _bad_soft_edge
        params = {"plan_id": plan["plan_id"], "motion": {}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        # `look.applied` tracks the grain stage, which is independent of the
        # vignette mask and still succeeds — the mask failure must surface in
        # `errors`, not silently disappear from the applied count.
        self.assertTrue(
            any("vignette mask geometry SetInput readback mismatch" in e for e in out["errors"])
        )

    def test_failed_set_expression_is_surfaced_not_swallowed(self):
        # SetExpression's own return is NOT trustworthy over the Lua bridge
        # (live-verified — see _fusion_expression_set_ok) — a genuine failure
        # shows up as an empty/falsy GetExpression READBACK, not a False
        # return from SetExpression itself. A discarded readback failure
        # would silently report success (issue #111's finding shape), so it
        # must show up in `errors` and NOT count toward `applied`.
        plan = self._plan_with_directives([{"motion": self._motion_directive()}])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl, items, comps = self._mock_project(item_count=1)
        comps[0].AddTool.return_value.__getitem__.return_value.GetExpression.return_value = ""
        params = {"plan_id": plan["plan_id"], "motion": {}}
        gate = self._finish(proj, params)
        out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["motion"]["applied"], 0)
        self.assertTrue(any("SetExpression(Size) readback empty after set" in e for e in out["errors"]))


class QCWiringTest(unittest.TestCase):
    """finish()'s QC pass (issue #181, phase 6/6 of the montage-quality
    epic): on by default for a successful montage render, opt-outable, and
    purely additive — never flips render/finish's own success."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-qc-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.target_dir = tempfile.mkdtemp(prefix="auto-edit-qc-render-")
        self.addCleanup(shutil.rmtree, self.target_dir, True)
        with open(os.path.join(self.target_dir, "cut.mov"), "wb") as handle:
            handle.write(b"\x00")

    def _mock_project(self):
        from unittest import mock
        tl = mock.Mock()
        tl.GetName.return_value = "TL"
        tl.GetUniqueId.return_value = "uid-TL"
        tl.GetItemListInTrack.return_value = []
        proj = mock.Mock()
        proj.GetTimelineCount.return_value = 1
        proj.GetTimelineByIndex.return_value = tl

        def _switch(target):
            proj.GetCurrentTimeline.return_value = target
            return True

        proj.SetCurrentTimeline.side_effect = _switch
        proj.GetCurrentTimeline.return_value = tl
        proj.SetRenderSettings.return_value = True
        proj.AddRenderJob.return_value = "job-1"
        proj.StartRendering.return_value = True
        proj.IsRenderingInProgress.return_value = False
        proj.GetRenderJobStatus.return_value = {"JobStatus": "Complete", "CompletionPercentage": 100}
        return proj, tl

    def _montage_plan(self):
        segments = [
            cut_ir.make_cut_list_segment(
                role="montage_hook", clip_id="clip-0", clip_uuid="uuid-0",
                source_start_frame=0, source_end_frame=48),
            cut_ir.make_cut_list_segment(
                role="montage", clip_id="clip-1", clip_uuid="uuid-1",
                source_start_frame=0, source_end_frame=48),
        ]
        plan = cut_ir.make_cut_list(segments=segments, fps=24.0)
        auto_edit._assign_record_frames(plan)
        return edit_engine.save_plan(self.root, plan)

    def _finish(self, proj, params):
        from unittest import mock
        with mock.patch.object(_dom_auto_edit, "_destructive_versioning_provider",
            return_value=(None, proj, self.root, "P"),
        ):
            return run(s.auto_edit("finish", params))

    def _params(self, plan, **extra):
        return {
            "plan_id": plan["plan_id"],
            "render": {"target_dir": self.target_dir, "custom_name": "cut"},
            **extra,
        }

    def test_qc_runs_by_default_on_a_successful_montage_render(self):
        from unittest import mock
        plan = self._montage_plan()
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        params = self._params(plan)
        gate = self._finish(proj, params)
        fake_qc = {"success": True, "status": "pending_host_analysis", "frame_paths": ["/x.jpg"]}
        with mock.patch.object(_dom_auto_edit._montage_edit_mod, "build_qc_request",
                               return_value=fake_qc) as build_qc:
            out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["qc"], fake_qc)
        build_qc.assert_called_once()
        call_plan = build_qc.call_args.args[0]
        self.assertEqual(call_plan["plan_id"], plan["plan_id"])
        self.assertEqual(build_qc.call_args.args[1], out["render"]["output_path"])

    def test_qc_false_opts_out(self):
        from unittest import mock
        plan = self._montage_plan()
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        params = self._params(plan, qc=False)
        gate = self._finish(proj, params)
        with mock.patch.object(_dom_auto_edit._montage_edit_mod, "build_qc_request") as build_qc:
            out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertNotIn("qc", out)
        build_qc.assert_not_called()

    def test_qc_disabled_via_dict_shape(self):
        from unittest import mock
        plan = self._montage_plan()
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        params = self._params(plan, qc={"enabled": False})
        gate = self._finish(proj, params)
        with mock.patch.object(_dom_auto_edit._montage_edit_mod, "build_qc_request") as build_qc:
            out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertNotIn("qc", out)
        build_qc.assert_not_called()

    def test_qc_never_blocks_finish_even_when_it_fails(self):
        from unittest import mock
        plan = self._montage_plan()
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        params = self._params(plan)
        gate = self._finish(proj, params)
        failing_qc = {"success": False, "error": "no frames could be extracted from the render for QC"}
        with mock.patch.object(_dom_auto_edit._montage_edit_mod, "build_qc_request",
                               return_value=failing_qc):
            out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["qc"], failing_qc)

    def test_qc_skipped_for_talking_head_plans(self):
        from unittest import mock
        plan = make_plan(self.root)  # talking-head (role="speech")
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        params = self._params(plan)
        gate = self._finish(proj, params)
        with mock.patch.object(_dom_auto_edit._montage_edit_mod, "build_qc_request") as build_qc:
            out = self._finish(proj, {**params, "confirm_token": gate["confirm_token"]})
        self.assertTrue(out.get("success"), out)
        self.assertNotIn("qc", out)
        build_qc.assert_not_called()


class CommitQCActionTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-commit-qc-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_commit_qc_normalizes_findings_and_saves_no_plan_mutation(self):
        segments = [
            cut_ir.make_cut_list_segment(
                role="montage_hook", clip_id="clip-0", clip_uuid="uuid-0",
                source_start_frame=0, source_end_frame=48),
            cut_ir.make_cut_list_segment(
                role="montage", clip_id="clip-1", clip_uuid="uuid-1",
                source_start_frame=0, source_end_frame=48),
        ]
        plan = cut_ir.make_cut_list(segments=segments, fps=24.0)
        auto_edit._assign_record_frames(plan)
        plan = edit_engine.save_plan(self.root, plan)
        out = run(s.auto_edit("commit_qc", {
            "plan_id": plan["plan_id"], "analysis_root": self.root,
            "qc_report": {"findings": [
                {"kind": "repeated_shot", "segment_index": 1, "why": "dup", "severity": "high"},
            ]},
        }))
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["suggested_edits"], [{"op": "drop", "index": 1}])

    def test_commit_qc_unknown_plan_is_honest(self):
        out = run(s.auto_edit("commit_qc", {
            "plan_id": "does-not-exist", "analysis_root": self.root, "qc_report": {"findings": []},
        }))
        self.assertFalse(out.get("success"))
        self.assertIn("error", out)


class PolishActionTest(unittest.TestCase):
    """polish_timeline() dispatch: the offline-reachable gates before the live
    export→drt-surgery→reimport round-trip (that round-trip is #13's live gate)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="auto-edit-polish-tool-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _mock_project(self, timeline_name="TL"):
        from unittest import mock
        tl = mock.Mock()
        tl.GetName.return_value = timeline_name
        proj = mock.Mock()
        proj.GetTimelineCount.return_value = 1
        proj.GetTimelineByIndex.return_value = tl
        return proj, tl

    def _polish(self, proj, params):
        from unittest import mock
        with mock.patch.object(_dom_auto_edit, "_destructive_versioning_provider",
            return_value=(None, proj, self.root, "P"),
        ):
            return run(s.auto_edit("polish_timeline", params))

    def _two_source_plan(self):
        # A source change between segment 0 (uuid-a) and 1 (uuid-b) ⇒ a dissolve.
        segments = [
            cut_ir.make_cut_list_segment(
                role="speech", clip_id="clip-a", clip_uuid="uuid-a",
                source_start_frame=0, source_end_frame=48, audio_track_indices=[1]),
            cut_ir.make_cut_list_segment(
                role="speech", clip_id="clip-b", clip_uuid="uuid-b",
                source_start_frame=0, source_end_frame=48, audio_track_indices=[1]),
        ]
        plan = cut_ir.make_cut_list(segments=segments, fps=24.0)
        auto_edit._assign_record_frames(plan)
        return edit_engine.save_plan(self.root, plan)

    def test_requires_built_timeline(self):
        plan = make_plan(self.root)
        auto_edit.mark_approved(self.root, plan["plan_id"])
        proj, _tl = self._mock_project()
        out = self._polish(proj, {"plan_id": plan["plan_id"]})
        self.assertIn("no built timeline",
                      out.get("error", {}).get("message", str(out)))

    def test_nothing_to_polish_for_single_source_cut(self):
        plan = make_plan(self.root)  # both segments are uuid-a, no story beats
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        out = self._polish(proj, {"plan_id": plan["plan_id"]})
        self.assertIn("nothing to polish",
                      out.get("error", {}).get("message", str(out)))
        self.assertIn("polish", out)  # the decision payload is attached

    @unittest.skipUnless(s._advanced_bridge.node_available(),
                         "node required: without it polish refuses before the token gate")
    def test_confirm_token_preview_lists_the_ops(self):
        plan = self._two_source_plan()
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        out = self._polish(proj, {"plan_id": plan["plan_id"]})
        # Node is available here, so we reach the checkpoint (before any Resolve export).
        self.assertEqual(out.get("status"), "confirmation_required")
        preview = out.get("preview") or {}
        self.assertEqual(preview.get("transitions"), 1)
        self.assertEqual(preview.get("built_timeline"), "TL")

    def test_honest_refusal_when_node_unavailable(self):
        from unittest import mock
        plan = self._two_source_plan()
        auto_edit.mark_approved(self.root, plan["plan_id"])
        edit_engine.mark_plan_executed(self.root, plan["plan_id"], {"timeline_name": "TL"})
        proj, _tl = self._mock_project()
        with mock.patch.object(s._advanced_bridge, "node_available", return_value=False):
            out = self._polish(proj, {"plan_id": plan["plan_id"]})
        self.assertIn("Node", out.get("error", {}).get("message", str(out)))


if __name__ == "__main__":
    unittest.main()
