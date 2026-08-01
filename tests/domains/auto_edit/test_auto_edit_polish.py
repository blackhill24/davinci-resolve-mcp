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
import unittest.mock

from src.domains.auto_edit.utils import auto_edit, montage_motion, music_analysis


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


class MontageDissolveTest(unittest.TestCase):
    """issue #180, phase 5/6 of the montage-quality epic: phase 2 guarantees
    every montage cut is a source change, so the talking-head heuristic would
    dissolve the whole edit. Montage defaults to no_dissolves; re-enabling
    dissolves for a montage plan uses the musically motivated breathe-section
    list instead of the source-change heuristic."""

    def test_montage_plan_emits_zero_dissolves_by_default(self):
        # Every segment a different source, exactly like a real beat-cut
        # montage — the talking-head heuristic would fire on every boundary.
        plan = _plan([
            _seg("A", 0, role="montage_hook"),
            _seg("B", 48, role="montage"),
            _seg("C", 96, role="montage"),
            _seg("D", 144, role="montage"),
        ])
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["transitions"], 0)

    def test_talking_head_default_and_heuristic_unaffected(self):
        # Regression: a plain speech-role plan gets EXACTLY today's behaviour.
        plan = _plan([_seg("A", 0), _seg("A", 48), _seg("B", 96)])
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["transitions"], 1)
        self.assertEqual(out["ops"][0]["segment_index"], 2)

    def test_montage_no_dissolves_can_be_forced_off(self):
        plan = _plan([
            _seg("A", 0, role="montage_hook"),
            _seg("B", 48, role="montage"),
        ])
        out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": False})
        # source-change heuristic never applies to montage even when
        # dissolves are force-enabled — only a breathe-section opener does,
        # and neither segment here has one, so still zero.
        self.assertEqual(out["transitions"], 0)

    def test_montage_dissolves_into_breathe_section_only(self):
        # #208: a section-boundary transition needs real handle media on
        # both sides — source_limit_frame (B's tail) / source_floor_frame
        # (C's head) give it 12f of margin each, comfortably above the 6f
        # (half of DEFAULT_DISSOLVE_FRAMES) the check requires.
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="intro"),
            _seg("B", 48, role="montage", section="high", source_limit_frame=60),
            _seg("C", 96, role="montage", section="breathe", source_floor_frame=-12),  # dissolve INTO this
            _seg("D", 144, role="montage", section="breathe"),  # already in breathe — no dissolve
            _seg("E", 192, role="montage", section="mid"),  # leaving breathe — no dissolve
        ])
        out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": False})
        self.assertEqual(out["transitions"], 1)
        op = next(o for o in out["ops"] if o["op"] == "place_transition")
        self.assertEqual(op["segment_index"], 2)
        self.assertIn("breathe", op["reason"])
        self.assertEqual(op["args"]["durationFrames"], auto_edit.DEFAULT_DISSOLVE_FRAMES)
        self.assertEqual(op["intended_type"], "cross_dissolve")

    def test_montage_hard_cut_sections_never_get_a_transition(self):
        # Even with generous handle margin, entering accelerate/high stays a
        # hard cut — "a 1-beat cut with a transition on it is mush" (#208).
        for hard_section in ("accelerate", "high"):
            with self.subTest(section=hard_section):
                plan = _plan([
                    _seg("A", 0, role="montage_hook", section="breathe", source_limit_frame=200),
                    _seg("B", 48, role="montage", section=hard_section, source_floor_frame=-200),
                ])
                out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": False})
                self.assertEqual(out["transitions"], 0)

    def test_montage_transition_skipped_without_handle_margin(self):
        # Same breathe boundary as above, but no margin at all (plain _seg
        # defaults) — the transition must be skipped, not silently placed as
        # a freeze/black edge, and the note must name the segment.
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="high"),
            _seg("B", 48, role="montage", section="breathe"),
        ])
        out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": False})
        self.assertEqual(out["transitions"], 0)
        self.assertTrue(any(
            "segment 1" in n and "handle media" in n for n in out["notes"]))

    def test_montage_outro_gets_a_longer_dissolve(self):
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="mid", source_limit_frame=200),
            _seg("B", 48, role="montage", section="outro", source_floor_frame=-200),
        ])
        out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": False})
        op = next(o for o in out["ops"] if o["op"] == "place_transition")
        self.assertEqual(op["args"]["durationFrames"], auto_edit.DEFAULT_DISSOLVE_FRAMES * 2)

    def test_montage_drop_boundary_reports_its_intended_type(self):
        # Only cross_dissolve is captured today (#208 leaves new template
        # capture open), but the placement rule still records the INTENDED
        # type so a future capture only needs to change the type lookup.
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="build", source_limit_frame=200),
            _seg("B", 48, role="montage", section="drop", source_floor_frame=-200),
        ])
        out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": False})
        op = next(o for o in out["ops"] if o["op"] == "place_transition")
        self.assertEqual(op["kind"], "cross_dissolve")
        self.assertEqual(op["intended_type"], "dip_to_colour")

    def test_montage_transitions_are_spaced_apart(self):
        # Two boundaries a single beat apart — well inside
        # MONTAGE_MIN_BEATS_BETWEEN_TRANSITIONS — only the first fires.
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="high", beat_index=0,
                 source_limit_frame=200),
            _seg("B", 48, role="montage", section="breathe", beat_index=2,
                 source_limit_frame=200, source_floor_frame=-200),
            _seg("C", 96, role="montage", section="low", beat_index=3,
                 source_floor_frame=-200),
        ])
        out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": False})
        self.assertEqual(out["transitions"], 1)
        self.assertEqual(out["ops"][0]["segment_index"], 1)
        self.assertTrue(any(
            "segment 2" in n and "beats of the last placed one" in n for n in out["notes"]))

    def test_montage_transitions_beyond_spacing_both_fire(self):
        # Same shape, but far enough apart (beat_index gap >= the minimum) —
        # both boundaries get a transition.
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="high", beat_index=0,
                 source_limit_frame=200),
            _seg("B", 48, role="montage", section="breathe",
                 beat_index=auto_edit.MONTAGE_MIN_BEATS_BETWEEN_TRANSITIONS,
                 source_limit_frame=200, source_floor_frame=-200),
            _seg("C", 96, role="montage", section="low",
                 beat_index=auto_edit.MONTAGE_MIN_BEATS_BETWEEN_TRANSITIONS * 2,
                 source_floor_frame=-200),
        ])
        out = auto_edit.plan_polish_ops(plan, options={"no_dissolves": False})
        self.assertEqual(out["transitions"], 2)

    def test_montage_explicit_dissolve_at_segments_still_overrides(self):
        # no_dissolves is montage's master switch (defaults on); an explicit
        # dissolve_at_segments list only overrides the AUTO-DETECTION within
        # that switch, same as talking-head — so re-enable it explicitly here.
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="intro"),
            _seg("B", 48, role="montage", section="high"),
        ])
        out = auto_edit.plan_polish_ops(
            plan, options={"no_dissolves": False, "dissolve_at_segments": [1]})
        self.assertEqual(out["transitions"], 1)
        self.assertEqual(out["ops"][0]["segment_index"], 1)


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


