"""Unit tests for the Phase-2 polish decision layer (auto_edit.plan_polish_ops).

Pure and offline: plan_polish_ops turns an approved+built CutList into drp-format
vendor op specs (cross-dissolves at flagged cuts + lower-thirds on an upper
track). No Resolve, no Node, no I/O — the server exports the timeline, threads
these specs through advanced_bridge.run_drp_op_chain, and reimports. The live
export/reimport round-trip is #13's final acceptance gate; this file covers every
decision the offline layer makes on the way there.
"""

from __future__ import annotations

import unittest

from src.utils import auto_edit


def _seg(clip_uuid, record_start, length=48, **extra):
    seg = {
        "role": "speech",
        "clip_uuid": clip_uuid,
        "clip_id": None,
        "source_start_frame": 0,
        "source_end_frame": length,
        "record_start_frame": record_start,
        "transcript_excerpt": "",
        "rationale": "",
        "evidence": {},
    }
    seg.update(extra)
    return seg


def _plan(segments, overlays=None):
    return {
        "kind": "auto_edit_cut",
        "fps": 24.0,
        "segments": segments,
        "overlays": overlays or [],
        "titles": [],
        "music": None,
    }


class DissolveTest(unittest.TestCase):
    def test_source_change_flags_a_cross_dissolve(self):
        # A → A (no dissolve) then A → B (source change ⇒ dissolve).
        plan = _plan([
            _seg("A", 0), _seg("A", 48), _seg("B", 96),
        ])
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["transitions"], 1)
        op = next(o for o in out["ops"] if o["op"] == "place_transition")
        self.assertEqual(op["args"]["track"], auto_edit.SPEECH_VIDEO_TRACK)
        self.assertEqual(op["args"]["atFrame"], 96)  # boundary before segment 2
        self.assertEqual(op["args"]["durationFrames"], auto_edit.DEFAULT_DISSOLVE_FRAMES)
        self.assertEqual(op["segment_index"], 2)

    def test_single_source_cut_has_no_auto_dissolves(self):
        plan = _plan([_seg("A", 0), _seg("A", 48), _seg("A", 96)])
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["transitions"], 0)

    def test_record_offset_shifts_transition_position(self):
        plan = _plan([_seg("A", 0), _seg("B", 48)])
        out = auto_edit.plan_polish_ops(plan, record_offset=100)
        op = next(o for o in out["ops"] if o["op"] == "place_transition")
        self.assertEqual(op["args"]["atFrame"], 148)  # 48 + intro-title footprint
        self.assertEqual(out["record_offset"], 100)

    def test_broll_overlay_suppresses_the_dissolve_at_that_cut(self):
        # Source change at segment 1, but a b-roll overlay already smooths it.
        plan = _plan(
            [_seg("A", 0), _seg("B", 48)],
            overlays=[{"over_segment_index": 1, "clip_uuid": "C"}],
        )
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["transitions"], 0)
        self.assertTrue(any("overlay already smooths" in n for n in out["notes"]))

    def test_transition_in_flag_forces_a_dissolve_within_one_source(self):
        plan = _plan([
            _seg("A", 0),
            _seg("A", 48, transition_in={"duration_frames": 30}),
        ])
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["transitions"], 1)
        op = next(o for o in out["ops"] if o["op"] == "place_transition")
        self.assertEqual(op["args"]["durationFrames"], 30)  # flag's own duration wins

    def test_explicit_dissolve_at_segments_overrides_auto(self):
        # Source changes everywhere, but the explicit list wins: only segment 1.
        plan = _plan([_seg("A", 0), _seg("B", 48), _seg("C", 96)])
        out = auto_edit.plan_polish_ops(
            plan, options={"dissolve_at_segments": [1]})
        self.assertEqual(out["transitions"], 1)
        self.assertEqual(out["ops"][0]["segment_index"], 1)

    def test_beat_change_dissolve_is_opt_in(self):
        plan = _plan([
            _seg("A", 0, story_beat="intro"),
            _seg("A", 48, story_beat="middle"),
        ])
        # Off by default (same source, no flag).
        self.assertEqual(auto_edit.plan_polish_ops(plan)["transitions"], 0)
        # On with the option.
        out = auto_edit.plan_polish_ops(
            plan, options={"dissolve_on_beat_change": True})
        self.assertEqual(out["transitions"], 1)

    def test_no_dissolves_option_suppresses_all(self):
        plan = _plan([_seg("A", 0), _seg("B", 48)])
        out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": True})
        self.assertEqual(out["transitions"], 0)


