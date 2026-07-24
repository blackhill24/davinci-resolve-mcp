"""Offline tests for the montage genre wired into the auto_edit tool + its
shared execution (epic #38 P2 = issue #41).

Verifies — doesn't assume — that auto_edit's genre-agnostic execution
(apply_revision, approve_cut's ducking-force, cut-summary dispatch) works
correctly against montage-role CutLists, not just talking-head ones.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from unittest import mock

import src.server as s
from src.domains.auto_edit.utils import auto_edit, cut_ir, edit_engine


def run(coro):
    return asyncio.run(coro)


def make_montage_plan(root, *, n_segments=3, music=True):
    segments = [
        cut_ir.make_cut_list_segment(
            role="montage_hook", clip_id="hook-clip", clip_uuid="hook-uuid",
            source_start_frame=0, source_end_frame=36,
            rationale="select_potential rank 3, pacing=kinetic",
            evidence={"description": "Opening hook.", "pacing": "kinetic"}),
    ]
    for i in range(n_segments):
        segments.append(cut_ir.make_cut_list_segment(
            role="montage", clip_id=f"clip-{i}", clip_uuid=f"uuid-{i}",
            source_start_frame=0, source_end_frame=48,
            rationale="select_potential rank 2, pacing=moderate",
            evidence={"description": f"Shot {i}.", "pacing": "moderate"}))
    plan = cut_ir.make_cut_list(
        segments=segments, fps=24.0,
        music={"path": "/media/track.wav", "track_index": 2} if music else None)
    auto_edit._assign_record_frames(plan)
    plan["basis"] = "select_potential+pacing+beat_snap"
    plan["problems"] = []
    plan["tempo_bpm"] = 120.0
    plan["onset_count"] = 24
    return edit_engine.save_plan(root, plan)


class IsMontagePlanTests(unittest.TestCase):
    def test_detects_montage_role(self):
        root = tempfile.mkdtemp(prefix="montage-wiring-")
        self.addCleanup(shutil.rmtree, root, True)
        plan = make_montage_plan(root)
        self.assertTrue(s._is_montage_plan(plan))

    def test_talking_head_plan_not_montage(self):
        root = tempfile.mkdtemp(prefix="montage-wiring-")
        self.addCleanup(shutil.rmtree, root, True)
        seg = cut_ir.make_cut_list_segment(
            role="speech", clip_id="c", clip_uuid="u",
            source_start_frame=0, source_end_frame=48)
        plan = cut_ir.make_cut_list(segments=[seg], fps=24.0)
        self.assertFalse(s._is_montage_plan(plan))


class PlanCutDispatchTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="montage-wiring-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_montage_genre_dispatches_to_montage_edit(self):
        brief = auto_edit.create_brief(
            self.root, files=["/media/b1.mp4"], music="/media/track.wav", genre="montage")
        self.assertTrue(brief["success"], brief)
        auto_edit.advance_brief(self.root, brief["brief_id"], "ready")
        fake_plan = make_montage_plan(self.root)
        with mock.patch.object(
                s._montage_edit_mod, "build_cut_list_for_brief",
                return_value={"success": True, "plan": fake_plan}) as mocked, \
             mock.patch.object(s._auto_edit_mod, "build_cut_list_for_brief") as mocked_talking_head:
            out = run(s.auto_edit("plan_cut", {"brief_id": brief["brief_id"], "analysis_root": self.root}))
        self.assertTrue(out.get("success"), out)
        mocked.assert_called_once()
        mocked_talking_head.assert_not_called()
        self.assertIn("Montage cut list", out["summary"])

    def test_talking_head_genre_still_dispatches_to_auto_edit(self):
        brief = auto_edit.create_brief(self.root, files=["/media/talk.mp4"], genre="talking_head")
        self.assertTrue(brief["success"], brief)
        auto_edit.advance_brief(self.root, brief["brief_id"], "ready")
        with mock.patch.object(s._montage_edit_mod, "build_cut_list_for_brief") as mocked_montage, \
             mock.patch.object(s._auto_edit_mod, "build_cut_list_for_brief",
                                return_value={"success": False, "error": "no speech"}) as mocked:
            out = run(s.auto_edit("plan_cut", {"brief_id": brief["brief_id"], "analysis_root": self.root}))
        mocked.assert_called_once()
        mocked_montage.assert_not_called()
        self.assertIn("error", out)


class ReviseCutOnMontageTests(unittest.TestCase):
    """apply_revision is genre-agnostic (operates on segment structure only)
    — verify it actually works against montage roles, don't just assume."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="montage-wiring-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_drop_a_montage_segment(self):
        plan = make_montage_plan(self.root, n_segments=3)
        out = auto_edit.apply_revision(self.root, plan["plan_id"], notes="drop one", edits=[
            {"op": "drop", "index": 1},
        ])
        self.assertTrue(out["success"], out)
        revised = out["plan"]
        self.assertEqual(len(revised["segments"]), 3)  # hook + 3 - 1 dropped
        self.assertEqual(revised["segments"][0]["role"], "montage_hook")
        self.assertTrue(all(s["role"] == "montage" for s in revised["segments"][1:]))

    def test_reorder_montage_segments(self):
        plan = make_montage_plan(self.root, n_segments=3)
        order = list(range(len(plan["segments"])))
        order[1], order[2] = order[2], order[1]
        out = auto_edit.apply_revision(self.root, plan["plan_id"], notes="reorder", edits=[
            {"op": "reorder", "order": order},
        ])
        self.assertTrue(out["success"], out)

    def test_revise_cut_tool_action_uses_montage_summary(self):
        plan = make_montage_plan(self.root, n_segments=2)
        brief = auto_edit.create_brief(
            self.root, files=["/media/b1.mp4"], music="/media/track.wav", genre="montage")
        auto_edit.advance_brief(self.root, brief["brief_id"], "ready")
        auto_edit.advance_brief(self.root, brief["brief_id"], "planned", latest_plan_id=plan["plan_id"])
        out = run(s.auto_edit("revise_cut", {
            "brief_id": brief["brief_id"], "plan_id": plan["plan_id"],
            "notes": "drop one", "edits": [{"op": "drop", "index": 1}],
            "analysis_root": self.root,
        }))
        self.assertTrue(out.get("success"), out)
        self.assertIn("Montage cut list", out["summary"])