def _plan_with_music(mode, *, gain_db=-14.0, track_index=2):
    plan = _plan([_seg("A", 0), _seg("A", 48)])  # single source: no auto-dissolves
    plan["music"] = {
        "path": "/x/bed.mov", "track_index": track_index, "gain_db": gain_db,
        "ducking": {"mode": mode},
    }
    return plan


class MusicDuckTest(unittest.TestCase):
    """Tier-2 ducking (issue #14): drt_automation emits a set_audio_level op."""

    def test_drt_automation_emits_a_music_duck_op(self):
        plan = _plan_with_music(music_analysis.DUCKING_DRT_AUTOMATION, gain_db=-14.0)
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["music_ducks"], 1)
        duck = next(o for o in out["ops"] if o["op"] == "set_audio_level")
        self.assertEqual(duck["args"], {"track": 2, "volumeDb": -14.0, "clipIndex": 0})
        self.assertEqual(duck["kind"], "music_duck")

    def test_rendered_bed_and_static_emit_no_duck_op(self):
        for mode in (music_analysis.DUCKING_RENDERED_BED, music_analysis.DUCKING_STATIC):
            out = auto_edit.plan_polish_ops(_plan_with_music(mode))
            self.assertEqual(out["music_ducks"], 0, mode)

    def test_duck_targets_the_music_track_index(self):
        plan = _plan_with_music(music_analysis.DUCKING_DRT_AUTOMATION, track_index=3)
        duck = next(o for o in auto_edit.plan_polish_ops(plan)["ops"]
                    if o["op"] == "set_audio_level")
        self.assertEqual(duck["args"]["track"], 3)

    def test_no_music_duck_option_suppresses_it(self):
        plan = _plan_with_music(music_analysis.DUCKING_DRT_AUTOMATION)
        out = auto_edit.plan_polish_ops(plan, options={"no_music_duck": True})
        self.assertEqual(out["music_ducks"], 0)

    def test_missing_gain_is_an_honest_note_not_a_fabricated_op(self):
        plan = _plan_with_music(music_analysis.DUCKING_DRT_AUTOMATION, gain_db=None)
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(out["music_ducks"], 0)
        self.assertTrue(any("music_duck skipped" in n for n in out["notes"]))


