"""#113 Tier 3: the four sites that turned out NOT to be ignorable.

Tier 3 was expected to be a documentation pass — "decide these are fine and
record why". Triaging them found four that were not fine, each producing
plausible-looking but wrong output rather than a visible failure:

  * `_run_inline_lua` clears four Fusion sentinel slots before RunScript. The
    completion poll is `while fusion.GetData("__mcp_done__") != "1"`, so a clear
    that did not take leaves the PREVIOUS run's sentinel at "1": the poll exits
    immediately and the stdout/result slots read back are the previous
    invocation's output, returned as if this script had produced it.
  * `_timeline_thumbnail_contact_sheet` moves the playhead per sample, then grabs
    a thumbnail regardless and labels it with the REQUESTED timecode. A playhead
    that did not move produced a contact sheet whose frames are all from the
    wrong position but carry correct-looking timecodes.
  * `SetCurrentRenderMode` in `render_deliver._build_proxies` (0 = individual
    clips) and `timeline_edit._timeline_render_in_place_impl` (1 = single clip).
    Left in the wrong mode Resolve emits one stitched movie instead of one proxy
    per clip, or one file per clip instead of a continuous render-in-place.

The first two are verified by READ-BACK rather than by the return value, which is
why their bare calls still appear in ACCEPTED_DISCARDED_RETURNS — the read-back
beneath them is the real check. See that file's grouped rationale.
"""
from __future__ import annotations

import binascii
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import src.server  # noqa: E402,F401  (breaks the actions<->server import cycle)

from src.domains.extension_authoring import actions as ext_actions  # noqa: E402
from src.domains.timeline_edit import actions as timeline_actions  # noqa: E402
from tests._error_envelope_helpers import err_message, is_err  # noqa: E402


# ── _run_inline_lua: the stale completion sentinel ───────────────────────────


class FusionStub:
    """Fusion handle whose SetData may silently not take."""

    def __init__(self, *, clears_work=True, stale=None):
        self.data = dict(stale or {})
        self.clears_work = clears_work
        self.ran = []

    def SetData(self, key, value):
        if self.clears_work:
            self.data[key] = value
        return None          # Fusion's SetData has no dependable return

    def GetData(self, key):
        return self.data.get(key, "")

    def RunScript(self, path):
        self.ran.append(path)
        # A working run sets the sentinel and its output slots.
        self.data["__mcp_done__"] = "1"
        self.data["__mcp_stdout__"] = "fresh output"
        self.data["__mcp_result__"] = "fresh result"
        return True


class ResolveStub:
    def __init__(self, fusion):
        self._fusion = fusion

    def Fusion(self):
        return self._fusion


class RunInlineLuaSentinelTest(unittest.TestCase):
    def _run(self, fusion, source="return 1"):
        with mock.patch.object(ext_actions, "get_resolve", return_value=ResolveStub(fusion)):
            return ext_actions._run_inline_lua(source)

    def test_refuses_when_a_stale_sentinel_cannot_be_cleared(self):
        """The finding: the previous run's output returned as this run's."""
        fusion = FusionStub(
            clears_work=False,
            stale={"__mcp_done__": "1",
                   "__mcp_stdout__": "PREVIOUS output",
                   "__mcp_result__": "PREVIOUS result"},
        )

        result = self._run(fusion)

        self.assertTrue(is_err(result))
        self.assertIn("sentinel", err_message(result))
        self.assertEqual([], fusion.ran, "must not run the script at all")

    def test_runs_normally_when_the_clear_takes(self):
        fusion = FusionStub(
            clears_work=True,
            stale={"__mcp_done__": "1", "__mcp_stdout__": "PREVIOUS output"},
        )

        result = self._run(fusion)

        self.assertFalse(is_err(result), result)
        self.assertEqual(1, len(fusion.ran), "the script should have run once")

    def test_clean_slate_runs_normally(self):
        fusion = FusionStub(clears_work=True)
        result = self._run(fusion)
        self.assertFalse(is_err(result), result)
        self.assertEqual(1, len(fusion.ran))


