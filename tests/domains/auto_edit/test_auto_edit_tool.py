"""Offline tests for the auto_edit compound tool (src/server.py).

No Resolve required: offline actions run against an explicit analysis_root;
the build-row assembler is pure. The live end-to-end path is validated by
tests/live_auto_edit_validation.py per the release process.
"""

from __future__ import annotations

import asyncio
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
