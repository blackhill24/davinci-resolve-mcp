"""Timeline-lookup capability gates, driven both ways (#119 tasks 4, 5).

These gates pick between a *preferred* API and a *fallback*: `GetSourceStartFrame`
falls back to `GetLeftOffset`, `GetDuration` falls back to `end - start`. That makes
them the most dangerous shape for an unfaithful double, because both branches return
a plausible number — a test using a `MagicMock` takes the fallback every time,
asserts on the number it produced, and passes without ever proving the preferred API
is used when it exists.

Every object here is a `ResolveBridgeDouble`, and each gate is driven with the
method present *and* absent, with the two answers made deliberately different so the
branch actually taken is observable.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.core.timeline_lookup import (  # noqa: E402
    _project_name_and_id,
    _timeline_item_duration,
    _timeline_item_source_start,
)
from tests.bridge_double import ResolveBridgeDouble, call_names  # noqa: E402


def _double(methods, name="obj"):
    return ResolveBridgeDouble(methods=methods, name=name)


class SourceStartGateTest(unittest.TestCase):
    """GetSourceStartFrame preferred, GetLeftOffset as the fallback."""

    def test_the_preferred_method_wins_when_present(self):
        item = _double({"GetSourceStartFrame": 1000, "GetLeftOffset": 7},
                       name="timelineItem")
        self.assertEqual(1000, _timeline_item_source_start(item))
        self.assertEqual(["GetSourceStartFrame"], call_names(item))

    def test_the_fallback_is_used_only_when_the_method_is_absent(self):
        item = _double({"GetLeftOffset": 7}, name="timelineItem")
        self.assertEqual(7, _timeline_item_source_start(item))
        self.assertEqual(["GetLeftOffset"], call_names(item))

    def test_a_preferred_method_returning_none_still_falls_back(self):
        item = _double({"GetSourceStartFrame": None, "GetLeftOffset": 7},
                       name="timelineItem")
        self.assertEqual(7, _timeline_item_source_start(item))
        self.assertEqual(["GetSourceStartFrame", "GetLeftOffset"], call_names(item))

    def test_a_zero_source_start_is_not_mistaken_for_missing(self):
        item = _double({"GetSourceStartFrame": 0, "GetLeftOffset": 7},
                       name="timelineItem")
        self.assertEqual(0, _timeline_item_source_start(item))

    def test_a_magicmock_would_have_silently_taken_the_fallback(self):
        item = mock.MagicMock()
        item.GetLeftOffset.return_value = 7
        self.assertEqual(7, _timeline_item_source_start(item))
        item.GetSourceStartFrame.assert_not_called()


class DurationGateTest(unittest.TestCase):
    """GetDuration preferred, `end - start` as the fallback."""

    def test_the_api_duration_wins_over_the_arithmetic_fallback(self):
        item = _double({"GetDuration": 240}, name="timelineItem")
        self.assertEqual(240, _timeline_item_duration(item, start=0, end=100))
        self.assertEqual(["GetDuration"], call_names(item))

    def test_arithmetic_is_used_only_when_the_method_is_absent(self):
        item = _double({}, name="timelineItem")
        self.assertEqual(100, _timeline_item_duration(item, start=0, end=100))
        self.assertEqual([], call_names(item))

    def test_no_method_and_no_bounds_yields_none(self):
        self.assertIsNone(_timeline_item_duration(_double({}, name="timelineItem")))

    def test_a_none_duration_falls_back_to_arithmetic(self):
        item = _double({"GetDuration": None}, name="timelineItem")
        self.assertEqual(100, _timeline_item_duration(item, start=0, end=100))


class ProjectIdentityGateTest(unittest.TestCase):
    def test_both_fields_are_read_when_both_methods_exist(self):
        project = _double({"GetName": "Ep101", "GetUniqueId": "uid-1"}, name="project")
        self.assertEqual(("Ep101", "uid-1"), _project_name_and_id(project))

    def test_each_field_is_independently_gated(self):
        self.assertEqual(("Ep101", None),
                         _project_name_and_id(_double({"GetName": "Ep101"})))
        self.assertEqual((None, "uid-1"),
                         _project_name_and_id(_double({"GetUniqueId": "uid-1"})))

    def test_a_bare_project_yields_nothing_and_calls_nothing(self):
        project = _double({}, name="project")
        self.assertEqual((None, None), _project_name_and_id(project))
        self.assertEqual([], call_names(project))

    def test_no_project_at_all_is_safe(self):
        self.assertEqual((None, None), _project_name_and_id(None))


if __name__ == "__main__":
    unittest.main()
