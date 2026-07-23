"""Unit tests for src/utils/auto_edit.py (the auto_edit decision layer).

No Resolve required: the decision layer is DB-only by design (same posture as
test_edit_engine.py). Seeds the analysis DB via analysis_store.ingest_report
and injects a fake similarity index for b-roll matching.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from src.core import timeline_brain_db
from src.utils import auto_edit, cut_ir, edit_engine, analysis_store

from tests.test_analysis_store import make_report

FPS = 24.0


def _word(word, start, end):
    return {"word": word, "start": start, "end": end, "probability": 0.9}


def talking_head_transcription():
    """Two speech regions (0-4s, 8-12s) with one filler; 4s of dead air between."""
    return {
        "success": True,
        "segments": [
            {"start": 0.0, "end": 4.0, "text": "welcome to the show um let's begin",
             "words": [
                 _word("welcome", 0.0, 0.5), _word("to", 0.6, 0.7),
                 _word("the", 0.8, 0.9), _word("show", 1.0, 1.4),
                 _word("um", 2.0, 2.4),
                 _word("let's", 2.5, 2.8), _word("begin", 2.9, 3.5),
             ]},
            {"start": 8.0, "end": 12.0, "text": "here is the second thought",
             "words": [
                 _word("here", 8.0, 8.3), _word("is", 8.4, 8.5),
                 _word("the", 8.6, 8.7), _word("second", 8.8, 9.3),
                 _word("thought", 9.4, 10.0),
             ]},
        ],
    }


class AutoEditBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="auto-edit-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(timeline_brain_db.close_all)

    def _ingest(self, *, clip_id, name, path, clip_dir, transcription=None):
        report = make_report()
        report["clip"] = dict(report["clip"], clip_id=clip_id, clip_name=name,
                              file_path=path, media_id=clip_id + "-m")
        if transcription is not None:
            report["transcription"] = transcription
        else:
            report["transcription"] = {"success": False, "segments": []}
        result = analysis_store.ingest_report(self.root, report, clip_dir=clip_dir)
        self.assertTrue(result["success"], result)
        return result["clip_uuid"]

    def _seed_talking_head(self, **brief_overrides):
        self.speech_uuid = self._ingest(
            clip_id="resolve-talk-1", name="Talk.mp4", path="/media/talk.mp4",
            clip_dir="t-tttttttttttt", transcription=talking_head_transcription())
        kwargs = dict(files=["/media/talk.mp4"], target_duration_seconds=None,
                      genre="talking_head", title_text="My Interview")
        kwargs.update(brief_overrides)
        created = auto_edit.create_brief(self.root, **kwargs)
        self.assertTrue(created["success"], created)
        return created["brief"]

    @staticmethod
    def _no_match(*args, **kwargs):
        return {"success": True, "results": []}


class BriefValidationTest(AutoEditBase):
    def test_valid_brief_persists(self):
        brief = self._seed_talking_head()
        self.assertEqual(brief["state"], "created")
        loaded = auto_edit.load_brief(self.root, brief["plan_id"])
        self.assertEqual(loaded["files"], ["/media/talk.mp4"])

    def test_montage_genre_accepted(self):
        # auto_edit.py stays unaware of montage's OWN rules (e.g. music
        # required) — that's montage_edit.validate_montage_brief_inputs' job,
        # called separately in server.py's start_brief dispatch. This module
        # only needs to accept the genre string.
        out = auto_edit.create_brief(self.root, files=["/media/broll.mp4"], genre="montage")
        self.assertTrue(out["success"], out)

    def test_invalid_briefs_rejected(self):
        for kwargs, needle in [
            (dict(files=[]), "non-empty list"),
            # "montage" became a valid genre string once montage_edit shipped
            # (epic #38) — GENRES now only rejects genres with no planner at all.
            (dict(files=["/a.mp4"], genre="narrative"), "genre"),
            (dict(files=["/a.mp4"], target_duration_seconds=-3), "positive"),
            (dict(files=["/a.mp4"], music=""), "music"),
        ]:
            out = auto_edit.create_brief(self.root, **kwargs)
            self.assertFalse(out["success"], kwargs)
            self.assertIn(needle, "\n".join(out["problems"]))

    def test_state_machine_transitions(self):
        brief = self._seed_talking_head()
        bid = brief["plan_id"]
        self.assertTrue(auto_edit.advance_brief(self.root, bid, "ready")["success"])
        self.assertTrue(auto_edit.advance_brief(self.root, bid, "planned")["success"])
        # planned -> planned (revision) is legal
        self.assertTrue(auto_edit.advance_brief(self.root, bid, "planned")["success"])
        # skipping straight to finished is not
        out = auto_edit.advance_brief(self.root, bid, "finished")
        self.assertFalse(out["success"])
        self.assertIn("illegal transition", out["error"])


class BuildCutListTest(AutoEditBase):
    def test_segments_are_half_open_and_skip_fillers(self):
        brief = self._seed_talking_head()
        out = auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=self._no_match)
        self.assertTrue(out["success"], out)
        plan = out["plan"]
        self.assertEqual(plan["kind"], cut_ir.CUT_LIST_KIND)
        self.assertEqual(cut_ir.validate_cut_list(plan), [])
        # Window 0-4s split around the "um" (2.0-2.4s) + window 8-12s:
        # segments (0, 2.0), (2.4, 4.0), (8.0, 12.0) in frames at 24fps.
        spans = [(s["source_start_frame"], s["source_end_frame"])
                 for s in plan["segments"]]
        self.assertEqual(spans, [(0, 48), (58, 96), (192, 288)])
        # The filler cut is recorded as evidence.
        kinds = [c["kind"] for c in plan["removed"]]
        self.assertIn("filler", kinds)
        self.assertEqual(plan["basis"], "words")

    def test_record_frames_walk_the_cursor(self):
        brief = self._seed_talking_head()
        plan = auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=self._no_match)["plan"]
        starts = [s["record_start_frame"] for s in plan["segments"]]
        # durations: 48, 38, 96 -> cursor 0, 48, 86
        self.assertEqual(starts, [0, 48, 86])
        self.assertEqual(plan["record_duration_frames"], 48 + 38 + 96)

    def test_jump_cut_smoothing_defaults_to_punch_in(self):
        brief = self._seed_talking_head()
        plan = auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=self._no_match)["plan"]
        # Second piece of window 0 is a jump cut; without b-roll it punches in.
        seg = plan["segments"][1]
        self.assertEqual(seg["jumpcut_smoothing"], "punch_in")
        self.assertEqual(seg["punch_in"]["zoom"], auto_edit.PUNCH_IN_ZOOM)
        self.assertNotIn("jumpcut_smoothing", plan["segments"][0])

    def test_broll_overlay_from_similarity_match(self):
        self.broll_uuid = self._ingest(
            clip_id="resolve-broll-1", name="Broll.mp4", path="/media/broll.mp4",
            clip_dir="b-bbbbbbbbbbbb")
        brief = self._seed_talking_head(files=["/media/talk.mp4", "/media/broll.mp4"])

        def fake_similar(project_root, **kwargs):
            return {"success": True, "results": [{
                "score": 0.91, "entity_type": "shot", "clip_uuid": self.broll_uuid,
                "clip_name": "Broll.mp4", "shot_index": 1, "time_seconds_start": 5.0,
            }]}

        plan = auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=fake_similar)["plan"]
        seg = plan["segments"][1]
        self.assertEqual(seg["jumpcut_smoothing"], "broll")
        self.assertEqual(len(plan["overlays"]), 1)
        overlay = plan["overlays"][0]
        self.assertEqual(overlay["track_index"], 2)
        self.assertEqual(overlay["clip_uuid"], self.broll_uuid)
        # Overlay covers the head of the jump-cut segment.
        self.assertEqual(overlay["record_start_frame"], seg["record_start_frame"])
        self.assertEqual(
            overlay["record_end_frame"] - overlay["record_start_frame"],
            overlay["duration_frames"])

    def test_duration_fit_drops_whole_segments(self):
        brief = self._seed_talking_head(target_duration_seconds=2.0)
        plan = auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=self._no_match)["plan"]
        self.assertEqual(len(plan["segments"]), 1)
        self.assertEqual(plan["estimates"]["duration_frames"], 48)
        fits = [c for c in plan["removed"]
                if (c.get("evidence") or {}).get("reason") == "duration_fit"]
        self.assertEqual(len(fits), 2)

    def test_music_trimmed_to_cut_length(self):
        brief = self._seed_talking_head(music="/media/song.wav")
        plan = auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=self._no_match, music_gain_db=-11.7)["plan"]
        music = plan["music"]
        self.assertEqual(music["record_start_frame"], 0)
        self.assertEqual(music["record_end_frame"], plan["record_duration_frames"])
        self.assertEqual(music["gain_db"], -11.7)
        self.assertEqual(music["ducking"],
                         {"mode": "static", "user_approved_render": False})

    def test_unanalyzed_brief_fails_honestly(self):
        created = auto_edit.create_brief(self.root, files=["/media/nothing.mp4"])
        out = auto_edit.build_cut_list_for_brief(
            self.root, created["brief"], similar_fn=self._no_match)
        self.assertFalse(out["success"])
        self.assertIn("no transcribed speech", out["error"])


class SummaryTest(AutoEditBase):
    def test_summary_renders_checkpoint_content(self):
        brief = self._seed_talking_head(music="/media/song.wav",
                                        title_text="My Interview")
        plan = auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=self._no_match)["plan"]
        text = auto_edit.render_cut_summary(plan)
        self.assertIn("Cut list — revision 0", text)
        self.assertIn("welcome to the show", text)          # excerpt
        self.assertIn("filler", text)                        # removed summary
        self.assertIn("My Interview", text)                  # title
        self.assertIn("song.wav", text)                      # music
        self.assertIn(auto_edit.MUSIC_BED_CONSENT_LINE, text)  # THE consent line


class ApprovalTest(AutoEditBase):
    def _plan(self, **brief_overrides):
        brief = self._seed_talking_head(**brief_overrides)
        return auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=self._no_match)["plan"]

    def test_executor_requires_approval(self):
        plan = self._plan()
        gate = auto_edit.require_approved_plan(self.root, plan["plan_id"])
        self.assertFalse(gate["success"])
        self.assertIn("not approved", gate["error"])
        auto_edit.mark_approved(self.root, plan["plan_id"])
        gate = auto_edit.require_approved_plan(self.root, plan["plan_id"])
        self.assertTrue(gate["success"])

    def test_music_consent_gates_ducking_mode(self):
        plan = self._plan(music="/media/song.wav")
        approved = auto_edit.mark_approved(
            self.root, plan["plan_id"], music_bed_consent=False)["plan"]
        self.assertEqual(approved["music"]["ducking"]["mode"], "static")
        self.assertFalse(approved["music"]["ducking"]["user_approved_render"])
        plan2 = self._plan(music="/media/song.wav")
        approved2 = auto_edit.mark_approved(
            self.root, plan2["plan_id"], music_bed_consent=True)["plan"]
        self.assertEqual(approved2["music"]["ducking"]["mode"], "rendered_bed")
        self.assertTrue(approved2["music"]["ducking"]["user_approved_render"])

    def test_fingerprint_gating_blocks_tampered_plan(self):
        plan = self._plan()
        auto_edit.mark_approved(self.root, plan["plan_id"])
        path = os.path.join(edit_engine._plan_dir(self.root),
                            f"{plan['plan_id']}.json")
        with open(path, "r+", encoding="utf-8") as handle:
            data = json.load(handle)
            data["segments"][0]["source_end_frame"] = 9999
            handle.seek(0)
            json.dump(data, handle)
            handle.truncate()
        gate = auto_edit.require_approved_plan(self.root, plan["plan_id"])
        self.assertFalse(gate["success"])
        self.assertIn("fingerprint", gate["error"])


class RevisionTest(AutoEditBase):
    def _plan(self):
        brief = self._seed_talking_head()
        return auto_edit.build_cut_list_for_brief(
            self.root, brief, similar_fn=self._no_match)["plan"]

    def test_drop_and_keep_round_trip(self):
        plan = self._plan()
        dropped = auto_edit.apply_revision(
            self.root, plan["plan_id"], notes="lose the aside",
            edits=[{"op": "drop", "index": 1}])
        self.assertTrue(dropped["success"], dropped)
        rev1 = dropped["plan"]
        self.assertEqual(rev1["revision"], 1)
        self.assertEqual(len(rev1["segments"]), 2)
        self.assertEqual(rev1["revised_from"], plan["plan_id"])
        # Old revision still loads intact (append-rebuild history).
        self.assertEqual(
            len(edit_engine.load_plan(self.root, plan["plan_id"])["segments"]), 3)
        restored = auto_edit.apply_revision(
            self.root, rev1["plan_id"], edits=[{"op": "keep"}])
        self.assertTrue(restored["success"], restored)
        self.assertEqual(len(restored["plan"]["segments"]), 3)

    def test_reorder_validates_permutation(self):
        plan = self._plan()
        bad = auto_edit.apply_revision(
            self.root, plan["plan_id"], edits=[{"op": "reorder", "order": [0, 0, 1]}])
        self.assertFalse(bad["success"])
        good = auto_edit.apply_revision(
            self.root, plan["plan_id"], edits=[{"op": "reorder", "order": [2, 0, 1]}])
        self.assertTrue(good["success"], good)
        first = good["plan"]["segments"][0]
        self.assertEqual(first["source_start_frame"], 192)
        # Record frames recomputed for the new order.
        self.assertEqual(first["record_start_frame"], 0)

    def test_title_edit_and_unknown_op(self):
        plan = self._plan()
        titled = auto_edit.apply_revision(
            self.root, plan["plan_id"], edits=[{"op": "title", "text": "Better Title"}])
        self.assertTrue(titled["success"])
        self.assertEqual(titled["plan"]["titles"][0]["text"], "Better Title")
        out = auto_edit.apply_revision(
            self.root, plan["plan_id"], edits=[{"op": "explode"}])
        self.assertFalse(out["success"])

    def test_revision_cannot_empty_the_cut(self):
        plan = self._plan()
        out = auto_edit.apply_revision(
            self.root, plan["plan_id"],
            edits=[{"op": "drop", "index": 0}, {"op": "drop", "index": 0},
                   {"op": "drop", "index": 0}])
        self.assertFalse(out["success"])
        self.assertIn("no segments", out["error"])

    def test_multi_drop_uses_displayed_indices(self):
        plan = self._plan()
        self.assertEqual(len(plan["segments"]), 3)
        survivor = plan["segments"][1]["source_start_frame"]
        # Ascending indices must still remove the segments shown at 0 and 2 —
        # the drop-only batch is normalized high→low internally.
        out = auto_edit.apply_revision(
            self.root, plan["plan_id"],
            edits=[{"op": "drop", "index": 0}, {"op": "drop", "index": 2}])
        self.assertTrue(out["success"], out)
        kept = out["plan"]["segments"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["source_start_frame"], survivor)

    def test_multi_drop_mixed_with_reorder_must_be_descending(self):
        plan = self._plan()
        bad = auto_edit.apply_revision(
            self.root, plan["plan_id"],
            edits=[{"op": "reorder", "order": [2, 0, 1]},
                   {"op": "drop", "index": 0}, {"op": "drop", "index": 2}])
        self.assertFalse(bad["success"])
        self.assertIn("descending", bad["error"])


if __name__ == "__main__":
    unittest.main()
