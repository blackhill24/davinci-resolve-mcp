"""Color/grade capability gates, driven both ways (#119 tasks 4, 5).

`_grade_item_snapshot`, `_graph_snapshot` and `_grade_capabilities` all gate on
`_has_method`. Nothing exercised the *supported* side, because the only object ever
passed in a test was a `MagicMock`, whose `dir()` lists only the children the test
touched — so every unconfigured method read as absent, the tests asserted on the
degraded snapshot, and the real path never ran.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import src.server  # noqa: E402,F401  domain modules import back from it
import src.domains.color_grade.actions as color_grade  # noqa: E402
from tests.bridge_double import ResolveBridgeDouble, call_names, calls_of  # noqa: E402


def _double(methods, name="obj"):
    return ResolveBridgeDouble(methods=methods, name=name)


class GradeItemSnapshotGateTest(unittest.TestCase):
    def _item(self, extra=None):
        methods = {
            "GetCurrentVersion": "Version 1",
            "GetVersionNameList": [],
            "GetNodeGraph": None,
            "GetColorGroup": None,
        }
        methods.update(extra or {})
        return _double(methods, name="timelineItem")

    def test_identity_fields_are_read_when_the_methods_exist(self):
        item = self._item({"GetName": "A001_C003", "GetUniqueId": "uid-1"})
        snapshot = color_grade._grade_item_snapshot(item)

        self.assertEqual("A001_C003", snapshot["name"])
        self.assertEqual("uid-1", snapshot["id"])
        self.assertIn("GetName", call_names(item))
        self.assertIn("GetUniqueId", call_names(item))

    def test_identity_fields_stay_none_and_uncalled_when_absent(self):
        item = self._item()
        snapshot = color_grade._grade_item_snapshot(item)

        self.assertIsNone(snapshot["name"])
        self.assertIsNone(snapshot["id"])
        self.assertNotIn("GetName", call_names(item))
        self.assertNotIn("GetUniqueId", call_names(item))

    def test_cache_flags_are_read_only_for_the_methods_that_exist(self):
        item = self._item({"GetIsColorOutputCacheEnabled": True})
        snapshot = color_grade._grade_item_snapshot(item)

        self.assertEqual({"color_output": True}, snapshot["cache"])
        self.assertNotIn("GetIsFusionOutputCacheEnabled", call_names(item))

    def test_both_cache_flags_are_read_when_both_exist(self):
        item = self._item({"GetIsColorOutputCacheEnabled": True,
                           "GetIsFusionOutputCacheEnabled": False})
        snapshot = color_grade._grade_item_snapshot(item)

        self.assertEqual({"color_output": True, "fusion_output": False},
                         snapshot["cache"])

    def test_a_bare_item_yields_an_empty_cache_map(self):
        self.assertEqual({}, color_grade._grade_item_snapshot(self._item())["cache"])


class GraphSnapshotGateTest(unittest.TestCase):
    """Per-node reads are gated one method at a time."""

    def _graph(self, extra=None, num_nodes=2):
        methods = {"GetNumNodes": num_nodes}
        methods.update(extra or {})
        return _double(methods, name="graph")

    def test_only_the_available_node_methods_are_read(self):
        graph = self._graph({"GetNodeLabel": "Primary", "GetLUT": "rec709.cube"})
        snapshot = color_grade._graph_snapshot(graph, max_nodes=1)

        node = snapshot["nodes"][0]
        self.assertEqual("Primary", node["label"])
        self.assertEqual("rec709.cube", node["lut"])
        self.assertNotIn("cache_mode", node)
        self.assertNotIn("tools", node)
        self.assertNotIn("GetNodeCacheMode", call_names(graph))

    def test_a_graph_exposing_everything_reports_every_field(self):
        graph = self._graph({"GetLUT": "rec709.cube", "GetNodeCacheMode": 1,
                             "GetNodeLabel": "Primary", "GetToolsInNode": ["Serial"]})
        node = color_grade._graph_snapshot(graph, max_nodes=1)["nodes"][0]

        for field in ("lut", "cache_mode", "label", "tools"):
            self.assertIn(field, node)

    def test_a_graph_exposing_nothing_yields_a_bare_node_row(self):
        graph = self._graph()
        node = color_grade._graph_snapshot(graph, max_nodes=1)["nodes"][0]

        self.assertEqual({"node_index": 1}, node)

    def test_node_methods_receive_the_node_index(self):
        graph = self._graph({"GetNodeLabel": "Primary"})
        color_grade._graph_snapshot(graph, max_nodes=1)

        self.assertEqual(("GetNodeLabel", (1,), {}),
                         [c for c in calls_of(graph) if c[0] == "GetNodeLabel"][0])


class GradeCapabilitiesGateTest(unittest.TestCase):
    def test_gallery_and_group_gates_are_independent(self):
        item = _double({}, name="timelineItem")
        proj = _double({"GetGallery": _double({}, name="gallery")}, name="project")

        report = color_grade._grade_capabilities(item, proj)

        self.assertTrue(report["gallery_available"])
        self.assertEqual(0, report["color_group_count"])
        self.assertNotIn("GetColorGroupsList", call_names(proj))

    def test_group_count_is_read_when_the_method_exists(self):
        item = _double({}, name="timelineItem")
        proj = _double({"GetColorGroupsList": [object(), object()]}, name="project")

        report = color_grade._grade_capabilities(item, proj)

        self.assertFalse(report["gallery_available"])
        self.assertEqual(2, report["color_group_count"])

    def test_a_bare_project_reports_neither(self):
        report = color_grade._grade_capabilities(_double({}), _double({}, name="project"))
        self.assertFalse(report["gallery_available"])
        self.assertEqual(0, report["color_group_count"])

    def test_a_magicmock_project_takes_the_closed_branch_on_every_gate(self):
        report = color_grade._grade_capabilities(mock.MagicMock(), mock.MagicMock())
        self.assertFalse(report["gallery_available"])
        self.assertEqual(0, report["color_group_count"])


if __name__ == "__main__":
    unittest.main()
