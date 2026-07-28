"""Offline tests for src/domains/media_analysis/utils/marker_plan.py.

Marker synthesis is pure: analysis payloads in, a marker plan out. Nothing here
touches Resolve or FFmpeg, so every branch is directly reachable.

The load-bearing behaviour these tests pin down is the *description provenance*
rule. A shot marker may only carry a description that genuinely belongs to that
shot; a point-in-time marker may only borrow a keyframe within ~2s. Where no
such evidence exists the plan must say so — via the clearly-marked clip_summary
fallback or the sentinel — rather than copying a far-away keyframe's text, which
would put fabricated visual claims in front of an editor.
"""
import unittest

from src.domains.media_analysis.utils.marker_plan import (
    _VISUAL_DESCRIPTION_UNAVAILABLE,
    _analysis_fps,
    _build_clip_marker_plan,
    _duration_frames,
    _ranges_overlap,
    _seconds_to_frame,
    _shot_ranges_from_scenes,
    _time_seconds_from_text,
    _transcript_excerpt_for_range,
    _trim_text,
    _visual_description_for_shot,
    _visual_description_for_time,
)


class AnalysisFpsTest(unittest.TestCase):
    def test_numeric_record_fps_wins(self):
        self.assertAlmostEqual(_analysis_fps({"fps": 25}, {}), 25.0)

    def test_rational_string_is_divided(self):
        self.assertAlmostEqual(_analysis_fps({"fps": "24000/1001"}, {}), 23.976, places=3)

    def test_embedded_number_is_extracted_from_prose(self):
        self.assertAlmostEqual(_analysis_fps({"frame_rate": "29.97 fps"}, {}), 29.97)

    def test_camelcase_key_accepted(self):
        self.assertAlmostEqual(_analysis_fps({"frameRate": "50"}, {}), 50.0)

    def test_falls_back_to_technical_video_stream(self):
        technical = {"summary": {"video": [{"frame_rate": "48"}]}}
        self.assertAlmostEqual(_analysis_fps({}, technical), 48.0)

    def test_defaults_to_24_when_nothing_is_parseable(self):
        self.assertAlmostEqual(_analysis_fps({"fps": "unknown"}, {}), 24.0)
        self.assertAlmostEqual(_analysis_fps({}, {"summary": {"video": []}}), 24.0)


class FrameMathTest(unittest.TestCase):
    def test_seconds_to_frame_rounds(self):
        self.assertEqual(_seconds_to_frame(1.5, 24.0), 36)
        self.assertEqual(_seconds_to_frame(1.02, 25.0), 26)

    def test_negative_seconds_clamp_to_zero(self):
        self.assertEqual(_seconds_to_frame(-5.0, 24.0), 0)

    def test_none_and_garbage_yield_none(self):
        self.assertIsNone(_seconds_to_frame(None, 24.0))
        self.assertIsNone(_seconds_to_frame("later", 24.0))

    def test_fps_below_one_is_floored_to_one(self):
        self.assertEqual(_seconds_to_frame(10.0, 0.0), 10)

    def test_duration_frames_is_at_least_one(self):
        # A sub-frame range must still produce a placeable marker.
        self.assertEqual(_duration_frames(1.0, 1.001, 24.0), 1)
        self.assertEqual(_duration_frames(1.0, 3.0, 24.0), 48)

    def test_duration_frames_falls_back_when_bounds_missing(self):
        self.assertEqual(_duration_frames(None, 3.0, 24.0), 1)
        self.assertEqual(_duration_frames(1.0, None, 24.0, fallback=7), 7)


