"""Tests for advanced_bridge.run_advanced_tool + scripts/advanced-bridge.mjs
(epic #37: generalizes drp-bridge.mjs's tool set to the full 18 tools for
pure file/DB-read offline compute — deliverable QC, conform analysis, etc.
that don't fit drp-bridge's drpPath-in/outputPath-out shape).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from src.utils import advanced_bridge as ab


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


class NodeUnavailableTests(unittest.TestCase):
    def test_refuses_without_node(self):
        with mock.patch.object(ab, "node_path", return_value=None):
            out = ab.run_advanced_tool("capabilities", "check", {})
        self.assertFalse(out["success"])
        self.assertIn("Node.js", out["error"])


@unittest.skipUnless(ab.node_available(), "Node required")
class RealBridgeDispatchTests(unittest.TestCase):
    def test_capabilities_tool_reachable(self):
        # capabilities is always available (pure-JS, no optional deps) — a
        # deterministic smoke test that the generic dispatch actually works.
        out = ab.run_advanced_tool("capabilities", "check", {})
        self.assertTrue(out.get("success"), out)
        self.assertIn("core", out["result"])

    def test_unknown_tool_refuses(self):
        out = ab.run_advanced_tool("not_a_real_tool", "foo", {})
        self.assertFalse(out.get("success"))
        self.assertIn("unknown tool", out["error"])


@unittest.skipUnless(ab.node_available() and _ffmpeg_available(),
                      "Node + ffmpeg required for a real deliverable_qc call")
class RealDeliverableQcTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="deliverable-qc-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.media = os.path.join(self.root, "out.mp4")
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", self.media,
        ], check=True, capture_output=True)

    def test_deliverable_qc_real_probe(self):
        out = ab.run_advanced_tool("deliverable", "deliverable_qc", {
            "file": self.media,
            "spec": {"video": {"codec": "h264", "width": 320, "height": 240, "fps": 24}},
        })
        self.assertTrue(out.get("success"), out)
        result = out["result"]
        self.assertEqual(result["gate"], "review")
        field_names = {f["field"] for f in result["fields"]}
        self.assertIn("video.codec", field_names)
        codec_field = next(f for f in result["fields"] if f["field"] == "video.codec")
        self.assertTrue(codec_field["pass"])

    def test_loudness_qc_real_probe(self):
        out = ab.run_advanced_tool("deliverable", "loudness_qc", {
            "file": self.media,
            "target": {"integrated": -23, "integratedTol": 20, "truePeakMax": 0, "lraMax": 30},
        })
        self.assertTrue(out.get("success"), out)


if __name__ == "__main__":
    unittest.main()
