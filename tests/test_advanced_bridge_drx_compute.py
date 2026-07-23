"""Tests for advanced_bridge.run_drx_compute (epic #37 phase A: offline
compute-then-apply for the orchestrate grade stage).

Two prongs, matching test_advanced_bridge_ops.py's posture:
1. Honest refusal when Node is unavailable (stubbed — always runs offline).
2. A real drx compute call against synthetic frames — needs Node + the
   resolve-advanced deps (sharp/zod/jszip), skips cleanly when absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from src.core import advanced_bridge as ab


def _advanced_ready() -> bool:
    if not ab.node_available():
        return False
    node_modules = os.path.join(ab.advanced_root(), "node_modules")
    return all(os.path.isdir(os.path.join(node_modules, dep)) for dep in ("jszip", "sharp", "zod"))


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


class NodeUnavailableTests(unittest.TestCase):
    def test_run_drx_compute_refuses_without_node(self):
        with mock.patch.object(ab, "node_path", return_value=None):
            out = ab.run_drx_compute("level_clips", {"clips": [], "outDir": "/tmp"})
        self.assertFalse(out["success"])
        self.assertIn("Node.js", out["error"])


@unittest.skipUnless(_advanced_ready() and _ffmpeg_available(),
                      "Node + resolve-advanced deps (jszip/sharp/zod) + ffmpeg required")
class RealComputeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="drx-compute-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _synth_frame(self, name: str, color: str) -> str:
        out = os.path.join(self.root, f"{name}.png")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=64x64",
             "-frames:v", "1", out],
            check=True, capture_output=True,
        )
        return out

    def test_level_clips_computes_real_drx_files(self):
        clip_a = self._synth_frame("clip_a", "gray")
        clip_b = self._synth_frame("clip_b", "gray")
        out_dir = os.path.join(self.root, "grades")
        result = ab.run_drx_compute("level_clips", {
            "clips": [
                {"id": "a", "png": clip_a, "group": "cam1"},
                {"id": "b", "png": clip_b, "group": "cam1"},
            ],
            "outDir": out_dir,
        })
        self.assertTrue(result.get("success"), result)
        grades = result["result"]["grades"]
        self.assertEqual(len(grades), 2)
        for grade in grades:
            self.assertTrue(os.path.isfile(grade["drxPath"]), grade)

    def test_unknown_action_refuses(self):
        result = ab.run_drx_compute("not_a_real_action", {"clips": [], "outDir": self.root})
        self.assertFalse(result.get("success"))


if __name__ == "__main__":
    unittest.main()