class LowerThirdTest(unittest.TestCase):
    def test_one_lower_third_per_distinct_story_beat(self):
        plan = _plan([
            _seg("A", 0, story_beat="Guest intro"),
            _seg("A", 48, story_beat="Guest intro"),   # same beat: no new title
            _seg("A", 96, story_beat="The pivot"),
        ])
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["lower_thirds"], 2)
        titles = [o for o in out["ops"] if o["op"] == "place_fusion_title"]
        self.assertEqual([t["args"]["text"] for t in titles], ["Guest intro", "The pivot"])
        self.assertEqual([t["args"]["startFrame"] for t in titles], [0, 96])

    def test_lower_thirds_land_above_broll_when_overlays_present(self):
        plan = _plan(
            [_seg("A", 0, story_beat="Topic")],
            overlays=[{"over_segment_index": 0}],
        )
        out = auto_edit.plan_polish_ops(plan)
        title = next(o for o in out["ops"] if o["op"] == "place_fusion_title")
        self.assertEqual(title["args"]["trackIndex"], 3)  # V3 above V2 b-roll

    def test_lower_thirds_default_to_v2_without_overlays(self):
        plan = _plan([_seg("A", 0, story_beat="Topic")])
        out = auto_edit.plan_polish_ops(plan)
        title = next(o for o in out["ops"] if o["op"] == "place_fusion_title")
        self.assertEqual(title["args"]["trackIndex"], 2)

    def test_no_story_beats_yields_honest_note_not_fabricated_captions(self):
        plan = _plan([_seg("A", 0), _seg("A", 48)])
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["lower_thirds"], 0)
        self.assertTrue(any("no lower-thirds" in n for n in out["notes"]))

    def test_explicit_lower_thirds_win_over_auto(self):
        plan = _plan([
            _seg("A", 0, story_beat="auto beat"),
            _seg("A", 48),
        ])
        out = auto_edit.plan_polish_ops(plan, options={"lower_thirds": [
            {"text": "Jane Doe, CEO", "at_segment": 1, "duration_frames": 72},
        ]})
        titles = [o for o in out["ops"] if o["op"] == "place_fusion_title"]
        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["args"]["text"], "Jane Doe, CEO")
        self.assertEqual(titles[0]["args"]["startFrame"], 48)
        self.assertEqual(titles[0]["args"]["durationFrames"], 72)

    def test_explicit_lower_third_by_record_frame_with_offset(self):
        plan = _plan([_seg("A", 0)])
        out = auto_edit.plan_polish_ops(
            plan, record_offset=10,
            options={"lower_thirds": [{"text": "caption", "record_start_frame": 20}]})
        title = next(o for o in out["ops"] if o["op"] == "place_fusion_title")
        self.assertEqual(title["args"]["startFrame"], 30)

    def test_explicit_lower_third_without_position_is_skipped_honestly(self):
        plan = _plan([_seg("A", 0)])
        out = auto_edit.plan_polish_ops(plan, options={"lower_thirds": [
            {"text": "no position"},
            {"text": "  "},  # blank text
        ]})
        self.assertEqual(out["lower_thirds"], 0)
        self.assertEqual(len([n for n in out["notes"] if "skipped" in n]), 2)

    def test_no_lower_thirds_option_suppresses_all(self):
        plan = _plan([_seg("A", 0, story_beat="Topic")])
        out = auto_edit.plan_polish_ops(plan, options={"no_lower_thirds": True})
        self.assertEqual(out["lower_thirds"], 0)


class OpOrderTest(unittest.TestCase):
    def test_transitions_precede_lower_thirds(self):
        plan = _plan([
            _seg("A", 0, story_beat="intro"),
            _seg("B", 48, story_beat="next"),
        ])
        out = auto_edit.plan_polish_ops(plan)
        kinds = [o["kind"] for o in out["ops"]]
        # every cross_dissolve appears before the first lower_third
        first_lt = kinds.index("lower_third")
        self.assertTrue(all(k == "cross_dissolve" for k in kinds[:first_lt]))


class PolishedRealOfflineTest(unittest.TestCase):
    """The media-link honesty diff (issue #13 relink wrinkle).

    The coverage scan counts media-less generators as "offline": the intro title
    build_timeline placed, plus each Text+ lower-third the polish adds. Judging the
    round-trip against a raw count false-alarms on those; the honest measure is a
    diff against the built timeline's pre-round-trip coverage.
    """

    def test_intro_title_alone_is_not_a_dropped_clip(self):
        # Built timeline carries an intro title (offline=1). Polish adds one
        # lower-third → polished offline=2. No SOURCE clip dropped.
        self.assertEqual(
            auto_edit.polished_real_offline(
                polished_offline=2, baseline_offline=1, lower_thirds=1),
            0,
        )

    def test_transitions_are_not_subtracted(self):
        # Two cross-dissolves + zero lower-thirds: transitions are not timeline
        # items, so they never enter the offline count and must not be subtracted.
        # Baseline intro title (1) still cancels; nothing real dropped.
        self.assertEqual(
            auto_edit.polished_real_offline(
                polished_offline=1, baseline_offline=1, lower_thirds=0),
            0,
        )

    def test_genuinely_dropped_source_clip_is_flagged(self):
        # Baseline offline=1 (intro title); polished offline=3 = intro title + one
        # added lower-third + one SOURCE clip that lost its link → real_offline=1.
        self.assertEqual(
            auto_edit.polished_real_offline(
                polished_offline=3, baseline_offline=1, lower_thirds=1),
            1,
        )

    def test_never_negative(self):
        self.assertEqual(
            auto_edit.polished_real_offline(
                polished_offline=0, baseline_offline=1, lower_thirds=2),
            0,
        )


if __name__ == "__main__":
    unittest.main()
