"""Tests for src/domains/auto_edit/utils/montage_motion.py (issue #180,
phase 5/6 of the montage-quality epic). Pure expression generation — no
Resolve, no Fusion. The exact expression syntax (comp-local `time`, `fmod`,
`exp`) was verified live against Resolve Studio 21.0.2.4's Fusion expression
engine before writing this module; see the phase-5 PR description.
"""
from __future__ import annotations

import math
import re
import unittest

from src.domains.auto_edit.utils import montage_motion as mm


class ComputeMotionDirectiveTest(unittest.TestCase):
    def test_known_section_maps_to_its_own_zoom_and_amp(self):
        directive = mm.compute_motion_directive("drop", beat_seconds=0.5545)
        self.assertEqual(directive["zoom_start"], 1.0)
        self.assertEqual(directive["zoom_end"], 1.08)
        self.assertEqual(directive["amp"], mm.MOTION_PULSE_AMP["drop"])
        self.assertEqual(directive["beat_seconds"], 0.5545)

    def test_unknown_or_missing_section_gets_the_default(self):
        directive = mm.compute_motion_directive(None, beat_seconds=0.5)
        self.assertEqual(
            (directive["zoom_start"], directive["zoom_end"]), mm.DEFAULT_ZOOM_RANGE)
        self.assertEqual(directive["amp"], mm.DEFAULT_PULSE_AMP)


class BuildZoomExpressionTest(unittest.TestCase):
    """Exact-string assertions for a known parameter set (the acceptance
    criterion's own wording) — plus numeric evaluation of that same string
    against Python's math, mirroring the C-like fmod/exp semantics verified
    live in the Fusion expression engine."""

    def _params(self):
        return dict(
            zoom_start=1.0, zoom_end=1.08, amp=0.06, beat_seconds=0.5545,
            fps=24.0, record_start_frame=48, clip_length_frames=53,
        )

    def test_emitted_expression_string_for_known_params(self):
        expr = mm.build_zoom_expression(**self._params())
        self.assertEqual(
            expr,
            "1.000000+0.00150943*time+0.060000*exp(-7*fmod((time+48),13.308000)/13.308000)"
            "+0.025200*exp(-9*fmod((time+48)+6.654000,13.308000)/13.308000)",
        )

    def _evaluate(self, expr: str, time: float) -> float:
        # A restricted eval mirroring the Fusion expression engine's fmod/exp
        # (verified live to behave like the C stdlib versions) plus the four
        # arithmetic operators — no other names are exposed.
        return eval(expr, {"__builtins__": {}}, {"time": time, "fmod": math.fmod, "exp": math.exp})

    def test_pulse_peaks_at_the_beat_frame_not_mid_beat(self):
        # record_start_frame must itself be a beat-grid frame (phase 2's
        # guarantee) for the phase offset to line up — derive it the same
        # way montage_edit does: beat_frames[k], a member of the ROUNDED
        # cumulative grid (never k * beat_frames computed independently,
        # which can be off by the same sub-frame rounding this test must
        # therefore tolerate too — real record frames are only ever accurate
        # to +/-1 frame, same as everywhere else in this codebase).
        beat_seconds, fps = 0.5545, 24.0
        beat_frames = beat_seconds * fps
        beat_grid_frames = [round(k * beat_frames) for k in range(6)]
        record_start_frame = beat_grid_frames[4]
        params = dict(self._params(), record_start_frame=record_start_frame,
                      beat_seconds=beat_seconds, fps=fps)
        expr = mm.build_zoom_expression(**params)
        window = range(-2, 3)
        values = {t: self._evaluate(expr, t) for t in window}
        peak_t = max(values, key=values.get)
        # the peak lands within +/-1 frame of the nominal beat (0) — the
        # only slack frame quantization allows — and is clearly higher than
        # mid-beat values further from any beat.
        self.assertIn(peak_t, (-1, 0, 1))
        mid_beat = self._evaluate(expr, round(beat_frames / 2))
        self.assertGreater(values[peak_t], mid_beat)

        # the NEXT beat (one full beat_frames later) peaks the same way.
        next_beat_frame = beat_grid_frames[5] - record_start_frame
        next_window = range(next_beat_frame - 2, next_beat_frame + 3)
        next_values = {t: self._evaluate(expr, t) for t in next_window}
        next_peak_t = max(next_values, key=next_values.get)
        self.assertIn(next_peak_t - next_beat_frame, (-1, 0, 1))
        self.assertGreater(next_values[next_peak_t], mid_beat)

    def test_ramp_moves_from_zoom_start_to_zoom_end_across_the_clip(self):
        params = self._params()
        expr = mm.build_zoom_expression(**params)
        # far from any beat pulse (mid-decay), the ramp component still
        # visibly separates start vs. end of the clip
        start_value = self._evaluate(expr, 3)
        end_value = self._evaluate(expr, params["clip_length_frames"] - 1)
        self.assertLess(start_value, end_value)

    def test_zero_or_negative_beat_seconds_does_not_divide_by_zero(self):
        expr = mm.build_zoom_expression(
            zoom_start=1.0, zoom_end=1.02, amp=0.02, beat_seconds=0.0,
            fps=24.0, record_start_frame=0, clip_length_frames=48)
        # must still be a valid, evaluable expression (falls back to 1 frame)
        self._evaluate(expr, 0)


class BuildFlashExpressionTest(unittest.TestCase):
    def test_peaks_at_the_clips_own_first_frame_and_decays(self):
        expr = mm.build_flash_expression()

        def ev(t):
            return eval(expr, {"__builtins__": {}}, {"time": t, "exp": math.exp})

        self.assertAlmostEqual(ev(0), mm.FLASH_GAIN)
        self.assertLess(ev(1), ev(0))
        self.assertLess(ev(mm.FLASH_DECAY_FRAMES * 2), 1.05)  # effectively gone

    def test_expression_has_no_phase_offset(self):
        # a flash is a one-shot at the cut itself, not beat-locked — no
        # record_start_frame term should appear in the string at all.
        expr = mm.build_flash_expression()
        self.assertNotIn("fmod", expr)
        self.assertTrue(re.match(r"^1\.0\+[\d.]+\*exp\(-[\d.]+\*time\)$", expr))


if __name__ == "__main__":
    unittest.main()
