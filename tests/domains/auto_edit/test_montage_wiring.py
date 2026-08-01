"""Offline tests for the montage genre wired into the auto_edit tool + its
shared execution (epic #38 P2 = issue #41).

Verifies — doesn't assume — that auto_edit's genre-agnostic execution
(apply_revision, approve_cut's ducking-force, cut-summary dispatch) works
correctly against montage-role CutLists, not just talking-head ones.
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import src.server as s
from src.core import timeline_brain_db
from src.domains.auto_edit.utils import auto_edit, cut_ir, edit_engine, montage_edit


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


class ScoutHandoffWiringTests(unittest.TestCase):
    """plan_cut's in-point scouting offer (issue #178, phase 3/6): on by
    default, an escape hatch, and never re-offered once a shot is scouted
    (cache hit — no re-scout on a later revision)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="montage-scout-wiring-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(timeline_brain_db.close_all)

    def _seed_clip(self):
        from tests.domains.media_analysis.test_analysis_store import make_report
        from src.domains.media_analysis.utils import analysis_store
        report = make_report()
        report["clip"] = dict(report["clip"], file_path="/media/b1.mp4", clip_name="B1.mp4")
        result = analysis_store.ingest_report(self.root, report, clip_dir="b1-dir")
        self.assertTrue(result["success"], result)
        return result["clip_uuid"]

    def test_unscouted_montage_brief_offers_scout_before_planning(self):
        self._seed_clip()
        brief = auto_edit.create_brief(
            self.root, files=["/media/b1.mp4"], music="/media/track.wav", genre="montage")
        auto_edit.advance_brief(self.root, brief["brief_id"], "ready")
        with mock.patch.object(s._montage_edit_mod, "build_cut_list_for_brief") as mocked_build:
            out = run(s.auto_edit("plan_cut", {"brief_id": brief["brief_id"], "analysis_root": self.root}))
        mocked_build.assert_not_called()
        self.assertEqual(out.get("status"), "confirmation_required")
        self.assertIn("estimate", out)

    def test_scout_false_skips_the_offer(self):
        self._seed_clip()
        brief = auto_edit.create_brief(
            self.root, files=["/media/b1.mp4"], music="/media/track.wav", genre="montage")
        auto_edit.advance_brief(self.root, brief["brief_id"], "ready")
        fake_plan = make_montage_plan(self.root)
        with mock.patch.object(
                s._montage_edit_mod, "build_cut_list_for_brief",
                return_value={"success": True, "plan": fake_plan}) as mocked_build:
            out = run(s.auto_edit(
                "plan_cut", {"brief_id": brief["brief_id"], "analysis_root": self.root, "scout": False}))
        mocked_build.assert_called_once()
        self.assertTrue(out.get("success"), out)

    def test_already_scouted_shots_issue_no_new_handoff(self):
        from src.domains.media_analysis.utils import analysis_store
        clip_uuid = self._seed_clip()
        # Simulate a prior, already-committed scout pass on every shot.
        report = analysis_store.export_report(self.root, clip_uuid)
        for shot in report["visual"]["shot_descriptions"]:
            shot["scout"] = [{"window_start_seconds": shot["time_seconds_start"],
                               "window_end_seconds": shot["time_seconds_end"],
                               "in_point_seconds": shot["time_seconds_start"] + 0.1,
                               "subject_clarity": "high", "motion_interest": "medium",
                               "composition": "high", "exposure": "good",
                               "dominant_colour": {"tone": "warm", "brightness": 0.5},
                               "usable": True, "why": "already scouted"}]
        ingest = analysis_store.ingest_report(self.root, report, clip_dir="b1-dir")
        self.assertTrue(ingest["success"], ingest)

        brief = auto_edit.create_brief(
            self.root, files=["/media/b1.mp4"], music="/media/track.wav", genre="montage")
        auto_edit.advance_brief(self.root, brief["brief_id"], "ready")
        fake_plan = make_montage_plan(self.root)
        with mock.patch.object(
                s._montage_edit_mod, "build_cut_list_for_brief",
                return_value={"success": True, "plan": fake_plan}) as mocked_build, \
             mock.patch("src.domains.media_analysis.utils.deep_vision.deepen_clip") as mocked_deepen:
            out = run(s.auto_edit("plan_cut", {"brief_id": brief["brief_id"], "analysis_root": self.root}))
        mocked_deepen.assert_not_called()
        mocked_build.assert_called_once()
        self.assertTrue(out.get("success"), out)


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

    def _grid_locked_plan(self):
        """A grid-locked montage plan whose cuts sit on REAL beat frames.

        Detected beats are not uniformly spaced in frames (108 BPM @ 24fps is
        13.31 frames/beat, rounded per beat), which is exactly why an
        accumulate-walk cannot reproduce them once a segment leaves.
        """
        beat_frames = [0, 13, 27, 40, 53, 67, 80, 93, 107]
        segments = []
        for i in range(0, 8, 2):
            start, end = beat_frames[i], beat_frames[i + 2]
            segments.append(cut_ir.make_cut_list_segment(
                role="montage_hook" if i == 0 else "montage",
                clip_id=f"clip-{i}", clip_uuid=f"uuid-{i}",
                source_start_frame=1000 + start, source_end_frame=1000 + end))
            segments[-1]["record_start_frame"] = start
        plan = cut_ir.make_cut_list(
            segments=segments, fps=24.0,
            music={"path": "/media/track.wav", "track_index": 2})
        plan["grid_available"] = True
        plan["problems"] = []
        plan["record_duration_frames"] = beat_frames[8]
        return edit_engine.save_plan(self.root, plan), beat_frames

    def test_drop_on_a_grid_locked_montage_reports_the_lost_beat_lock(self):
        plan, beat_frames = self._grid_locked_plan()
        out = auto_edit.apply_revision(self.root, plan["plan_id"], notes="drop", edits=[
            {"op": "drop", "index": 1},
        ])
        self.assertTrue(out["success"], out)
        revised = out["plan"]
        # The damage is real, not hypothetical: the last cut no longer lands on
        # a beat once the walk re-packs it.
        starts = [s["record_start_frame"] for s in revised["segments"]]
        self.assertFalse(all(f in beat_frames for f in starts), starts)
        self.assertTrue(revised.get("beat_lock_broken"))
        self.assertTrue(any("beat lock" in p for p in revised["problems"]), revised["problems"])
        # and the checkpoint summary the user is shown carries it
        self.assertIn("beat lock", montage_edit.render_montage_summary(revised))

    def test_title_only_revision_keeps_the_beat_lock(self):
        plan, beat_frames = self._grid_locked_plan()
        out = auto_edit.apply_revision(self.root, plan["plan_id"], notes="title", edits=[
            {"op": "title", "text": "Reel"},
        ])
        self.assertTrue(out["success"], out)
        revised = out["plan"]
        self.assertNotIn("beat_lock_broken", revised)
        self.assertEqual([s["record_start_frame"] for s in revised["segments"]],
                         [beat_frames[i] for i in (0, 2, 4, 6)])
        self.assertEqual(revised["problems"], [])

    def test_fallback_montage_revision_claims_no_lost_lock(self):
        # grid_available False — there was never a lock to lose, so a drop must
        # not manufacture a warning.
        plan = make_montage_plan(self.root, n_segments=3)
        out = auto_edit.apply_revision(self.root, plan["plan_id"], notes="drop", edits=[
            {"op": "drop", "index": 1},
        ])
        self.assertTrue(out["success"], out)
        self.assertNotIn("beat_lock_broken", out["plan"])
        self.assertEqual(out["plan"]["problems"], [])

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