class TimeFromTextTest(unittest.TestCase):
    def test_mm_ss_timecode(self):
        self.assertAlmostEqual(_time_seconds_from_text("great beat at 01:30"), 90.0)

    def test_hh_mm_ss_timecode(self):
        self.assertAlmostEqual(_time_seconds_from_text("1:02:03"), 3723.0)

    def test_fractional_seconds_with_comma_or_dot(self):
        self.assertAlmostEqual(_time_seconds_from_text("00:10,500"), 10.5)
        self.assertAlmostEqual(_time_seconds_from_text("00:10.250"), 10.25)

    def test_bare_seconds_phrasing(self):
        self.assertAlmostEqual(_time_seconds_from_text("laugh at 12 seconds"), 12.0)
        self.assertAlmostEqual(_time_seconds_from_text("cut at 7.5s"), 7.5)

    def test_dict_time_keys_take_priority_over_prose(self):
        note = {"time_seconds": 4.0, "text": "mentions 01:30 in the text"}
        self.assertAlmostEqual(_time_seconds_from_text(note), 4.0)

    def test_dict_falls_through_to_its_text(self):
        self.assertAlmostEqual(_time_seconds_from_text({"text": "at 00:30"}), 30.0)
        self.assertAlmostEqual(_time_seconds_from_text({"note": "at 00:45"}), 45.0)

    def test_untimed_text_returns_none(self):
        self.assertIsNone(_time_seconds_from_text("a really nice moment"))
        self.assertIsNone(_time_seconds_from_text(None))