class ApproveCutMontageDuckingTests(unittest.TestCase):
    """approve_cut must never honor a ducking-consent flag for montage —
    music.ducking.mode must stay static regardless of what's passed."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="montage-wiring-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _approve(self, plan_id, extra=None):
        params = {"plan_id": plan_id, "analysis_root": self.root}
        params.update(extra or {})
        return run(s.auto_edit("approve_cut", params))

    def test_music_bed_consent_ignored_for_montage(self):
        plan = make_montage_plan(self.root, music=True)
        first = self._approve(plan["plan_id"], {"music_bed_consent": True})
        self.assertEqual(first.get("status"), "confirmation_required")
        self.assertNotIn("music_bed_consent_line", first["preview"])
        second = self._approve(plan["plan_id"], {
            "music_bed_consent": True, "confirm_token": first["confirm_token"]})
        self.assertTrue(second.get("success"), second)
        stored = edit_engine.load_plan(self.root, plan["plan_id"])
        self.assertEqual(stored["music"]["ducking"]["mode"], cut_ir.DUCKING_STATIC)
        self.assertFalse(stored["music"]["ducking"]["user_approved_render"])

    def test_prefer_drt_ducking_also_ignored_for_montage(self):
        plan = make_montage_plan(self.root, music=True)
        first = self._approve(plan["plan_id"], {"prefer_drt_ducking": True})
        second = self._approve(plan["plan_id"], {
            "prefer_drt_ducking": True, "confirm_token": first["confirm_token"]})
        self.assertTrue(second.get("success"), second)
        stored = edit_engine.load_plan(self.root, plan["plan_id"])
        self.assertEqual(stored["music"]["ducking"]["mode"], cut_ir.DUCKING_STATIC)


class GetCutSummaryMontageTests(unittest.TestCase):
    def test_montage_plan_uses_montage_summary(self):
        root = tempfile.mkdtemp(prefix="montage-wiring-")
        self.addCleanup(shutil.rmtree, root, True)
        plan = make_montage_plan(root)
        out = run(s.auto_edit("get_cut_summary", {"plan_id": plan["plan_id"], "analysis_root": root}))
        self.assertTrue(out.get("success"), out)
        self.assertIn("Montage cut list", out["summary"])
        self.assertNotIn("Excerpt", out["summary"])


if __name__ == "__main__":
    unittest.main()
