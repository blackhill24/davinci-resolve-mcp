"""Tests for the drp-format OP path of src/core/advanced_bridge (t11).

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

from src.core import advanced_bridge as ab


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
    # Identity retime map (compact form): [02][end,0,end,0,end], end = 4 s.
    end = __import__("struct").pack(">d", 4.0).hex()
    zero = __import__("struct").pack(">d", 0.0).hex()
    timemap = f"02{end}{zero}{end}{zero}{end}"

    def clip(i: int) -> str:
        return (
            f'<Element><Sm2TiVideoClip DbId="c{i}"><FieldsBlob/><Name>c{i}</Name>'
            f"<Start>{i * 100}</Start><Duration>100</Duration><In>20</In>"
            f"<MediaTimemapBA>{timemap}</MediaTimemapBA></Sm2TiVideoClip></Element>"
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

    def test_op_chain_threads_multiple_ops_onto_one_timeline(self):
        # This is the polish_timeline mechanism: several drp-format ops applied
        # in sequence to ONE exported .drt, each reading the previous output.
        scratch = os.path.join(self.tmp, "chain")
        ops = [
            {"op": "place_transition", "args": {"track": 1, "atFrame": 100, "durationFrames": 24}},
            {"op": "split_clip", "args": {"track": 1, "at": 50}},
        ]
        out = ab.run_drp_op_chain(ops, self.src, scratch_dir=scratch)
        self.assertTrue(out.get("success"), out)
        self.assertEqual(len(out["steps"]), 2)
        self.assertTrue(all(s["success"] for s in out["steps"]))
        # Final output threads through both ops and lands under scratch.
        self.assertTrue(os.path.isfile(out["output_path"]))
        self.assertTrue(out["output_path"].startswith(scratch))
        # Source untouched.
        self.assertEqual(os.path.getsize(self.src), self.src_bytes)
        # The transition placed by op 0 survived into the final container.
        with zipfile.ZipFile(out["output_path"]) as z:
            seq_xml = z.read("SeqContainer/s1.xml").decode("utf-8")
        self.assertIn("Sm2TiTransition", seq_xml)

    def test_retime_clip_swaps_timemap_and_scales_duration(self):
        # 3.1.5 (#30): 0.5x retime swaps the identity map for an Sm2TimeMap
        # keyed-dict and doubles the record Duration.
        out = ab.run_drp_op(
            "retime_clip", self.src, scratch_dir=os.path.join(self.tmp, "retime"),
            track=1, clipIndex=0, speed=0.5)
        self.assertTrue(out.get("success"), out)
        result = out.get("result") or {}
        self.assertEqual(result.get("oldDuration"), 100)
        self.assertEqual(result.get("newDuration"), 200)
        with zipfile.ZipFile(out["output_path"]) as z:
            seq_xml = z.read("SeqContainer/s1.xml").decode("utf-8")
        # The retimed blob is an Sm2TimeMap keyed-dict; its DbType string shows
        # up UTF-16-BE-encoded in the hex MediaTimemapBA payload.
        self.assertIn("Sm2TimeMap".encode("utf-16-be").hex(), seq_xml)
        self.assertIn("<Duration>200</Duration>", seq_xml)

    def test_slip_clip_retreats_the_in_point(self):
        # 3.1.5 (#30): the single-op slip retreat (frames < 0) trim_clip_head
        # could never do — In goes 20 -> 5, Start/Duration untouched.
        out = ab.run_drp_op(
            "slip_clip", self.src, scratch_dir=os.path.join(self.tmp, "slip"),
            track=1, clipIndex=0, frames=-15)
        self.assertTrue(out.get("success"), out)
        result = out.get("result") or {}
        self.assertEqual(result.get("oldIn"), 20)
        self.assertEqual(result.get("newIn"), 5)
        with zipfile.ZipFile(out["output_path"]) as z:
            seq_xml = z.read("SeqContainer/s1.xml").decode("utf-8")
        self.assertIn("<In>5|", seq_xml)
        self.assertIn("<Start>0</Start>", seq_xml)
        self.assertIn("<Duration>100</Duration>", seq_xml)

    def test_op_chain_stops_and_reports_the_failing_step(self):
        ops = [
            {"op": "place_transition", "args": {"track": 1, "atFrame": 100, "durationFrames": 24}},
            {"op": "bogus_op", "args": {}},
        ]
        out = ab.run_drp_op_chain(ops, self.src, scratch_dir=os.path.join(self.tmp, "fail"))
        self.assertFalse(out.get("success"))
        self.assertEqual(out.get("failed_step"), 1)
        self.assertTrue(out["steps"][0]["success"])
        self.assertFalse(out["steps"][1]["success"])


class OpChainUnitTest(unittest.TestCase):
    """run_drp_op_chain refusals/no-op — no Node subprocess needed."""

    def test_empty_ops_is_a_noop_returning_the_source(self):
        with tempfile.NamedTemporaryFile(suffix=".drt", delete=False) as f:
            src = f.name
        try:
            out = ab.run_drp_op_chain([], src)
            self.assertTrue(out["success"])
            self.assertEqual(out["output_path"], os.path.abspath(src))
            self.assertEqual(out["steps"], [])
        finally:
            os.unlink(src)

    def test_missing_source_refused(self):
        if not ab.node_available():
            self.skipTest("node not on PATH")
        out = ab.run_drp_op_chain(
            [{"op": "split_clip", "args": {}}], "/nonexistent/ghost.drt")
        self.assertFalse(out["success"])
        self.assertIn("not found", out["error"])

    def test_no_node_refuses_cleanly(self):
        orig = ab.node_path
        ab.node_path = lambda: None
        try:
            out = ab.run_drp_op_chain([{"op": "split_clip", "args": {}}], "/whatever.drt")
            self.assertFalse(out["success"])
            self.assertIn("Node.js not found", out["error"])
        finally:
            ab.node_path = orig


if __name__ == "__main__":
    unittest.main()
