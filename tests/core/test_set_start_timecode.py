"""`_set_start_timecode` verifies the start timecode by read-back (#113 Tier 2).

Callers compute ABSOLUTE record frames from the requested start and then hand
them to `AppendToTimeline`. If the timecode silently fails to take, every clip
lands at the wrong offset — a conform/delivery tool mis-timing its entire output
while reporting success. All three call sites wrapped the call in
`try: ... except: pass` and discarded the return.

Same class as #111 finding 5 (`ensure_timeline` discarding the
`timelineFrameRate` set), and fixed the same way: honour the result. Verification
is by read-back per `src/core/readback.py`, with NORMALIZED comparison so a
drop-frame timeline reporting `01:00:00;00` for a requested `01:00:00:00` is not
a false failure — that would break working multicam flows, which is the risk this
whole approach is designed to avoid.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.core.timeline_lookup import _normalize_timecode, _set_start_timecode  # noqa: E402


class TimelineStub:
    def __init__(self, *, current="00:00:00:00", reported=True, applies=True,
                 set_raises=False, get_raises=False, normalizes_to=None):
        self.current = current
        self.reported = reported
        self.applies = applies
        self.set_raises = set_raises
        self.get_raises = get_raises
        self.normalizes_to = normalizes_to
        self.set_calls = []

    def SetStartTimecode(self, tc):
        self.set_calls.append(tc)
        if self.set_raises:
            raise RuntimeError("SetStartTimecode exploded")
        if self.applies:
            self.current = self.normalizes_to or tc
        return self.reported

    def GetStartTimecode(self):
        if self.get_raises:
            raise RuntimeError("GetStartTimecode unavailable")
        return self.current


class NormalizeTimecodeTest(unittest.TestCase):
    def test_pads_and_normalizes_separators(self):
        for raw, want in [
            ("01:00:00:00", "01:00:00:00"),
            ("1:00:00:00", "01:00:00:00"),
            ("01:00:00;00", "01:00:00:00"),   # drop-frame separator
            ("01:00:00.00", "01:00:00:00"),
            ("1:2:3:4", "01:02:03:04"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(want, _normalize_timecode(raw))

    def test_non_timecode_values_are_unverifiable(self):
        for raw in [None, "", "   ", "not a timecode", "01:00:00", "01:00:00:00:00", "aa:bb:cc:dd"]:
            with self.subTest(raw=raw):
                self.assertIsNone(_normalize_timecode(raw))


class SetStartTimecodeTest(unittest.TestCase):
    def test_timecode_that_takes_effect_reports_true(self):
        tl = TimelineStub()
        self.assertTrue(_set_start_timecode(tl, "01:00:00:00"))
        self.assertEqual(["01:00:00:00"], tl.set_calls)

    def test_silently_ignored_timecode_reports_false(self):
        """The finding: Resolve says True, the start never moved."""
        tl = TimelineStub(current="00:00:00:00", reported=True, applies=False)
        self.assertFalse(
            _set_start_timecode(tl, "01:00:00:00"),
            "a reported-True set that did not take effect must not read as success",
        )

    def test_falsy_return_is_overridden_by_a_successful_readback(self):
        tl = TimelineStub(reported=False, applies=True)
        self.assertTrue(_set_start_timecode(tl, "01:00:00:00"))

    def test_drop_frame_readback_is_not_a_false_failure(self):
        """Requested non-drop, timeline reports drop-frame — same timecode."""
        tl = TimelineStub(normalizes_to="01:00:00;00")
        self.assertTrue(
            _set_start_timecode(tl, "01:00:00:00"),
            "a drop-frame separator must not read as a different timecode",
        )

    def test_unpadded_readback_is_not_a_false_failure(self):
        tl = TimelineStub(normalizes_to="1:00:00:00")
        self.assertTrue(_set_start_timecode(tl, "01:00:00:00"))

    def test_different_timecode_reports_false(self):
        tl = TimelineStub(normalizes_to="02:00:00:00")
        self.assertFalse(_set_start_timecode(tl, "01:00:00:00"))

    def test_raising_setter_still_verified_by_readback(self):
        tl = TimelineStub(current="01:00:00:00", set_raises=True)
        self.assertTrue(_set_start_timecode(tl, "01:00:00:00"))

    def test_raising_setter_with_wrong_state_reports_false(self):
        tl = TimelineStub(current="00:00:00:00", set_raises=True)
        self.assertFalse(_set_start_timecode(tl, "01:00:00:00"))

    def test_unreadable_start_falls_back_to_the_reported_value(self):
        self.assertTrue(
            _set_start_timecode(TimelineStub(reported=True, get_raises=True), "01:00:00:00"))
        self.assertFalse(
            _set_start_timecode(TimelineStub(reported=False, get_raises=True), "01:00:00:00"))

    def test_unparseable_readback_falls_back_to_the_reported_value(self):
        self.assertTrue(
            _set_start_timecode(TimelineStub(reported=True, normalizes_to="??"), "01:00:00:00"))
        self.assertFalse(
            _set_start_timecode(TimelineStub(reported=False, normalizes_to="??"), "01:00:00:00"))

    def test_unparseable_request_falls_back_to_the_reported_value(self):
        self.assertTrue(_set_start_timecode(TimelineStub(reported=True), "garbage"))
        self.assertFalse(_set_start_timecode(TimelineStub(reported=False), "garbage"))

    def test_none_timeline_is_false_and_never_raises(self):
        self.assertFalse(_set_start_timecode(None, "01:00:00:00"))


if __name__ == "__main__":
    unittest.main()