class TitleRevisionBeatLockTests(unittest.TestCase):
    """#193 phase 2.1 / 3.1 — a title-only revision must keep the beat lock.

    ``apply_revision`` re-walks record frames from ``cursor = 0`` for EVERY op,
    including ``title``. Against a cut that started at ``round(beat_zero *
    fps)`` the walk shifted everything and set ``beat_lock_broken`` — so the
    one revision every montage host makes (a title is the only way a montage
    gets one) always reported the lock as lost. Now that the cut starts at
    frame 0 and the arrangement schedule is contiguous, the walk is a genuine
    no-op and the flag stays correctly unset.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="montage-titlelock-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _grid_locked_plan(self, *, phase_frames):
        """A grid-locked montage plan whose segments are contiguous from
        `phase_frames` — i.e. exactly the shape the planner produced BEFORE
        normalisation when `phase_frames > 0`, and after it when 0."""
        segments = []
        cursor = phase_frames
        for i in range(4):
            seg = cut_ir.make_cut_list_segment(
                role="montage_hook" if i == 0 else "montage",
                clip_id=f"clip-{i}", clip_uuid=f"uuid-{i}",
                source_start_frame=0, source_end_frame=24,
                rationale="select_potential rank 3, pacing=kinetic",
                evidence={"description": f"Shot {i}.", "pacing": "kinetic"})
            seg["record_start_frame"] = cursor
            seg["record_length_frames"] = 24
            cursor += 24
            segments.append(seg)
        plan = cut_ir.make_cut_list(
            segments=segments, fps=24.0,
            music={"path": "/media/track.wav", "track_index": 2})
        plan["grid_available"] = True
        plan["problems"] = []
        plan["tempo_bpm"] = 120.0
        return edit_engine.save_plan(self.root, plan)

    def _title_revision(self, plan):
        return auto_edit.apply_revision(
            self.root, plan["plan_id"], notes="add a title",
            edits=[{"op": "title", "text": "My Montage"}])

    def test_title_only_revision_keeps_the_beat_lock(self):
        plan = self._grid_locked_plan(phase_frames=0)
        out = self._title_revision(plan)
        self.assertTrue(out.get("success"), out)
        revised = out["plan"]
        self.assertFalse(revised.get("beat_lock_broken"),
                         f"title-only revision broke the lock: {revised.get('problems')}")
        self.assertEqual(revised["segments"][0]["record_start_frame"], 0)

    def test_a_phase_offset_cut_is_what_used_to_break_it(self):
        # Documents the mechanism rather than asserting the old bug is still
        # present: a cut that does NOT start at 0 cannot survive the walk, and
        # that is precisely why normalize_grid_phase exists.
        plan = self._grid_locked_plan(phase_frames=9)
        out = self._title_revision(plan)
        self.assertTrue(out.get("success"), out)
        self.assertTrue(out["plan"].get("beat_lock_broken"))


class PolishedItemMappingTests(unittest.TestCase):
    """#193 phase 6.2.5 — positional item mapping breaks on a polished timeline.

    A cross-dissolve is itself a V1 item, so `item i -> segment i - offset`
    gave every shot after the first dissolve another shot's look bucket and
    beat directive — and SKILL.md actively steers hosts into
    `finish(target="polished", grade={"match": ...}, motion={})`, the exact
    combination that trips it.
    """

    class _Item:
        """Faithful enough for the mapping: real integer start frames, which
        is what the record-frame match needs and what Resolve returns."""

        def __init__(self, start, name="clip"):
            self._start = start
            self.name = name

        def GetStart(self):
            return self._start

    def _segments(self, n, *, length=24):
        segs = []
        for i in range(n):
            seg = cut_ir.make_cut_list_segment(
                role="montage", clip_id=f"c{i}", clip_uuid=f"u{i}",
                source_start_frame=0, source_end_frame=length)
            seg["record_start_frame"] = i * length
            seg["record_length_frames"] = length
            seg["look_bucket"] = f"bucket-{i}"
            segs.append(seg)
        return segs

    def _map(self, items, segments, title_offset=0):
        return list(s._map_montage_items_to_segments(
            items, segments, title_offset, record_offset=title_offset))

    def test_built_timeline_maps_one_to_one(self):
        segments = self._segments(4)
        items = [self._Item(i * 24) for i in range(4)]
        pairs = self._map(items, segments)
        self.assertEqual([p[1] for p in pairs], [0, 1, 2, 3])
        self.assertEqual([p[2]["look_bucket"] for p in pairs],
                         ["bucket-0", "bucket-1", "bucket-2", "bucket-3"])

    def test_polished_timeline_transition_item_does_not_shift_every_later_shot(self):
        segments = self._segments(4)
        # V1 after polish: the clips, plus a cross-dissolve item sitting at a
        # frame that is not any segment's start.
        items = [
            self._Item(0), self._Item(24),
            self._Item(36, "Cross Dissolve"),          # the extra item
            self._Item(48), self._Item(72),
        ]
        pairs = self._map(items, segments)
        # Every segment is still paired with ITS OWN clip — the transition
        # drops out instead of consuming one.
        self.assertEqual([p[1] for p in pairs], [0, 1, 2, 3])
        for _item, idx, seg in pairs:
            self.assertEqual(seg["look_bucket"], f"bucket-{idx}")

    def test_intro_title_offset_is_honoured(self):
        segments = self._segments(3)
        title_len = 96
        items = [self._Item(0, "title")] + [
            self._Item(title_len + i * 24) for i in range(3)]
        pairs = self._map(items, segments, title_offset=title_len)
        self.assertEqual([p[1] for p in pairs], [0, 1, 2])

    def test_falls_back_to_positional_when_starts_are_unreadable(self):
        # A double (or a Resolve build) that can't report frames must not map
        # against Nones — the proven positional walk still applies.
        class _Opaque:
            def GetStart(self):
                return None

        segments = self._segments(3)
        pairs = self._map([_Opaque() for _ in range(3)], segments)
        self.assertEqual([p[1] for p in pairs], [0, 1, 2])


class MontageCameraAudioTests(unittest.TestCase):
    """#193 phase 5.1 — camera audio used to land on A1 under the music.

    Montage segments never set `audio_track_indices`, so the `or [1]` fallback
    mirrored every B-roll shot's production audio onto A1 at unity gain,
    straight under the music on A2. Nothing in the plan, the summary or the
    skill mentioned it and there was no knob.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="montage-audio-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_montage_segments_emit_no_audio_rows(self):
        plan = make_montage_plan(self.root, music=True)
        rows = s._auto_edit_build_rows(plan)
        audio = [r for r in rows if r["media_type"] == 2 and r["role"] == "speech_audio"]
        self.assertEqual(audio, [], "montage still mirrors camera audio onto A1")
        # the picture rows are untouched
        video = [r for r in rows if r["media_type"] == 1]
        self.assertEqual(len(video), len(plan["segments"]))

    def test_explicit_audio_track_indices_opt_back_in(self):
        # A host that genuinely wants nat sound on a shot can still ask.
        plan = make_montage_plan(self.root, music=True)
        plan["segments"][1]["audio_track_indices"] = [1]
        rows = s._auto_edit_build_rows(plan)
        audio = [r for r in rows if r["role"] == "speech_audio"]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]["track_index"], 1)

    def test_talking_head_audio_mirroring_is_unchanged(self):
        # The fallback must keep working for the genre it was written for.
        segments = [
            cut_ir.make_cut_list_segment(
                role="speech", clip_id="a", clip_uuid="ua",
                source_start_frame=0, source_end_frame=48, audio_track_indices=[1]),
            cut_ir.make_cut_list_segment(
                role="speech", clip_id="a", clip_uuid="ua",
                source_start_frame=48, source_end_frame=96),
        ]
        plan = cut_ir.make_cut_list(segments=segments, fps=24.0)
        auto_edit._assign_record_frames(plan)
        plan = edit_engine.save_plan(self.root, plan)
        rows = s._auto_edit_build_rows(plan)
        audio = [r for r in rows if r["role"] == "speech_audio"]
        self.assertEqual(len(audio), 2)  # incl. the one relying on the `or [1]` fallback


