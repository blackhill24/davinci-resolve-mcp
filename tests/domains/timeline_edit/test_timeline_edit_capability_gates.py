"""Timeline-edit capability gates, driven both ways (#119 tasks 4, 5).

`_append_clip_info_from_timeline_item` prefers `GetDuration()` over `GetEnd() -
GetStart()` for a documented reason: Resolve's timeline end position can be
*inclusive* for clips created via positioned append, so the arithmetic fallback is
off by one in exactly the case the preference exists to handle.

That makes the gate invisible to an unfaithful double. A `MagicMock` reports
`GetDuration` as absent, so every existing test took the arithmetic path and
asserted on its answer — the preferred path, and the off-by-one it prevents, were
never executed.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import src.server  # noqa: E402,F401  domain modules import back from it
import src.domains.timeline_edit.actions as timeline_edit  # noqa: E402
from tests.bridge_double import ResolveBridgeDouble, call_names  # noqa: E402


def _double(methods, name="obj"):
    return ResolveBridgeDouble(methods=methods, name=name)


class AppendClipInfoDurationGateTest(unittest.TestCase):
    """The one gate that decides which of two different numbers is used."""

    def _item(self, extra=None, start=100, end=200, left_offset=0):
        methods = {
            "GetMediaPoolItem": _double({"GetName": "A001_C003"}, name="mediaPoolItem"),
            "GetStart": start,
            "GetEnd": end,
            "GetLeftOffset": left_offset,
        }
        methods.update(extra or {})
        return _double(methods, name="timelineItem")

    def test_the_api_duration_is_preferred_over_end_minus_start(self):
        # GetDuration deliberately disagrees with end-start: 101 vs 100. Only the
        # preferred path can produce 101.
        item = self._item({"GetDuration": 101})
        info, err = timeline_edit._append_clip_info_from_timeline_item(item, 1)

        self.assertIsNone(err)
        self.assertIn("GetDuration", call_names(item))
        self.assertEqual(101, info["endFrame"] - info["startFrame"])

    def test_arithmetic_is_used_only_when_getduration_is_absent(self):
        item = self._item()
        info, err = timeline_edit._append_clip_info_from_timeline_item(item, 1)

        self.assertIsNone(err)
        self.assertNotIn("GetDuration", call_names(item))
        self.assertEqual(100, info["endFrame"] - info["startFrame"])

    def test_a_none_duration_falls_back_rather_than_erroring(self):
        item = self._item({"GetDuration": None})
        info, err = timeline_edit._append_clip_info_from_timeline_item(item, 1)

        self.assertIsNone(err)
        self.assertEqual(100, info["endFrame"] - info["startFrame"])

    def test_a_nonpositive_duration_is_rejected(self):
        item = self._item({"GetDuration": 0}, start=100, end=100)
        info, err = timeline_edit._append_clip_info_from_timeline_item(item, 1)

        self.assertIsNone(info)
        self.assertIn("duration", str(err["error"]).lower())

    def test_a_magicmock_silently_takes_the_fallback_and_still_looks_fine(self):
        """Why this gate had no coverage before — and the precise mechanism.

        `_has_method` tests `dir()`. A MagicMock's `dir()` lists only the children
        that have been *touched*, so a method the test never configured reads as
        absent, the gate closes, and the fallback produces a perfectly plausible
        number. The test passes without ever executing the preferred path, and
        nothing in its output hints that a branch was skipped.
        """
        item = mock.MagicMock()
        item.GetStart.return_value = 100
        item.GetEnd.return_value = 200
        item.GetLeftOffset.return_value = 0
        # GetDuration deliberately NOT configured — the ordinary way a test is written.

        info, err = timeline_edit._append_clip_info_from_timeline_item(item, 1)

        self.assertIsNone(err)
        self.assertEqual(100, info["endFrame"] - info["startFrame"])
        item.GetDuration.assert_not_called()


class AppendClipInfoSourceTrimGateTest(unittest.TestCase):
    def _item(self, extra=None):
        methods = {
            "GetMediaPoolItem": _double({"GetName": "A001_C003"}, name="mediaPoolItem"),
            "GetStart": 100,
            "GetEnd": 200,
        }
        methods.update(extra or {})
        return _double(methods, name="timelineItem")

    def test_source_start_prefers_getsourcestartframe(self):
        item = self._item({"GetSourceStartFrame": 5000, "GetLeftOffset": 7})
        info, err = timeline_edit._append_clip_info_from_timeline_item(item, 1)

        self.assertIsNone(err)
        self.assertEqual(5000, info["startFrame"])

    def test_source_start_falls_back_to_left_offset(self):
        item = self._item({"GetLeftOffset": 7})
        info, err = timeline_edit._append_clip_info_from_timeline_item(item, 1)

        self.assertIsNone(err)
        self.assertEqual(7, info["startFrame"])

    def test_an_item_with_no_pool_media_is_refused(self):
        item = _double({"GetMediaPoolItem": None}, name="timelineItem")
        info, err = timeline_edit._append_clip_info_from_timeline_item(item, 1)

        self.assertIsNone(info)
        self.assertIn("MediaPoolItem", str(err["error"]))


if __name__ == "__main__":
    unittest.main()
