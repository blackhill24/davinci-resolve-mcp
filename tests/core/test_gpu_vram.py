"""Tests for the RemoveMotionBlur VRAM precondition check (#188)."""
import subprocess
import unittest
from unittest import mock

from src.core import gpu_vram as G


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=["nvidia-smi"], returncode=returncode, stdout=stdout, stderr="")


class FreeVramMibTest(unittest.TestCase):
    def test_parses_first_line(self):
        with mock.patch.object(G.subprocess, "run", return_value=_completed("6144\n")):
            self.assertEqual(G.free_vram_mib(), 6144)

    def test_nonzero_exit_is_unknown(self):
        with mock.patch.object(G.subprocess, "run", return_value=_completed("", returncode=1)):
            self.assertIsNone(G.free_vram_mib())

    def test_missing_binary_is_unknown(self):
        with mock.patch.object(G.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(G.free_vram_mib())

    def test_timeout_is_unknown(self):
        with mock.patch.object(G.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=20)):
            self.assertIsNone(G.free_vram_mib())

    def test_empty_stdout_is_unknown(self):
        with mock.patch.object(G.subprocess, "run", return_value=_completed("")):
            self.assertIsNone(G.free_vram_mib())

    def test_non_numeric_stdout_is_unknown(self):
        with mock.patch.object(G.subprocess, "run", return_value=_completed("N/A\n")):
            self.assertIsNone(G.free_vram_mib())


class InsufficientVramErrorTest(unittest.TestCase):
    def test_below_floor_returns_error(self):
        with mock.patch.object(G, "free_vram_mib", return_value=4000):
            err = G.insufficient_vram_error(min_free_mib=12000)
        self.assertIn("error", err)
        self.assertEqual(err["free_vram_mib"], 4000)
        self.assertEqual(err["required_vram_mib"], 12000)

    def test_at_or_above_floor_returns_none(self):
        with mock.patch.object(G, "free_vram_mib", return_value=12000):
            self.assertIsNone(G.insufficient_vram_error(min_free_mib=12000))
        with mock.patch.object(G, "free_vram_mib", return_value=16000):
            self.assertIsNone(G.insufficient_vram_error(min_free_mib=12000))

    def test_unknown_vram_never_blocks(self):
        with mock.patch.object(G, "free_vram_mib", return_value=None):
            self.assertIsNone(G.insufficient_vram_error(min_free_mib=12000))

    def test_default_floor_matches_measured_repro(self):
        self.assertEqual(G.REMOVE_MOTION_BLUR_MIN_FREE_MIB, 12000)


if __name__ == "__main__":
    unittest.main()