class MontageExportAudioTests(unittest.TestCase):
    """#193 phase 5.4 — for a music-cut montage the audio IS the deliverable.

    `render(build_proxies)` writes ExportAudio=False project-wide (deliberately
    — it dodges the headless Fairlight/PipeWire 0%-stall) and never restores
    it, so a proxy build earlier in the same session left the next montage
    render silent with every check green.
    """

    def test_finish_asserts_export_audio_for_montage(self):
        source = (pathlib.Path(__file__).resolve().parents[3] / "src" / "domains"
                  / "auto_edit" / "actions.py").read_text(encoding="utf-8")
        self.assertIn('render_settings.setdefault("ExportAudio", True)', source)
        # ...and only for montage, so talking-head keeps whatever it had: the
        # setdefault must sit under a montage guard, not at the top level.
        preceding = source.split(
            'render_settings.setdefault("ExportAudio", True)')[0].splitlines()
        guard = next(line for line in reversed(preceding) if line.strip()
                     and not line.strip().startswith("#"))
        self.assertEqual(guard.strip(), "if _is_montage_plan(plan):")

    def test_build_proxies_still_leaves_it_false(self):
        # Documents WHY the assertion above is needed — if this ever starts
        # restoring the setting, the reason for the setdefault is gone.
        source = (pathlib.Path(__file__).resolve().parents[3] / "src" / "domains"
                  / "render_deliver" / "actions.py").read_text(encoding="utf-8")
        self.assertIn('"ExportAudio": False', source)