# ── contact sheet: the stuck playhead ────────────────────────────────────────


class ContactSheetTimelineStub:
    """Timeline whose playhead may refuse to move."""

    def __init__(self, *, playhead_moves=True):
        self.playhead_moves = playhead_moves
        self.timecode = "01:00:00:00"
        self.thumbnails_grabbed = 0

    def GetName(self):
        return "TL"

    def GetUniqueId(self):
        return "tl-1"

    def GetStartFrame(self):
        return 0

    def GetSetting(self, key):
        return "24" if key == "timelineFrameRate" else ""

    def GetCurrentTimecode(self):
        return self.timecode

    def SetCurrentTimecode(self, tc):
        if self.playhead_moves:
            self.timecode = tc
        return True          # Resolve reports success either way — the finding

    def GetCurrentClipThumbnailImage(self):
        self.thumbnails_grabbed += 1
        return {"width": 2, "height": 1, "format": "RGB",
                "data": binascii.hexlify(b"\x00" * 6).decode()}


class ContactSheetProjectStub:
    def GetName(self):
        return "proj"

    def GetUniqueId(self):
        return "proj-1"


class ContactSheetPlayheadTest(unittest.TestCase):
    """A thumbnail grabbed at the wrong frame is worse than a missing one.

    Nothing in the output says it is wrong: the sample carries the REQUESTED
    timecode, so a contact sheet built from a stuck playhead looks correct while
    every frame is from the same wrong position.
    """

    def _run(self, tl, tmp):
        return timeline_actions._timeline_thumbnail_contact_sheet(
            ContactSheetProjectStub(), tl, {"frames": [24, 48], "analysis_root": tmp})

    def test_stuck_playhead_skips_the_sample_instead_of_capturing_it(self):
        tl = ContactSheetTimelineStub(playhead_moves=False)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tl, tmp)

        # Every sample was refused, so the action reports no thumbnails rather
        # than handing back a contact sheet of wrong-frame images.
        self.assertTrue(is_err(result), result)
        self.assertIn("No thumbnails", err_message(result))
        samples = result.get("samples") or []
        self.assertEqual(2, len(samples), result)
        for sample in samples:
            self.assertIn("playhead did not move", str(sample.get("error")))
            self.assertNotIn("thumbnail_rgb", sample)
        self.assertEqual(0, tl.thumbnails_grabbed,
                         "must not capture a thumbnail at the wrong frame")

    def test_moving_playhead_captures_normally(self):
        tl = ContactSheetTimelineStub(playhead_moves=True)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tl, tmp)

        self.assertFalse(is_err(result), result)
        samples = result.get("samples") or []
        self.assertEqual(2, len(samples), result)
        for sample in samples:
            self.assertIsNone(sample.get("error"))
            self.assertTrue(sample.get("thumbnail_available"))
        self.assertEqual(2, tl.thumbnails_grabbed)


# ── render mode ──────────────────────────────────────────────────────────────


class RenderModeGuardTest(unittest.TestCase):
    """The render-mode guards are one-line checks on a deep, gated path.

    Driving `_build_proxies` / `_timeline_render_in_place_impl` end-to-end needs a
    confirm-token + render-job harness that would mostly test the stubs. These
    assert on the source instead — a weaker check, and called out as such — so
    that the guard cannot be quietly dropped while the render mode stays wrong.
    """

    def _source(self, relpath):
        return (pathlib.Path(__file__).resolve().parents[1] / relpath).read_text(encoding="utf-8")

    def test_build_proxies_checks_the_individual_clips_mode(self):
        src = self._source("src/domains/render_deliver/actions.py")
        self.assertIn("if not proj.SetCurrentRenderMode(0):", src)
        self.assertIn("one stitched file instead of one", src)

    def test_render_in_place_checks_the_single_clip_mode(self):
        src = self._source("src/domains/timeline_edit/actions.py")
        self.assertIn("if not proj.SetCurrentRenderMode(1):", src)
        self.assertIn("single-clip render mode", src)


if __name__ == "__main__":
    unittest.main()
