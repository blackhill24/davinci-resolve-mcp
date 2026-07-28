"""#142 finding 4: range selectors must refuse bad input, not crash on it.

`_range_track_indices` was a bare comprehension over `int(...)`, so
`track_indices="V1"` raised ValueError and `track_indices=2.0` raised
"TypeError: 'float' object is not iterable" — as raw tracebacks, since it sits
on the path of every copy_range / duplicate_range / overwrite_range /
lift_range via `_collect_timeline_items_in_range` and nothing catches there.

Its neighbour `_range_frames_from_params` assumed `GetMarkInOut()` returns a
dict-of-dicts with no shape check.
"""

from __future__ import annotations

import unittest

from src.core.timeline_lookup import (
    _collect_timeline_items_in_range,
    _range_frames_from_params,
    _range_track_indices,
)
from tests.bridge_double import ResolveBridgeDouble


class RangeTrackIndicesTest(unittest.TestCase):
    def _indices(self, raw):
        return _range_track_indices({"track_indices": raw}, "video")

    def test_unspecified_means_all_tracks(self):
        self.assertEqual((None, None), _range_track_indices({}, "video"))

    def test_the_accepted_shapes_still_parse(self):
        self.assertEqual(([2], None), self._indices(2))
        self.assertEqual(([1, 2], None), self._indices("1,2"))
        self.assertEqual(([1, 2], None), self._indices([1, 2]))
        self.assertEqual(([1, 2], None), self._indices(("1", " 2 ")))
        # A whole-number float is unambiguous.
        self.assertEqual(([2], None), self._indices(2.0))

    def test_a_track_name_is_refused_with_a_usable_message(self):
        indices, err = self._indices("V1")
        self.assertIsNone(indices)
        self.assertEqual("INVALID_TRACK_INDICES", err["error"]["code"])
        self.assertEqual("invalid_input", err["error"]["category"])
        self.assertIn("V1", err["error"]["message"])

    def test_a_zero_or_negative_index_is_refused(self):
        for bad in (0, -1, "0"):
            with self.subTest(bad=bad):
                indices, err = self._indices(bad)
                self.assertIsNone(indices)
                self.assertIn("start at 1", err["error"]["message"])

    def test_a_nonsense_type_is_refused_rather_than_iterated(self):
        for bad in ({"a": 1}, True, object()):
            with self.subTest(bad=bad):
                indices, err = self._indices(bad)
                self.assertIsNone(indices)
                self.assertEqual("INVALID_TRACK_INDICES", err["error"]["code"])

    def test_a_bad_selector_stops_the_range_collection(self):
        timeline = ResolveBridgeDouble(methods={
            "GetMarkInOut": {},
            "GetTrackCount": 2,
            "GetItemListInTrack": [],
        })
        start, end, items, err = _collect_timeline_items_in_range(
            timeline, {"start_frame": 0, "end_frame": 100, "track_indices": "V1"})
        self.assertIsNone(items)
        self.assertIsNone(start)
        self.assertIsNone(end)
        self.assertEqual("INVALID_TRACK_INDICES", err["error"]["code"])


class RangeFramesFromParamsTest(unittest.TestCase):
    def test_a_well_formed_mark_in_out_is_read(self):
        timeline = ResolveBridgeDouble(methods={
            "GetMarkInOut": {"video": {"in": 10, "out": 90}},
        })
        self.assertEqual((10, 90, None),
                         _range_frames_from_params(timeline, {"use_mark_in_out": True}))

    def test_a_non_dict_payload_is_an_envelope_not_an_attributeerror(self):
        timeline = ResolveBridgeDouble(methods={"GetMarkInOut": ["video", 1]})
        _start, _end, err = _range_frames_from_params(timeline, {"use_mark_in_out": True})
        self.assertIn("expected a dict", err["error"]["message"])

    def test_a_flat_inner_payload_is_an_envelope_not_an_attributeerror(self):
        timeline = ResolveBridgeDouble(methods={"GetMarkInOut": {"video": [10, 90]}})
        _start, _end, err = _range_frames_from_params(timeline, {"use_mark_in_out": True})
        self.assertIn("expected", err["error"]["message"])

    def test_missing_in_out_values_are_reported(self):
        timeline = ResolveBridgeDouble(methods={"GetMarkInOut": {"video": {"in": None}}})
        _start, _end, err = _range_frames_from_params(timeline, {"use_mark_in_out": True})
        self.assertIn("no numeric", err["error"]["message"])


if __name__ == "__main__":
    unittest.main()
