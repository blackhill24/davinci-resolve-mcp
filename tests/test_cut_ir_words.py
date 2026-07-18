"""Tests for the Cut-IR word-level Pass-1 (auto_edit) and the CutList schema.

Covers filler / false-start detection over seeded ``transcript_words`` rows,
the cue-level fallback path when word timestamps are unavailable, half-open
frame math, and CutList make/validate round trips.
"""
import unittest

from src.utils import cut_ir

FPS = 24.0


def w(word, start, end, **extra):
    """A word row in the strata.read_words shape (seconds)."""
    row = {"word": word, "start_seconds": start, "end_seconds": end}
    row.update(extra)
    return row


class WordFillerTest(unittest.TestCase):
    def test_hesitation_cut_anywhere(self):
        words = [w("So", 0.0, 0.2), w("um", 0.25, 0.5), w("hello", 0.55, 0.9)]
        cuts = cut_ir.detect_cuts_words(words, fps=FPS)
        fillers = [c for c in cuts if c["kind"] == "filler"]
        self.assertEqual(len(fillers), 1)
        self.assertEqual(fillers[0]["evidence"]["word"], "um")
        # half-open frames at 24fps: 0.25s -> 6, 0.5s -> 12
        self.assertEqual(fillers[0]["span"], {"start": 6, "end": 12})

    def test_discourse_word_needs_isolation(self):
        # "like" flanked by speech with no gap: keep it.
        packed = [w("I", 0.0, 0.2), w("like", 0.22, 0.4), w("dogs", 0.42, 0.7)]
        self.assertEqual(cut_ir.detect_cuts_words(packed, fps=FPS), [])
        # "like" as an isolated interjection: cut it.
        isolated = [w("I", 0.0, 0.2), w("like", 0.8, 1.0), w("dogs", 1.6, 1.9)]
        kinds = [c["kind"] for c in cut_ir.detect_cuts_words(isolated, fps=FPS)]
        self.assertIn("filler", kinds)

    def test_filler_phrase_bigram(self):
        words = [w("it", 0.0, 0.1), w("was", 0.12, 0.3),
                 w("you", 0.35, 0.5), w("know", 0.52, 0.7),
                 w("great", 0.75, 1.0)]
        cuts = cut_ir.detect_cuts_words(words, fps=FPS)
        phrases = [c for c in cuts if c["evidence"].get("phrase")]
        self.assertEqual(len(phrases), 1)
        self.assertEqual(phrases[0]["evidence"]["phrase"], "you know")
        self.assertEqual(phrases[0]["span"]["start"], int(round(0.35 * FPS)))
        self.assertEqual(phrases[0]["span"]["end"], int(round(0.7 * FPS)))

    def test_frame_span_minimum_one_frame(self):
        words = [w("um", 1.0, 1.0)]  # zero-length word timing
        cuts = cut_ir.detect_cuts_words(words, fps=FPS)
        self.assertEqual(len(cuts), 1)
        span = cuts[0]["span"]
        self.assertGreater(span["end"], span["start"])


class FalseStartTest(unittest.TestCase):
    def test_repeated_phrase_cuts_first_occurrence(self):
        words = [w("I", 0.0, 0.1), w("was", 0.12, 0.3),
                 w("I", 0.6, 0.7), w("was", 0.72, 0.9),
                 w("going", 0.95, 1.2), w("home", 1.25, 1.5)]
        cuts = cut_ir.detect_cuts_words(words, fps=FPS)
        fs = [c for c in cuts if c["kind"] == "false_start"]
        self.assertEqual(len(fs), 1)
        # first occurrence only: 0.0..0.3s
        self.assertEqual(fs[0]["span"], {"start": 0, "end": int(round(0.3 * FPS))})
        self.assertEqual(fs[0]["evidence"]["repeat_len"], 2)

    def test_single_repeated_word_is_stammer(self):
        words = [w("I", 0.0, 0.1), w("I", 0.2, 0.3), w("think", 0.35, 0.6)]
        cuts = cut_ir.detect_cuts_words(words, fps=FPS)
        self.assertEqual([c["kind"] for c in cuts], ["stammer"])
        self.assertEqual(cuts[0]["span"]["start"], 0)

    def test_repeated_filler_not_double_counted(self):
        words = [w("um", 0.0, 0.2), w("um", 0.3, 0.5), w("right", 0.55, 0.8)]
        cuts = cut_ir.detect_cuts_words(words, fps=FPS)
        self.assertEqual([c["kind"] for c in cuts], ["filler", "filler"])

    def test_aborted_word(self):
        words = [w("tomor-", 0.0, 0.3), w("tomorrow", 0.4, 0.9)]
        cuts = cut_ir.detect_cuts_words(words, fps=FPS)
        fs = [c for c in cuts if c["kind"] == "false_start"]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["evidence"]["word"], "tomor-")

    def test_clean_speech_no_cuts(self):
        words = [w("the", 0.0, 0.1), w("quick", 0.12, 0.35),
                 w("brown", 0.37, 0.6), w("fox", 0.62, 0.85)]
        self.assertEqual(cut_ir.detect_cuts_words(words, fps=FPS), [])


