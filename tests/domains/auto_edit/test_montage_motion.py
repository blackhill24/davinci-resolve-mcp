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

    # #209: the pulse table is now an exhaustive opt-in list — everything
    # not named in it, including sections that DO have their own zoom range,
    # must fall through to amp 0, not a nonzero "default" throb.
    def test_pulse_is_opt_in_the_default_is_zero(self):
        self.assertEqual(mm.DEFAULT_PULSE_AMP, 0.0)

    def test_only_drop_and_high_are_in_the_pulse_table(self):
        self.assertEqual(set(mm.MOTION_PULSE_AMP), {"drop", "high"})

    def test_high_section_pulses(self):
        directive = mm.compute_motion_directive("high", beat_seconds=0.5)
        self.assertEqual(directive["amp"], mm.MOTION_PULSE_AMP["high"])
        self.assertGreater(directive["amp"], 0)

    def test_mid_section_has_its_own_zoom_range_but_no_pulse(self):
        # A section can be a real, named entry in MOTION_ZOOM_RANGE and still
        # get amp 0 — zoom range and pulse amplitude are independent tables.
        directive = mm.compute_motion_directive("mid", beat_seconds=0.5)
        self.assertEqual(
            (directive["zoom_start"], directive["zoom_end"]),
            mm.MOTION_ZOOM_RANGE["mid"])
        self.assertEqual(directive["amp"], 0.0)

    def test_build_and_accelerate_no_longer_pulse(self):
        # Deliberately dropped from MOTION_PULSE_AMP (#209): both sections
        # already carry their own energy (MONTAGE_RETIME_SPEED slows them,
        # and the zoom ramp itself accelerates), so a pulse under a
        # tension-building section read as arriving before the drop earned it.
        for section in ("build", "accelerate"):
            with self.subTest(section=section):
                directive = mm.compute_motion_directive(section, beat_seconds=0.5)
                self.assertEqual(directive["amp"], 0.0)