class SkillDocRelaysTheGatesTests(unittest.TestCase):
    """#193 phase 4 — the three flags a montage host must relay.

    A green plan can still be a structurally different cut than the user
    asked for (`grid_available: false`), a revision can silently be off the
    grid (`beat_lock_broken`), and a colour match can report `applied: N/N`
    while changing nothing (`look_bucket_basis: "default"`). None of the three
    was named in the skill, so the host could not report any of them. This
    guards the doc against drifting back.
    """

    def _skill(self):
        return (pathlib.Path(__file__).resolve().parents[3]
                / ".claude" / "skills" / "resolve-auto-edit" / "SKILL.md").read_text(encoding="utf-8")

    def test_skill_names_every_flag_the_host_must_relay(self):
        skill = self._skill()
        for key in ("grid_available", "beat_lock_broken", "look_bucket_basis"):
            self.assertIn(key, skill, f"SKILL.md never names {key}")

    def test_skill_explains_the_no_grid_montage(self):
        skill = self._skill()
        # The consequence, not just the flag name.
        self.assertIn("MIN_TEMPO_CONFIDENCE", skill)
        self.assertIn("onset-density", skill)

    def test_every_relayed_flag_is_really_on_the_plan(self):
        # The doc must not name keys the planner doesn't produce.
        source = (pathlib.Path(__file__).resolve().parents[3] / "src" / "domains"
                  / "auto_edit" / "utils" / "montage_edit.py").read_text(encoding="utf-8")
        self.assertIn('plan["grid_available"]', source)
        self.assertIn('plan["look_bucket_basis"]', source)