class DroppedSourceClipsTest(unittest.TestCase):
    """The media-link honesty check (issue #13 relink wrinkle).

    The coverage scan counts media-less items as "offline": the intro title, each
    Text+ lower-third, and (in the reimported timeline) a cross-dissolve transition
    item. Diffing the offline count false-positives on those additions, so the
    check diffs the LINKED count instead — only a dropped source clip reduces it.
    """

    def test_added_generators_do_not_count_as_dropped(self):
        # The live two-source case: built 9 items / 8 linked → polished 11 / 8
        # (offline rose by two: a lower-third + a transition item). linked held at
        # 8, so ZERO source clips dropped.
        self.assertEqual(
            auto_edit.dropped_source_clips(baseline_linked=8, polished_linked=8),
            0,
        )

    def test_single_source_lower_third_only(self):
        # Built 3 / 2 linked (intro title offline) → polished 4 / 2 (added
        # lower-third). Still 2 linked → nothing dropped.
        self.assertEqual(
            auto_edit.dropped_source_clips(baseline_linked=2, polished_linked=2),
            0,
        )

    def test_genuinely_dropped_source_clip_is_flagged(self):
        # A real drop is the only thing that reduces linked: 8 → 7 = one clip lost
        # its link, regardless of how many generators were added alongside.
        self.assertEqual(
            auto_edit.dropped_source_clips(baseline_linked=8, polished_linked=7),
            1,
        )

    def test_reimport_relinking_never_goes_negative(self):
        # If the reimport re-links a previously-offline item (linked rises), that's
        # only an improvement — never a negative "drop".
        self.assertEqual(
            auto_edit.dropped_source_clips(baseline_linked=7, polished_linked=8),
            0,
        )


