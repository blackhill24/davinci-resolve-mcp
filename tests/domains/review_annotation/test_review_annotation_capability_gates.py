"""Annotation capability gates, driven both ways (#119 tasks 4, 5).

`_annotation_snapshot` has six include-if-present gates. This gate shape has no
error envelope — the field is simply omitted — so a gate stuck open does something
worse than returning a wrong flag: it calls an attribute the bridge fabricated and
records the resulting `None` as if it were real annotation data.

Every object here is a faithful `ResolveBridgeDouble`. A `MagicMock` reports as absent every
method a test did not explicitly configure, which is why nothing exercised the
populated path before.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import src.server  # noqa: E402,F401  domain modules import back from it
import src.domains.review_annotation.actions as review_annotation  # noqa: E402
from tests.bridge_double import ResolveBridgeDouble, call_names  # noqa: E402


def _double(methods, name="obj"):
    return ResolveBridgeDouble(methods=methods, name=name)


class AnnotationSnapshotGateTest(unittest.TestCase):
    """src/domains/review_annotation — six include-if-present gates.

    Different gate shape: no error envelope, the field is simply omitted. A gate
    stuck open would call a fabricated attribute and record `None` as if it were
    real data, which is worse than omitting it.
    """

    _ALL = {
        "GetMarkers": {1001: {"color": "Blue", "name": "note"}},
        "GetFlagList": ["Blue", "Red"],
        "GetClipColor": "Orange",
        "GetUniqueId": "uid-1",
        "GetName": "A001_C003",
    }

    def test_a_full_featured_target_populates_every_field(self):
        target = _double(dict(self._ALL), name="timelineItem")
        snapshot = review_annotation._annotation_snapshot("clip", target)

        self.assertEqual({"1001": {"color": "Blue", "name": "note"}},
                         {str(k): v for k, v in snapshot["markers"].items()})
        self.assertEqual(["Blue", "Red"], snapshot["flags"])
        self.assertEqual("Orange", snapshot["clip_color"])
        self.assertEqual("uid-1", snapshot["id"])
        self.assertEqual("A001_C003", snapshot["name"])
        self.assertEqual(sorted(self._ALL), sorted(call_names(target)))

    def test_each_absent_method_omits_exactly_its_own_field(self):
        field_of = {
            "GetMarkers": "markers",
            "GetFlagList": "flags",
            "GetClipColor": "clip_color",
            "GetUniqueId": "id",
            "GetName": "name",
        }
        for missing, field in field_of.items():
            with self.subTest(missing=missing):
                methods = {k: v for k, v in self._ALL.items() if k != missing}
                target = _double(methods, name="timelineItem")
                snapshot = review_annotation._annotation_snapshot("clip", target)

                self.assertNotIn(missing, call_names(target),
                                 f"called {missing} on an object that lacks it — "
                                 f"the bridge fabricates it and returns None")
                if field in ("markers", "flags", "clip_color"):
                    # These keys are pre-seeded, so absence shows as the default.
                    self.assertIn(snapshot[field], ({}, None, []))
                else:
                    self.assertNotIn(field, snapshot)

    def test_a_bare_target_yields_the_empty_snapshot_without_calling_anything(self):
        target = _double({}, name="timelineItem")
        snapshot = review_annotation._annotation_snapshot("clip", target)

        self.assertEqual([], call_names(target))
        self.assertEqual({}, snapshot["markers"])
        self.assertIsNone(snapshot["flags"])
        self.assertIsNone(snapshot["clip_color"])


class MagicMockWouldHaveHiddenThisTest(unittest.TestCase):
    """A MagicMock target yields the empty snapshot — every field untested."""

    def test_a_magicmock_annotation_target_produces_an_empty_snapshot(self):
        snapshot = review_annotation._annotation_snapshot("clip", mock.MagicMock())
        self.assertEqual({}, snapshot["markers"])
        self.assertIsNone(snapshot["flags"])
        self.assertNotIn("name", snapshot)


if __name__ == "__main__":
    unittest.main()