class TrimTextTest(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(_trim_text("  a\n\t b  "), "a b")

    def test_truncates_with_ellipsis_at_the_limit(self):
        out = _trim_text("x" * 50, limit=10)
        self.assertEqual(len(out), 12)
        self.assertTrue(out.endswith("..."))

    def test_none_becomes_empty_string(self):
        self.assertEqual(_trim_text(None), "")


class RangesOverlapTest(unittest.TestCase):
    def test_plain_overlap_and_disjoint(self):
        self.assertTrue(_ranges_overlap(0.0, 5.0, 4.0, 9.0))
        self.assertFalse(_ranges_overlap(0.0, 5.0, 6.0, 9.0))

    def test_touching_bounds_count_as_overlapping(self):
        self.assertTrue(_ranges_overlap(0.0, 5.0, 5.0, 9.0))

    def test_missing_end_is_treated_as_a_point(self):
        self.assertTrue(_ranges_overlap(3.0, None, 0.0, 4.0))
        self.assertFalse(_ranges_overlap(3.0, None, 0.0, 2.0))

    def test_missing_start_anchors_at_zero(self):
        self.assertTrue(_ranges_overlap(None, 2.0, 0.0, 1.0))


class TranscriptExcerptTest(unittest.TestCase):
    WORDS = {
        "words": [
            {"word": "hello", "start": 0.0, "end": 0.5},
            {"word": "world", "start": 0.6, "end": 1.0},
            {"word": "later", "start": 30.0, "end": 30.4},
        ]
    }

    def test_word_timestamps_select_only_the_range(self):
        self.assertEqual(_transcript_excerpt_for_range(self.WORDS, 0.0, 2.0), "hello world")

    def test_words_are_harvested_from_segments_when_top_level_absent(self):
        transcript = {"segments": [{"words": [{"word": "nested", "start": 1.0, "end": 1.5}]}]}
        self.assertEqual(_transcript_excerpt_for_range(transcript, 0.0, 2.0), "nested")

    def test_falls_back_to_segment_text_without_word_timings(self):
        transcript = {"segments": [
            {"text": "in range", "start": 0.0, "end": 2.0},
            {"text": "out of range", "start": 60.0, "end": 62.0},
        ]}
        self.assertEqual(_transcript_excerpt_for_range(transcript, 0.0, 2.0), "in range")

    def test_no_match_yields_empty_string(self):
        self.assertEqual(_transcript_excerpt_for_range(self.WORDS, 100.0, 101.0), "")
        self.assertEqual(_transcript_excerpt_for_range({}, 0.0, 1.0), "")


class VisualDescriptionForShotTest(unittest.TestCase):
    VISION = {
        "shot_descriptions": [
            {"shot_index": 1, "description": "wide establishing shot", "time_seconds_start": 0.0, "time_seconds_end": 3.0},
            {"shot_index": 2, "visual_description": "close up on hands", "time_seconds_start": 3.0, "time_seconds_end": 6.0},
        ],
        "analysis_keyframes": [{"time_seconds": 4.0, "description": "keyframe text"}],
        "clip_summary": "an interview in a kitchen",
    }

    def test_exact_index_match_wins(self):
        self.assertEqual(_visual_description_for_shot(self.VISION, 1, 0.0, 3.0), "wide establishing shot")

    def test_visual_description_key_is_accepted(self):
        self.assertEqual(_visual_description_for_shot(self.VISION, 2, 3.0, 6.0), "close up on hands")

    def test_time_range_match_when_index_is_unknown(self):
        self.assertEqual(_visual_description_for_shot(self.VISION, None, 3.0, 6.0), "close up on hands")

    def test_non_numeric_index_degrades_to_time_matching(self):
        self.assertEqual(_visual_description_for_shot(self.VISION, "second", 3.0, 6.0), "close up on hands")

    def test_keyframe_in_range_is_the_second_layer(self):
        vision = {
            "analysis_keyframes": [{"time_seconds": 4.0, "description": "keyframe text"}],
            "clip_summary": "a summary",
        }
        self.assertEqual(_visual_description_for_shot(vision, 9, 3.0, 6.0), "keyframe text")

    def test_clip_summary_fallback_is_labelled_as_such(self):
        out = _visual_description_for_shot({"clip_summary": "a kitchen"}, 1, 0.0, 3.0)
        self.assertIn("shot description unavailable", out)
        self.assertIn("a kitchen", out)

    def test_sentinel_when_nothing_is_available(self):
        self.assertEqual(_visual_description_for_shot({}, 1, 0.0, 3.0), _VISUAL_DESCRIPTION_UNAVAILABLE)

    def test_unmatched_index_and_unmatched_time_do_not_borrow_another_shot(self):
        vision = {"shot_descriptions": [
            {"shot_index": 1, "description": "wide establishing shot", "time_seconds_start": 0.0, "time_seconds_end": 3.0},
        ]}
        self.assertEqual(
            _visual_description_for_shot(vision, 9, 500.0, 503.0), _VISUAL_DESCRIPTION_UNAVAILABLE
        )


class VisualDescriptionForTimeTest(unittest.TestCase):
    VISION = {
        "analysis_keyframes": [
            {"time_seconds": 10.0, "description": "man pours coffee"},
            {"time_seconds": 90.0, "description": "sunset over the bay"},
        ],
        "clip_summary": "a kitchen interview",
    }

    def test_keyframe_inside_the_marker_range_is_used(self):
        self.assertEqual(_visual_description_for_time(self.VISION, 9.5, 10.5), "man pours coffee")

    def test_nearest_keyframe_within_two_seconds_is_borrowed(self):
        self.assertEqual(_visual_description_for_time(self.VISION, 11.5, 12.0), "man pours coffee")

    def test_closest_of_several_nearby_keyframes_wins(self):
        vision = {"analysis_keyframes": [
            {"time_seconds": 10.0, "description": "far"},
            {"time_seconds": 11.8, "description": "near"},
        ]}
        self.assertEqual(_visual_description_for_time(vision, 12.0, 12.0), "near")

    def test_far_away_keyframe_is_never_copied(self):
        # 50s is well outside the 2s window: the summary fallback must win, or
        # the marker would assert visuals from a different part of the clip.
        self.assertEqual(_visual_description_for_time(self.VISION, 50.0, 50.5), "a kitchen interview")

    def test_start_only_marker_still_resolves(self):
        self.assertEqual(_visual_description_for_time(self.VISION, 10.0, None), "man pours coffee")

    def test_sentinel_when_no_keyframe_and_no_summary(self):
        vision = {"analysis_keyframes": [{"time_seconds": 90.0, "description": "far away"}]}
        self.assertEqual(_visual_description_for_time(vision, 5.0, 5.5), _VISUAL_DESCRIPTION_UNAVAILABLE)

    def test_untimed_marker_falls_back_to_summary(self):
        self.assertEqual(_visual_description_for_time(self.VISION, None, None), "a kitchen interview")

    def test_keyframes_without_a_description_are_skipped(self):
        vision = {"analysis_keyframes": [
            {"time_seconds": 10.0},
            {"time_seconds": 10.5, "visual_description": "usable"},
        ]}
        self.assertEqual(_visual_description_for_time(vision, 10.0, 11.0), "usable")


class ShotRangesFromScenesTest(unittest.TestCase):
    def test_scene_cuts_split_a_known_duration(self):
        out = _shot_ranges_from_scenes(10.0, [{"time_seconds": 4.0}])
        self.assertEqual(
            [(r["index"], r["start"], r["end"]) for r in out],
            [(1, 0.0, 4.0), (2, 4.0, 10.0)],
        )

    def test_cuts_closer_than_the_minimum_are_merged_away(self):
        out = _shot_ranges_from_scenes(10.0, [{"time_seconds": 4.0}, {"time_seconds": 4.2}])
        self.assertEqual([r["start"] for r in out], [0.0, 4.0])

    def test_duplicate_and_out_of_bounds_cuts_are_dropped(self):
        out = _shot_ranges_from_scenes(10.0, [
            {"time_seconds": 4.0},
            {"time_seconds": 4.0},
            {"time_seconds": 0.0},
            {"time_seconds": 99.0},
            {"time_seconds": None},
            "not a dict",
        ])
        self.assertEqual([r["start"] for r in out], [0.0, 4.0])

    def test_no_cuts_yields_one_whole_clip_range(self):
        self.assertEqual(_shot_ranges_from_scenes(10.0, []), [{"index": 1, "start": 0.0, "end": 10.0}])

    def test_unknown_duration_leaves_the_last_shot_open_ended(self):
        out = _shot_ranges_from_scenes(None, [{"time_seconds": 4.0}])
        self.assertEqual(out, [{"index": 1, "start": 0.0, "end": 4.0}, {"index": 2, "start": 4.0, "end": None}])

    def test_unknown_duration_and_no_cuts(self):
        self.assertEqual(_shot_ranges_from_scenes(None, []), [{"index": 1, "start": 0.0, "end": None}])

    def test_custom_minimum_duration_is_honoured(self):
        out = _shot_ranges_from_scenes(10.0, [{"time_seconds": 2.0}], min_duration_seconds=5.0)
        self.assertEqual([r["start"] for r in out], [0.0])


class BuildClipMarkerPlanTest(unittest.TestCase):
    def _plan(self, **kwargs):
        payload = {
            "record": {"fps": 24},
            "technical": {"summary": {"format": {"duration_seconds": 10.0}}},
            "readthrough": {},
            "motion": {},
            "transcript": {},
            "vision": {},
            "options": {},
        }
        payload.update(kwargs)
        return _build_clip_marker_plan(
            payload["record"],
            payload["technical"],
            payload["readthrough"],
            payload["motion"],
            payload["transcript"],
            payload["vision"],
            options=payload["options"],
        )

    def test_envelope_shape(self):
        out = self._plan()
        self.assertTrue(out["success"])
        self.assertEqual(out["schema"], "davinci_resolve_mcp.clip_analysis_markers.v1")
        self.assertAlmostEqual(out["fps"], 24.0)
        self.assertAlmostEqual(out["duration_seconds"], 10.0)
        self.assertEqual(out["marker_count"], len(out["markers"]))

    def test_shot_markers_come_from_scene_detection(self):
        out = self._plan(readthrough={"scenes": {"items": [{"time_seconds": 4.0}]}})
        shots = [m for m in out["markers"] if m["type"] == "shot"]
        self.assertEqual([m["id"] for m in shots], ["shot-001", "shot-002"])
        self.assertEqual(shots[0]["name"], "Shot 001")
        self.assertEqual(shots[0]["color"], "Blue")
        self.assertEqual(shots[0]["start_frame"], 0)
        self.assertEqual(shots[0]["duration_frames"], 96)
        self.assertEqual(shots[0]["source"], "scene_detection")

    def test_precomputed_shot_ranges_beat_scene_detection(self):
        out = self._plan(readthrough={
            "scenes": {"items": [{"time_seconds": 4.0}]},
            "cut_analysis": {"shot_ranges": [{"index": 1, "start": 0.0, "end": 10.0}], "cut_count": 3},
        })
        shots = [m for m in out["markers"] if m["type"] == "shot"]
        self.assertEqual(len(shots), 1)
        self.assertEqual(out["cut_analysis"]["cut_count"], 3)

    def test_flash_frame_candidates_become_qc_warnings(self):
        out = self._plan(readthrough={"cut_analysis": {
            "shot_ranges": [{"index": 1, "start": 0.0, "end": 10.0}],
            "flash_frame_candidates": [{"start": 2.0, "end": 2.1}, "junk"],
        }})
        flashes = [m for m in out["markers"] if m.get("subtype") == "flash_frame_candidate"]
        self.assertEqual(len(flashes), 1)
        self.assertEqual(flashes[0]["id"], "flash-frame-candidate-001")
        self.assertEqual(flashes[0]["confidence"], "computed_needs_visual_confirmation")
        # The summary counts markers actually BUILT, so the skipped malformed
        # entry is not claimed here, and the id sequence has no hole.
        self.assertEqual(out["cut_analysis"]["flash_frame_candidates"], 1)

    def test_malformed_flash_candidates_do_not_leave_a_hole_in_the_ids(self):
        out = self._plan(readthrough={"cut_analysis": {
            "shot_ranges": [{"index": 1, "start": 0.0, "end": 10.0}],
            "flash_frame_candidates": ["junk", {"start": 2.0, "end": 2.1}, None,
                                       {"start": 5.0, "end": 5.1}],
        }})
        flashes = [m for m in out["markers"] if m.get("subtype") == "flash_frame_candidate"]
        self.assertEqual([m["id"] for m in flashes],
                         ["flash-frame-candidate-001", "flash-frame-candidate-002"])
        self.assertEqual(out["cut_analysis"]["flash_frame_candidates"], 2)

    def test_flash_candidate_count_matches_markers_for_well_formed_input(self):
        out = self._plan(readthrough={"cut_analysis": {
            "shot_ranges": [{"index": 1, "start": 0.0, "end": 10.0}],
            "flash_frame_candidates": [{"start": 2.0, "end": 2.1}, {"start": 5.0, "end": 5.1}],
        }})
        flashes = [m for m in out["markers"] if m.get("subtype") == "flash_frame_candidate"]
        self.assertEqual(len(flashes), out["cut_analysis"]["flash_frame_candidates"])
        self.assertEqual([m["id"] for m in flashes],
                         ["flash-frame-candidate-001", "flash-frame-candidate-002"])

    def test_black_frame_ranges_become_qc_warnings(self):
        out = self._plan(readthrough={"black_frames": {"items": [{"start": 8.0, "end": 9.0}]}})
        blacks = [m for m in out["markers"] if m.get("subtype") == "black_or_title"]
        self.assertEqual(len(blacks), 1)
        self.assertEqual(blacks[0]["id"], "black-or-title-001")
        self.assertEqual(blacks[0]["source"], "blackdetect")

    def test_timed_best_moments_become_markers(self):
        out = self._plan(vision={
            "editing_notes": {"best_moments": ["great laugh at 00:05"]},
            "analysis_keyframes": [{"time_seconds": 5.2, "description": "she laughs"}],
        })
        best = [m for m in out["markers"] if m["type"] == "best_moment"]
        self.assertEqual(len(best), 1)
        self.assertAlmostEqual(best[0]["start_seconds"], 5.0)
        self.assertEqual(best[0]["visual_description"], "she laughs")
        self.assertEqual(best[0]["confidence"], "model_suggested")
        self.assertEqual(out["untimed_notes"], [])

    def test_untimed_best_moments_are_diverted_not_dropped(self):
        out = self._plan(vision={"editing_notes": {"best_moments": ["a really nice moment"]}})
        self.assertEqual([m for m in out["markers"] if m["type"] == "best_moment"], [])
        self.assertEqual(
            out["untimed_notes"],
            [{"type": "best_moment", "note": "a really nice moment", "reason": "missing_time"}],
        )

    def test_qc_warnings_merge_technical_and_vision_sources(self):
        out = self._plan(
            technical={
                "summary": {
                    "format": {"duration_seconds": 10.0},
                    "warnings": ["clipping at 00:02"],
                }
            },
            vision={"editing_notes": {"qc_flags": ["soft focus at 00:06", "no timestamp here"]}},
        )
        qc = [m for m in out["markers"] if m["type"] == "qc_warning"]
        self.assertEqual([m["id"] for m in qc], ["qc-warning-001", "qc-warning-002"])
        self.assertEqual([n["type"] for n in out["untimed_notes"]], ["qc_warning"])

    def test_best_moment_end_is_clamped_to_the_clip_duration(self):
        out = self._plan(vision={"editing_notes": {"best_moments": ["at 00:09.8"]}})
        best = [m for m in out["markers"] if m["type"] == "best_moment"][0]
        self.assertAlmostEqual(best["end_seconds"], 10.0)

    def test_marker_colors_can_be_overridden(self):
        out = self._plan(options={"marker_plan": {"colors": {"shot": "Purple", "qc_warning": ""}}})
        self.assertEqual(out["color_scheme"]["shot"], "Purple")
        # Empty overrides must not blank out a default.
        self.assertEqual(out["color_scheme"]["qc_warning"], "Red")

    def test_min_shot_duration_option_is_applied(self):
        out = self._plan(
            readthrough={"scenes": {"items": [{"time_seconds": 2.0}]}},
            options={"marker_plan": {"min_shot_duration_seconds": 5.0}},
        )
        self.assertEqual(len([m for m in out["markers"] if m["type"] == "shot"]), 1)

    def test_markers_are_sorted_by_start_time(self):
        out = self._plan(
            readthrough={
                "scenes": {"items": [{"time_seconds": 4.0}]},
                "black_frames": {"items": [{"start": 1.0, "end": 1.5}]},
            },
            vision={"editing_notes": {"best_moments": ["at 00:06"]}},
        )
        starts = [m.get("start_seconds", 0.0) for m in out["markers"]]
        self.assertEqual(starts, sorted(starts))

    def test_transcript_index_and_sound_notes(self):
        transcript = {
            "text": "hello world",
            "segments": [{"text": "hello world", "start": 0.0, "end": 3.0}],
            "words": [{"word": "hello", "start": 0.0, "end": 1.0}],
        }
        out = self._plan(transcript=transcript)
        self.assertTrue(out["transcript_index"]["available"])
        self.assertTrue(out["transcript_index"]["word_timestamps"])
        self.assertEqual(out["transcript_index"]["words"], 1)
        self.assertEqual(out["transcript_index"]["segments"], 1)
        shot = [m for m in out["markers"] if m["type"] == "shot"][0]
        self.assertEqual(shot["sound_note"], "Transcript: hello")
        self.assertEqual(shot["transcript_text"], "hello")

    def test_silence_note_when_no_transcript_covers_the_range(self):
        out = self._plan(readthrough={"silence": {"items": [{"start": 0.0, "end": 10.0}]}})
        shot = [m for m in out["markers"] if m["type"] == "shot"][0]
        self.assertIn("silence", shot["sound_note"])

    def test_sound_note_when_neither_transcript_nor_silence_applies(self):
        shot = [m for m in self._plan()["markers"] if m["type"] == "shot"][0]
        self.assertIn("no transcript excerpt", shot["sound_note"])

    def test_empty_values_are_stripped_from_marker_entries(self):
        shot = [m for m in self._plan()["markers"] if m["type"] == "shot"][0]
        self.assertNotIn("subtype", shot)
        self.assertNotIn("transcript_text", shot)

    def test_motion_and_occurrences_are_passed_through(self):
        out = self._plan(
            record={"fps": 24, "timeline_occurrences": [{"timeline": "Edit v1"}]},
            motion={"overall_motion_level": "high", "average_frame_delta": 0.4, "max_frame_delta": 0.9},
        )
        self.assertEqual(out["timeline_occurrences"], [{"timeline": "Edit v1"}])
        self.assertEqual(out["motion_summary"]["overall_motion_level"], "high")

    def test_analysis_signature_defaults_to_empty(self):
        self.assertEqual(self._plan()["analysis_signature"], {})


if __name__ == "__main__":
    unittest.main()