class ScoutTokenRoundTripTests(unittest.TestCase):
    """#193 phase 2.4 — the scout offer renamed its token on the way back.

    `deep_vision` writes the handshake for its own tool: the token comes back
    under `confirm_token` and the note says to re-call
    `media_analysis(action='deepen')`. A montage host must re-call `plan_cut`,
    whose parameter is `scout_confirm_token`. Echoing the key it was handed
    returned the identical offer forever, with no error.
    """

    def test_offer_carries_the_token_under_both_names_and_routes_to_plan_cut(self):
        root = tempfile.mkdtemp(prefix="montage-scout-")
        self.addCleanup(shutil.rmtree, root, True)
        offer = {"success": True, "status": "confirmation_required",
                 "estimate": {"frame_count": 12}, "confirm_token": "tok-abc",
                 "note": "Re-call media_analysis(action='deepen') with this confirm_token."}

        class _FakeDeepVision:
            @staticmethod
            def deepen_clip(*_a, **_k):
                return dict(offer)

        with mock.patch.object(montage_edit, "_deep_vision", lambda: _FakeDeepVision), \
                mock.patch.object(montage_edit, "_shots_needing_scout",
                                  lambda *_a, **_k: {"uuid-1": {1: [(0.0, 1.0)]}}), \
                mock.patch.object(montage_edit.auto_edit, "_clip_for_file",
                                  lambda *_a, **_k: {"clip_uuid": "uuid-1"}):
            out = montage_edit.scout_handoff_if_needed(root, {"files": ["/media/a.mov"]})

        self.assertEqual(out["confirm_token"], "tok-abc")
        self.assertEqual(out["scout_confirm_token"], "tok-abc")
        self.assertIn("plan_cut", out["note"])
        self.assertIn("scout_confirm_token", out["note"])
        # the misrouting note is gone
        self.assertNotIn("deepen", out["note"])

    def test_plan_cut_accepts_the_offers_own_key_as_an_alias(self):
        # The action must read confirm_token as well as scout_confirm_token —
        # otherwise echoing what you were handed is a silent no-op.
        source = (pathlib.Path(__file__).resolve().parents[3]
                  / "src" / "domains" / "auto_edit" / "actions.py").read_text(encoding="utf-8")
        block = source.split("scout_handoff_if_needed(")[1][:400]
        self.assertIn('p.get("scout_confirm_token")', block)
        self.assertIn('p.get("confirm_token")', block)


