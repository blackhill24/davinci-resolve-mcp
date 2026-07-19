"""Tests for the ffmpeg-only music_analysis util (bed gain + beat/onset)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from src.utils import music_analysis

EBUR128_SUMMARY = """
[Parsed_ebur128_0 @ 0x55] Summary:

  Integrated loudness:
    I:         -18.3 LUFS
    Threshold: -28.6 LUFS

  Loudness range:
    LRA:         6.4 LU
    Threshold: -38.9 LUFS
    LRA low:   -22.1 LUFS
    LRA high:  -15.7 LUFS

  True peak:
    Peak:       -1.2 dBFS
"""


class BedGainTest(unittest.TestCase):
    def test_gain_moves_measured_to_bed_target(self):
        # -18.3 LUFS measured, bed target -30: gain = -11.7
        self.assertEqual(music_analysis.bed_gain_db(-18.3), -11.7)

    def test_custom_targets(self):
        gain = music_analysis.bed_gain_db(
            -20.0, dialogue_target_lufs=-16.0, bed_offset_lu=-10.0)
        self.assertEqual(gain, -6.0)

    def test_boost_clamped(self):
        self.assertEqual(
            music_analysis.bed_gain_db(-60.0), music_analysis.MAX_BED_GAIN_DB)

    def test_unusable_measurement_returns_none(self):
        self.assertIsNone(music_analysis.bed_gain_db(None))
        self.assertIsNone(music_analysis.bed_gain_db("loud"))


class AnalyzeMusicBedTest(unittest.TestCase):
    def test_derives_gain_from_ebur128_output(self):
        with mock.patch.object(
            music_analysis, "_ffmpeg_stderr_filter",
            return_value=(0, EBUR128_SUMMARY),
        ) as run:
            out = music_analysis.analyze_music_bed("/media/song.wav")
        run.assert_called_once_with("/media/song.wav", audio_filter="ebur128=peak=true")
        self.assertTrue(out["success"])
        self.assertEqual(out["metrics"]["integrated_lufs"], -18.3)
        self.assertEqual(out["metrics"]["loudness_range_lu"], 6.4)
        self.assertEqual(out["metrics"]["true_peak_dbtp"], -1.2)
        self.assertEqual(out["target_bed_lufs"], -30.0)
        self.assertEqual(out["gain_db"], -11.7)

    def test_failed_ffmpeg_reports_error_and_no_gain(self):
        with mock.patch.object(
            music_analysis, "_ffmpeg_stderr_filter", return_value=(1, "boom")
        ):
            out = music_analysis.analyze_music_bed("/media/missing.wav")
        self.assertFalse(out["success"])
        self.assertIn("error", out)
        self.assertIsNone(out["gain_db"])

    def test_unparseable_summary_yields_none_gain(self):
        with mock.patch.object(
            music_analysis, "_ffmpeg_stderr_filter", return_value=(0, "no summary here")
        ):
            out = music_analysis.analyze_music_bed("/media/silent.wav")
        self.assertTrue(out["success"])
        self.assertIsNone(out["gain_db"])


class BeatDetectionTest(unittest.TestCase):
    def test_missing_file_honestly_unavailable(self):
        out = music_analysis.detect_beats("/media/does-not-exist.wav")
        self.assertFalse(out["success"])
        self.assertFalse(out["available"])
        self.assertNotIn("onsets", out)

    def test_undecodable_track_honestly_unavailable(self):
        # ffmpeg present but decode fails → honest unavailable, no fabricated grid.
        with mock.patch.object(music_analysis, "_decode_pcm_mono",
                               return_value=(None, music_analysis.BEAT_SAMPLE_RATE)):
            with mock.patch("os.path.isfile", return_value=True):
                out = music_analysis.detect_beats("/media/song.wav")
        self.assertFalse(out["success"])
        self.assertFalse(out["available"])
        self.assertNotIn("onsets", out)

    def test_onset_novelty_fires_on_energy_rise(self):
        # Silence → burst → silence: novelty must spike at the burst onset only.
        import array
        sr = 22050
        quiet = [0.0] * sr
        loud = [0.5 if i % 2 else -0.5 for i in range(sr)]  # full-scale-ish square
        samples = array.array("f", quiet + loud + quiet)
        times, novelty = music_analysis.onset_novelty(samples, sr)
        self.assertTrue(times and novelty)
        peak_t = times[max(range(len(novelty)), key=lambda k: novelty[k])]
        self.assertAlmostEqual(peak_t, 1.0, delta=0.1)  # burst starts at ~1.0s

    def test_pick_onsets_spacing_and_threshold(self):
        # Three clear novelty spikes, well separated → three onsets.
        times = [i * 0.05 for i in range(60)]
        novelty = [0.0] * 60
        for k in (10, 30, 50):
            novelty[k] = 1.0
        onsets = music_analysis.pick_onsets(times, novelty, sensitivity=1.5,
                                            min_gap_seconds=0.12)
        self.assertEqual(len(onsets), 3)
        self.assertAlmostEqual(onsets[0], 0.5, delta=1e-6)

    def test_estimate_tempo_from_even_onsets(self):
        # Onsets every 0.5s → 120 BPM.
        onsets = [round(0.5 * i, 3) for i in range(9)]
        self.assertAlmostEqual(music_analysis.estimate_tempo_bpm(onsets), 120.0, delta=0.5)

    def test_estimate_tempo_none_when_too_few(self):
        self.assertIsNone(music_analysis.estimate_tempo_bpm([1.0]))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
    def test_detect_beats_end_to_end_on_click_track(self):
        # A 120-BPM metronome authored as a real WAV (deterministic), then decoded
        # through ffmpeg by detect_beats: 20 ms decaying tone bursts every 0.5 s
        # for 6 s → 12 clicks.
        import math
        import struct
        import wave

        sr = 22050
        total = sr * 6
        click_len = int(0.02 * sr)
        buf = bytearray(total * 2)
        for beat in range(12):
            start = int(beat * 0.5 * sr)
            for i in range(click_len):
                idx = start + i
                if idx >= total:
                    break
                env = 1.0 - i / click_len
                val = int(0.7 * env * math.sin(2 * math.pi * 880 * i / sr) * 32767)
                struct.pack_into("<h", buf, idx * 2, val)

        tmp = tempfile.mkdtemp(prefix="drm-beats-")
        wav = os.path.join(tmp, "click.wav")
        with wave.open(wav, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(bytes(buf))

        out = music_analysis.detect_beats(wav)
        self.assertTrue(out["success"], out)
        self.assertTrue(out["available"])
        # ~12 onsets; allow slack for edge frames and threshold warm-up.
        self.assertGreaterEqual(out["onset_count"], 8)
        self.assertLessEqual(out["onset_count"], 16)
        self.assertIsNotNone(out["tempo_bpm"])
        self.assertAlmostEqual(out["tempo_bpm"], 120.0, delta=15.0)


class RenderDuckedBedTest(unittest.TestCase):
    def test_refuses_without_checkpoint_consent(self):
        out = music_analysis.render_ducked_bed(
            "/media/song.wav", "/root/analysis/bed.wav",
            duration_seconds=10.0, gain_db=-11.7,
            user_approved_render=False)
        self.assertFalse(out["success"])
        self.assertTrue(out["refused"])
        self.assertIn("static", out["error"])

    def test_invalid_duration_rejected(self):
        out = music_analysis.render_ducked_bed(
            "/media/song.wav", "/root/analysis/bed.wav",
            duration_seconds=0, user_approved_render=True)
        self.assertFalse(out["success"])
        self.assertNotIn("refused", out)

    def test_consented_render_builds_ffmpeg_filtergraph(self):
        import os, tempfile, shutil
        tmp = tempfile.mkdtemp(prefix="bed-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        bed_path = os.path.join(tmp, "sub", "bed.wav")
        captured = {}

        def fake_run(args, timeout=0):
            captured["args"] = args
            with open(bed_path, "wb") as handle:
                handle.write(b"RIFF")
            return 0, "", ""

        with mock.patch.object(music_analysis, "_run_command", side_effect=fake_run):
            out = music_analysis.render_ducked_bed(
                "/media/song.wav", bed_path,
                duration_seconds=10.0, gain_db=-11.7,
                user_approved_render=True)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["output_path"], bed_path)
        self.assertEqual(out["mode"], "rendered_bed")
        joined = " ".join(captured["args"])
        self.assertIn("volume=-11.7dB", joined)
        self.assertIn("afade=t=in", joined)
        self.assertIn("afade=t=out:st=9.0", joined)
        self.assertIn("-t 10.0", joined)

    def test_ffmpeg_failure_reported(self):
        with mock.patch.object(music_analysis, "_run_command", return_value=(1, "", "boom")):
            out = music_analysis.render_ducked_bed(
                "/media/song.wav", "/tmp/nonexistent-dir-xyz/bed.wav",
                duration_seconds=5.0, user_approved_render=True)
        self.assertFalse(out["success"])
        self.assertIn("boom", out["error"])


class DuckingModeLadderTest(unittest.TestCase):
    """The ducking-mode vocabulary (issue #14 groundwork)."""

    def test_rendered_bed_reports_its_mode_constant(self):
        with mock.patch.object(music_analysis, "_run_command", return_value=(0, "", "")), \
             mock.patch("os.path.isfile", return_value=True):
            out = music_analysis.render_ducked_bed(
                "/media/song.wav", "/tmp/bed.wav",
                duration_seconds=5.0, user_approved_render=True)
        self.assertEqual(out["mode"], music_analysis.DUCKING_RENDERED_BED)

    def test_drt_automation_implemented_xmeml_still_reserved(self):
        # drt_automation is now a real path (encoding verified live, issue #14).
        # xmeml_keyframes stays reserved — the drt route made it unnecessary, and
        # no code path may claim an unproven tier.
        self.assertIn(music_analysis.DUCKING_DRT_AUTOMATION, music_analysis.DUCKING_MODES_ALL)
        self.assertIn(music_analysis.DUCKING_XMEML_KEYFRAMES, music_analysis.DUCKING_MODES_ALL)
        self.assertIn(
            music_analysis.DUCKING_DRT_AUTOMATION, music_analysis.DUCKING_MODES_IMPLEMENTED)
        self.assertNotIn(
            music_analysis.DUCKING_XMEML_KEYFRAMES, music_analysis.DUCKING_MODES_IMPLEMENTED)
        self.assertEqual(
            music_analysis.DUCKING_MODES_IMPLEMENTED,
            {music_analysis.DUCKING_STATIC, music_analysis.DUCKING_RENDERED_BED,
             music_analysis.DUCKING_DRT_AUTOMATION})


if __name__ == "__main__":
    unittest.main()
