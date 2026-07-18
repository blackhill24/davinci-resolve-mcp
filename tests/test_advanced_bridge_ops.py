"""Tests for the drp-format OP path of src/utils/advanced_bridge (t11).

The existing tests/test_advanced_bridge.py covers the read-only panel/lineage
bridge. This file covers the reusable util and its WRITE bridge (drp-bridge.mjs),
which invokes drp-format vendor ops on an exported ``.drt`` in scratch.

Two prongs, matching the t11 acceptance criteria:

1. Honest refusal when Node is unavailable (no Node subprocess needed — we stub
   the Node lookup, so this always runs in the offline suite).
2. The bridge actually invokes drp-format vendor ops on a scratch ``.drt`` and
   writes the mutated timeline to scratch, never touching the source. This prong
   needs Node + the resolve-advanced deps installed, so it skips cleanly when they
   are not (e.g. the dependency-light publish gate).
"""

import os
import tempfile
import unittest
import zipfile

from src.utils import advanced_bridge as ab


def _advanced_ready() -> bool:
    """Node on PATH AND resolve-advanced deps installed (jszip is the tell)."""
    if not ab.node_available():
        return False
    return os.path.isdir(os.path.join(ab.advanced_root(), "node_modules", "jszip"))


def _synth_drt(path: str) -> None:
    """A minimal exported-timeline container: two abutting clips, cut at 100.

    Mirrors the vendor place-transition fixture (c0 [0,100), c1 [100,200)) as a
    JSZip-readable zip with a single SeqContainer — enough for a real op to bite.
    """
    def clip(i: int) -> str:
        return (
            f'<Element><Sm2TiVideoClip DbId="c{i}"><FieldsBlob/><Name>c{i}</Name>'
            f"<Start>{i * 100}</Start><Duration>100</Duration><In/></Sm2TiVideoClip></Element>"
        )

    track = (
        '<Element><Sm2TiTrack DbId="t"><FieldsBlob/><Type>0</Type><SubType>0</SubType>'
        f"<Flags>0</Flags><Sequence>s</Sequence><Items>{clip(0)}{clip(1)}</Items>"
        "<FusionCompHolderItems/><UserDefinedName/><LayersVec/></Sm2TiTrack></Element>"
    )
    seq = (
        '<?xml version="1.0"?>\n<Sm2SequenceContainer DbId="s1"><FieldsBlob/>'
        f"<VideoTrackVec>{track}</VideoTrackVec><AudioTrackVec/></Sm2SequenceContainer>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("SeqContainer/s1.xml", seq)


class NodeUnavailableTest(unittest.TestCase):
    """Every entry point refuses cleanly (structured, no exception) with no Node."""

    def setUp(self):
        self._orig = ab.node_path
        ab.node_path = lambda: None  # simulate Node absent

    def tearDown(self):
        ab.node_path = self._orig

    def test_run_node_bridge_refuses(self):
        out = ab.run_node_bridge("scripts/drp-bridge.mjs", ["drp", "split_clip", "{}"])
        self.assertFalse(out["success"])
        self.assertIn("Node.js not found", out["error"])
        self.assertIn("hint", out)

    def test_run_panel_bridge_refuses(self):
        out = ab.run_panel_bridge("capabilities", "get")
        self.assertFalse(out["success"])
        self.assertIn("Node.js not found", out["error"])

    def test_run_drp_op_refuses_before_touching_source(self):
        out = ab.run_drp_op("split_clip", "/nonexistent/whatever.drt")
        self.assertFalse(out["success"])
        self.assertIn("Node.js not found", out["error"])


class MissingBridgeTest(unittest.TestCase):
    def test_missing_bridge_script_reported(self):
        if not ab.node_available():
            self.skipTest("node not on PATH")
        out = ab.run_node_bridge("scripts/does-not-exist.mjs", ["drp", "x", "{}"])
        self.assertFalse(out["success"])
        self.assertIn("advanced bridge missing", out["error"])


@unittest.skipUnless(_advanced_ready(), "node + resolve-advanced deps required")
class DrpOpIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drm-adv-bridge-test-")
        self.src = os.path.join(self.tmp, "exported.drt")
        _synth_drt(self.src)
        self.src_bytes = os.path.getsize(self.src)

    def test_missing_source_refused(self):
        out = ab.run_drp_op("split_clip", os.path.join(self.tmp, "ghost.drt"))
        self.assertFalse(out["success"])
        self.assertIn("not found", out["error"])

    def test_unknown_tool_refused(self):
        out = ab.run_drp_op("split_clip", self.src, tool="bogus")
        self.assertFalse(out["success"])
        self.assertIn("unknown tool", out["error"])

    def test_place_transition_writes_mutated_timeline_to_scratch(self):
        scratch = os.path.join(self.tmp, "scratch")
        out = ab.run_drp_op(
            "place_transition",
            self.src,
            scratch_dir=scratch,
            track=1,
            atFrame=100,
            durationFrames=24,
        )
        self.assertTrue(out.get("success"), out)
        # Output landed under scratch, not beside the source.
        self.assertTrue(os.path.isfile(out["output_path"]))
        self.assertTrue(out["output_path"].startswith(scratch))
        self.assertNotEqual(os.path.abspath(out["output_path"]), os.path.abspath(self.src))
        # Source untouched (scratch discipline).
        self.assertEqual(os.path.getsize(self.src), self.src_bytes)
        # The op actually did work (drp-format returns accounting).
        result = out.get("result") or {}
        self.assertIn("outputPath", result)
        self.assertGreater(result.get("bytes", 0), 0)
        # The written container really contains an Sm2TiTransition now.
        with zipfile.ZipFile(out["output_path"]) as z:
            seq_xml = z.read("SeqContainer/s1.xml").decode("utf-8")
        self.assertIn("Sm2TiTransition", seq_xml)


if __name__ == "__main__":
    unittest.main()