class VisionDefaultTests(unittest.TestCase):
    """#193 phase 1 — the first-run blocker.

    Montage's whole candidate pool is the `shots` table, whose only writer is
    fed by `visual.shot_descriptions`; nothing but the vision pass produces
    one. Vision-off montage therefore analysed "successfully" and then died in
    plan_cut with an error that named nothing about vision.
    """

    def test_montage_defaults_vision_on(self):
        enabled, warning = auto_edit.resolve_vision_default("montage", None)
        self.assertTrue(enabled)
        self.assertIsNone(warning)

    def test_montage_defaults_vision_on_with_other_options(self):
        # An options dict that simply doesn't mention vision must not read as
        # an opt-out — this is the shape a host passing sampling_mode sends.
        enabled, warning = auto_edit.resolve_vision_default(
            "montage", {"sampling_mode": "adaptive_capped"})
        self.assertTrue(enabled)
        self.assertIsNone(warning)

    def test_talking_head_still_defaults_vision_off(self):
        for options in (None, {}, {"sampling_mode": "adaptive_capped"}):
            enabled, warning = auto_edit.resolve_vision_default("talking_head", options)
            self.assertFalse(enabled, options)
            self.assertIsNone(warning, options)

    def test_explicit_true_is_honoured_on_both_genres(self):
        for genre in ("montage", "talking_head"):
            enabled, warning = auto_edit.resolve_vision_default(genre, {"vision": True})
            self.assertTrue(enabled, genre)
            self.assertIsNone(warning, genre)

    def test_explicit_false_on_montage_is_honoured_but_warned(self):
        # Honoured, not overridden: the caller may be running the vision pass
        # separately. But it is the one combination that cannot plan, so it
        # must say so here rather than failing two steps later.
        enabled, warning = auto_edit.resolve_vision_default("montage", {"vision": False})
        self.assertFalse(enabled)
        self.assertIsNotNone(warning)
        self.assertIn("vision", warning)
        self.assertIn("plan_cut", warning)

    def test_explicit_false_on_talking_head_is_silent(self):
        enabled, warning = auto_edit.resolve_vision_default("talking_head", {"vision": False})
        self.assertFalse(enabled)
        self.assertIsNone(warning)

    def test_every_vision_required_genre_is_a_real_genre(self):
        # Guards the constant against a typo silently disabling the default.
        self.assertTrue(auto_edit.VISION_REQUIRED_GENRES <= auto_edit.GENRES)


if __name__ == "__main__":
    unittest.main()