class AutoFallbackTest(unittest.TestCase):
    def test_words_basis_when_timings_present(self):
        words = [w("um", 0.0, 0.2)]
        cues = [{"text": "um", "start": 0, "end": 5}]
        out = cut_ir.detect_cuts_auto(words, cues, fps=FPS)
        self.assertEqual(out["basis"], "words")
        self.assertEqual(out["basis_word_count"], 1)
        self.assertTrue(out["cuts"])

    def test_cue_fallback_without_word_timings(self):
        # Backend without word timestamps: rows lack usable start/end.
        words = [{"word": "um"}, {"word": "hello", "start_seconds": None}]
        cues = [{"text": "um", "start": 0, "end": 5},
                {"text": "the point", "start": 7, "end": 40}]
        out = cut_ir.detect_cuts_auto(words, cues, fps=FPS)
        self.assertEqual(out["basis"], "cues")
        self.assertEqual(out["basis_cue_count"], 2)
        self.assertIn("filler", [c["kind"] for c in out["cuts"]])

    def test_cue_fallback_with_no_words_at_all(self):
        out = cut_ir.detect_cuts_auto([], [], fps=FPS)
        self.assertEqual(out["basis"], "cues")
        self.assertEqual(out["cuts"], [])


class CutListSchemaTest(unittest.TestCase):
    def _valid_plan(self):
        seg = cut_ir.make_cut_list_segment(
            role="speech", clip_uuid="abc123",
            source_start_frame=0, source_end_frame=240,
            audio_track_indices=[1], transcript_excerpt="hello world",
            rationale="opening statement",
        )
        return cut_ir.make_cut_list(
            segments=[seg], fps=FPS, brief_id="brief1",
            titles=[{"text": "My Title", "at_frame": 0}],
            music={"path": "/media/song.wav",
                   "ducking": {"mode": "static", "user_approved_render": False}},
            removed=[cut_ir.make_cut("filler", 10, 15, "lift", 0.8, "um", {})],
        )

    def test_valid_plan_passes(self):
        plan = self._valid_plan()
        self.assertEqual(cut_ir.validate_cut_list(plan), [])
        self.assertEqual(plan["kind"], cut_ir.CUT_LIST_KIND)
        self.assertEqual(plan["estimates"]["duration_frames"], 240)
        self.assertEqual(plan["estimates"]["duration_seconds"], 10.0)

    def test_broll_excluded_from_runtime_estimate(self):
        speech = cut_ir.make_cut_list_segment(
            role="speech", clip_uuid="a", source_start_frame=0, source_end_frame=100)
        broll = cut_ir.make_cut_list_segment(
            role="broll", clip_uuid="b", source_start_frame=0, source_end_frame=50)
        plan = cut_ir.make_cut_list(segments=[speech, broll], fps=FPS)
        self.assertEqual(plan["estimates"]["duration_frames"], 100)

    def test_ducking_defaults_applied(self):
        plan = cut_ir.make_cut_list(
            segments=[cut_ir.make_cut_list_segment(
                role="speech", clip_uuid="a",
                source_start_frame=0, source_end_frame=10)],
            fps=FPS, music={"path": "/media/song.wav"})
        self.assertEqual(plan["music"]["ducking"]["mode"], "static")
        self.assertFalse(plan["music"]["ducking"]["user_approved_render"])

    def test_validators_catch_problems(self):
        plan = self._valid_plan()
        plan["segments"][0]["source_end_frame"] = 0  # not half-open
        plan["segments"][0]["role"] = "montage"      # bad role
        del plan["segments"][0]["clip_uuid"]         # no identity
        plan["music"]["ducking"]["mode"] = "sidechain"
        errors = cut_ir.validate_cut_list(plan)
        joined = "\n".join(errors)
        self.assertIn("end > start", joined)
        self.assertIn("role", joined)
        self.assertIn("clip_id or clip_uuid", joined)
        self.assertIn("ducking.mode", joined)

    def test_kind_and_empty_segments_rejected(self):
        errors = cut_ir.validate_cut_list({"kind": "other", "fps": 24, "segments": []})
        joined = "\n".join(errors)
        self.assertIn("kind", joined)
        self.assertIn("non-empty", joined)


if __name__ == "__main__":
    unittest.main()