class BuildZoomExpressionTest(unittest.TestCase):
    """Exact-string assertions for a known parameter set (the acceptance
    criterion's own wording) — plus numeric evaluation of that same string
    against Python's math, mirroring the C-like fmod/exp semantics verified
    live in the Fusion expression engine."""

    def _params(self):
        return dict(
            zoom_start=1.0, zoom_end=1.08, amp=0.06, beat_seconds=0.5545,
            fps=24.0, clip_length_frames=53,
        )

    def test_emitted_expression_string_for_known_params(self):
        expr = mm.build_zoom_expression(**self._params())
        self.assertEqual(
            expr,
            "1.000000+0.00150943*time+0.060000*exp(-7*fmod(time,13.308000)/13.308000)"
            "+0.025200*exp(-9*fmod(time+6.654000,13.308000)/13.308000)",
        )

    def _evaluate(self, expr: str, time: float) -> float:
        # A restricted eval mirroring the Fusion expression engine's fmod/exp
        # (verified live to behave like the C stdlib versions) plus the four
        # arithmetic operators — no other names are exposed.
        return eval(expr, {"__builtins__": {}}, {"time": time, "fmod": math.fmod, "exp": math.exp})

    def test_pulse_peaks_at_the_clips_own_first_frame(self):
        # Every grid-locked segment starts ON a beat, so comp-local frame 0
        # IS a beat and the pulse must peak there (#193 phase 3).
        beat_seconds, fps = 0.5545, 24.0
        beat_frames = beat_seconds * fps
        params = dict(self._params(), beat_seconds=beat_seconds, fps=fps)
        expr = mm.build_zoom_expression(**params)
        values = {t: self._evaluate(expr, t) for t in range(0, 4)}
        self.assertEqual(max(values, key=values.get), 0)
        mid_beat = self._evaluate(expr, round(beat_frames / 2))
        self.assertGreater(values[0], mid_beat)

        # the NEXT beat (one full beat_frames later) peaks the same way.
        next_beat_frame = round(beat_frames)
        next_window = range(next_beat_frame - 2, next_beat_frame + 3)
        next_values = {t: self._evaluate(expr, t) for t in next_window}
        next_peak_t = max(next_values, key=next_values.get)
        self.assertIn(next_peak_t - next_beat_frame, (-1, 0, 1))
        self.assertGreater(next_values[next_peak_t], mid_beat)

    def test_pulse_stays_on_the_beat_for_a_track_with_a_phase_offset(self):
        """Regression for #193 phase 3.

        The old formula folded ``record_start_frame`` into the modulo phase.
        A segment's start frame is congruent to ``beat_zero * fps`` modulo the
        beat period, so that shifted every peak by a constant ``beat_zero`` —
        up to half a beat off the beat the pulse is named after. It went
        unnoticed because this suite's fixture used ``beat_zero = 0``, the one
        value for which the bug is invisible. ``lock_phase`` returns a
        non-zero offset for essentially every real track.
        """
        beat_seconds, fps = 0.5545, 24.0
        beat_frames = beat_seconds * fps
        beat_zero = 0.31            # a real, non-zero phase lock
        # The grid montage_edit builds, and the record frames it derives.
        grid = [beat_zero + k * beat_seconds for k in range(8)]
        beat_grid_frames = [round(t * fps) for t in grid]
        # normalize_grid_phase slides the cut back so segment 0 sits at 0;
        # every segment start is then an exact multiple of the beat period.
        normalized = [f - beat_grid_frames[0] for f in beat_grid_frames]
        expr = mm.build_zoom_expression(
            **dict(self._params(), beat_seconds=beat_seconds, fps=fps))
        mid_beat = self._evaluate(expr, round(beat_frames / 2))
        for seg_start in normalized[:4]:
            # For a segment starting at `seg_start`, the beats inside it are
            # at comp-local times (beat_frame - seg_start). Each must peak.
            for beat_frame in normalized:
                local = beat_frame - seg_start
                if not 0 <= local <= 60:
                    continue
                window = range(max(0, local - 2), local + 3)
                values = {t: self._evaluate(expr, t) for t in window}
                peak_t = max(values, key=values.get)
                self.assertIn(
                    peak_t - local, (-1, 0, 1),
                    f"peak drifted off the beat at seg_start={seg_start}, beat={beat_frame}")
                self.assertGreater(values[peak_t], mid_beat)

    def test_ramp_moves_from_zoom_start_to_zoom_end_across_the_clip(self):
        params = self._params()
        expr = mm.build_zoom_expression(**params)
        # far from any beat pulse (mid-decay), the ramp component still
        # visibly separates start vs. end of the clip
        start_value = self._evaluate(expr, 3)
        end_value = self._evaluate(expr, params["clip_length_frames"] - 1)
        self.assertLess(start_value, end_value)

    # #209 acceptance criterion: a section not in MOTION_PULSE_AMP must
    # produce NO pulse — verified here by the emitted expression containing
    # no pulse term at all when amp is 0, not merely one that evaluates to 0.
    def test_zero_amp_omits_the_pulse_term_entirely(self):
        params = dict(self._params(), amp=0.0)
        expr = mm.build_zoom_expression(**params)
        self.assertNotIn("exp", expr)
        self.assertNotIn("fmod", expr)
        self.assertEqual(expr, "1.000000+0.00150943*time")

    def test_zero_amp_ramp_still_applies_and_varies_across_the_clip(self):
        # The ramp — and per-shot variation, which lives entirely in
        # zoom_start/zoom_end and is untouched by amp — must survive amp=0.
        params = dict(self._params(), amp=0.0, zoom_start=1.0, zoom_end=1.05)
        expr = mm.build_zoom_expression(**params)
        start_value = self._evaluate(expr, 0)
        end_value = self._evaluate(expr, params["clip_length_frames"] - 1)
        self.assertLess(start_value, end_value)

    def test_negative_amp_also_omits_the_pulse_term(self):
        params = dict(self._params(), amp=-0.01)
        expr = mm.build_zoom_expression(**params)
        self.assertNotIn("exp", expr)

    def test_positive_amp_still_pulses(self):
        params = self._params()  # amp=0.06, the fixture default
        expr = mm.build_zoom_expression(**params)
        self.assertIn("exp", expr)
        self.assertIn("fmod", expr)

    def test_zero_or_negative_beat_seconds_does_not_divide_by_zero(self):
        expr = mm.build_zoom_expression(
            zoom_start=1.0, zoom_end=1.02, amp=0.02, beat_seconds=0.0,
            fps=24.0, clip_length_frames=48)
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