class MontageSpeedRampTest(unittest.TestCase):
    """The `retime` flag used to reach finish() and set RetimeProcess — the
    interpolation QUALITY — and nothing else, so no speed change ever happened.
    The scripting API cannot change speed at all, so the ramp has to be authored
    here, in the drt round-trip."""

    def _retime_plan(self, **seg_extra):
        return _plan([
            _seg("A", 0, role="montage_hook", section="intro"),
            _seg("B", 48, role="montage", section="build", retime=True, **seg_extra),
            _seg("C", 96, role="montage", section="mid"),
        ])

    def _retime_ops(self, out):
        return [op for op in out["ops"] if op["op"] == "retime_clip"]

    def test_flagged_segment_gets_a_retime_op(self):
        ops = self._retime_ops(auto_edit.plan_polish_ops(self._retime_plan()))
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["segment_index"], 1)
        self.assertEqual(ops[0]["kind"], "speed_ramp")
        self.assertEqual(
            ops[0]["args"]["speed"], montage_motion.MONTAGE_RETIME_SPEED["build"])

    def test_unflagged_segments_get_nothing(self):
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="intro"),
            _seg("B", 48, role="montage", section="mid"),
        ])
        self.assertEqual(self._retime_ops(auto_edit.plan_polish_ops(plan)), [])

    def test_record_duration_is_pinned_so_the_beat_grid_survives(self):
        # newDuration must equal the segment's own record length: the default
        # (oldDuration * oldSpeed / speed) would stretch the clip and walk every
        # downstream cut off its beat frame. preserveDuration=True (issue #202)
        # is what actually enforces this against the built .drt's ground truth —
        # newDuration alone is only ever a prediction that can drift.
        plan = self._retime_plan()
        ops = self._retime_ops(auto_edit.plan_polish_ops(plan))
        seg = plan["segments"][1]
        record_len = seg["source_end_frame"] - seg["source_start_frame"]
        self.assertEqual(ops[0]["args"]["newDuration"], record_len)
        self.assertIs(ops[0]["args"]["preserveDuration"], True)
        self.assertIs(ops[0]["args"]["ripple"], False)

    def test_preserve_duration_set_even_when_record_length_does_not_divide_evenly(self):
        # issue #202: a mixed-fps plan carries an explicit record_length_frames
        # that need not divide evenly against anything (unlike same-fps plans,
        # where record length is just a source-frame span). preserveDuration
        # must still be set — it's what makes newDuration's exact value
        # irrelevant to whether a gap opens, regardless of how it was rounded.
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="intro"),
            _seg("B", 48, role="montage", section="build", retime=True,
                 record_length_frames=17),  # not a clean multiple of anything
            _seg("C", 96, role="montage", section="mid"),
        ])
        ops = self._retime_ops(auto_edit.plan_polish_ops(plan))
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["args"]["newDuration"], 17)
        self.assertIs(ops[0]["args"]["preserveDuration"], True)

    def test_clip_index_accounts_for_the_intro_title(self):
        plan = self._retime_plan()
        without = self._retime_ops(auto_edit.plan_polish_ops(plan))
        with_title = self._retime_ops(auto_edit.plan_polish_ops(plan, record_offset=100))
        self.assertEqual(without[0]["args"]["clipIndex"], 1)
        self.assertEqual(with_title[0]["args"]["clipIndex"], 2)

    def test_speed_above_one_is_refused_with_an_honest_note(self):
        # >1x eats record_len*speed source out of a window that only reserved
        # record_len, and the CutList carries no clip-length bound to check it
        # against — so it must skip and say so, never silently run off the media.
        with unittest.mock.patch.dict(
            montage_motion.MONTAGE_RETIME_SPEED, {"build": 2.0}, clear=False
        ):
            out = auto_edit.plan_polish_ops(self._retime_plan())
        self.assertEqual(self._retime_ops(out), [])
        self.assertTrue(any("retime skipped" in n and "2.0x" in n for n in out["notes"]),
                        f"expected an honest skip note, got {out['notes']}")

    def test_zero_length_segment_is_skipped_not_emitted(self):
        plan = _plan([
            _seg("A", 0, role="montage_hook", section="intro"),
            _seg("B", 48, length=0, role="montage", section="build", retime=True),
        ])
        out = auto_edit.plan_polish_ops(plan)
        self.assertEqual(self._retime_ops(out), [])
        self.assertTrue(any("non-positive record length" in n for n in out["notes"]))

    def test_talking_head_plans_never_get_speed_ramps(self):
        # `retime` is a montage arrangement flag; a speech plan carrying one by
        # accident must not be retimed.
        plan = _plan([_seg("A", 0), _seg("B", 48, retime=True)])
        self.assertEqual(self._retime_ops(auto_edit.plan_polish_ops(plan)), [])

    def test_no_retime_option_suppresses_the_family(self):
        out = auto_edit.plan_polish_ops(self._retime_plan(), options={"no_retime": True})
        self.assertEqual(self._retime_ops(out), [])


if __name__ == "__main__":
    unittest.main()
