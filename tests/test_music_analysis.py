"""Tests for the ffmpeg-only music_analysis util (bed gain + Phase-3 stub)."""
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


class BeatStubTest(unittest.TestCase):
    def test_beats_honestly_unavailable(self):
        out = music_analysis.detect_beats("/media/song.wav")
        self.assertFalse(out["success"])
        self.assertFalse(out["available"])
        self.assertNotIn("beats", out)


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


if __name__ == "__main__":
    unittest.main()