class BuildShakeExpressionTest(unittest.TestCase):
    """The `shake` flag phase 2 has emitted since #177 but nothing consumed."""

    def _evaluate(self, expr: str, time: float) -> float:
        return eval(expr, {"__builtins__": {}},
                    {"time": time, "fmod": math.fmod, "exp": math.exp, "sin": math.sin})

    def test_jitter_is_strongest_at_the_beat_and_decays_before_the_next(self):
        beat_seconds, fps = 0.5, 24.0
        beat_frames = beat_seconds * fps
        expr = mm.build_shake_expression(beat_seconds=beat_seconds, fps=fps)
        # Sample a whole beat: the largest swing lives in the first third,
        # and the tail before the next beat is effectively still.
        on_beat = max(abs(self._evaluate(expr, t))
                      for t in range(0, int(beat_frames / 3)))
        pre_next_beat = max(abs(self._evaluate(expr, t))
                            for t in range(int(beat_frames * 0.8), int(beat_frames)))
        self.assertGreater(on_beat, pre_next_beat * 3)

    def test_amplitude_never_exceeds_the_configured_ceiling(self):
        expr = mm.build_shake_expression(beat_seconds=0.5, fps=24.0)
        peak = max(abs(self._evaluate(expr, t)) for t in range(0, 48))
        self.assertLessEqual(peak, mm.SHAKE_MAX_DEGREES)

    def test_phase_is_plain_comp_local_time(self):
        # #193 phase 3: a grid-locked segment starts ON a beat, so comp-local
        # time is already the beat phase. The old record_start_frame term put
        # every peak a constant beat_zero off the beat.
        expr = mm.build_shake_expression(beat_seconds=0.5, fps=24.0)
        self.assertIn("fmod(time,", expr)
        self.assertNotIn("time+", expr)

    def test_zero_beat_seconds_does_not_divide_by_zero(self):
        expr = mm.build_shake_expression(beat_seconds=0.0, fps=24.0)
        self._evaluate(expr, 0)


class BuildFadeoutExpressionTest(unittest.TestCase):
    """The `fadeout` flag phase 2 has emitted since #177 but nothing consumed."""

    def _evaluate(self, expr: str, time: float) -> float:
        return eval(expr, {"__builtins__": {}},
                    {"time": time, "min": min, "max": max})

    def test_full_gain_until_the_fade_starts_then_reaches_black(self):
        fps, clip_len = 24.0, 120
        expr = mm.build_fadeout_expression(fps=fps, clip_length_frames=clip_len)
        fade_frames = mm.FADEOUT_SECONDS * fps
        self.assertAlmostEqual(self._evaluate(expr, 0), 1.0)
        self.assertAlmostEqual(self._evaluate(expr, clip_len - fade_frames), 1.0)
        self.assertAlmostEqual(self._evaluate(expr, clip_len), 0.0)
        # monotonically down across the fade, never negative
        mid = self._evaluate(expr, clip_len - fade_frames / 2)
        self.assertTrue(0.0 < mid < 1.0)

    def test_clamped_so_it_never_leaves_the_zero_one_range(self):
        expr = mm.build_fadeout_expression(fps=24.0, clip_length_frames=120)
        for t in (-10, 0, 60, 119, 120, 500):
            self.assertGreaterEqual(self._evaluate(expr, t), 0.0)
            self.assertLessEqual(self._evaluate(expr, t), 1.0)

    def test_short_clip_fades_across_itself_not_from_mid_black(self):
        # A 6-frame outro shot is shorter than the nominal 1s fade — it must
        # still start at full gain rather than partway into the ramp.
        expr = mm.build_fadeout_expression(fps=24.0, clip_length_frames=6)
        self.assertAlmostEqual(self._evaluate(expr, 0), 1.0)
        self.assertAlmostEqual(self._evaluate(expr, 6), 0.0)


if __name__ == "__main__":
    unittest.main()
