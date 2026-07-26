"""Project / database / preset capability gates, driven both ways (#119 tasks 4, 5).

Part of the #119 tasks 4/5 migration: every Resolve object here is a faithful
`tests.bridge_double.ResolveBridgeDouble`, not a `MagicMock`. `_has_method` tests
`dir()`, and a MagicMock's `dir()` lists only the children a test has touched — so
every method the test did not explicitly configure reads as absent, the gate closes,
and the test asserts on the degraded result while the supported path never runs. Each gate below is driven in BOTH
directions so a gate stuck open or stuck shut fails here.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import src.server  # noqa: E402,F401  domain modules import back from it
import src.domains.project_lifecycle.actions as project_lifecycle  # noqa: E402
from tests.bridge_double import ResolveBridgeDouble  # noqa: E402


def _double(methods, name="obj"):
    return ResolveBridgeDouble(methods=methods, name=name)


def _flatten(mapping, prefix=""):
    """Every leaf bool in a nested capability map, keyed by dotted path."""
    out = {}
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, prefix=f"{path}."))
        elif isinstance(value, bool):
            out[path] = value
    return out


class ProjectCapabilitiesTest(unittest.TestCase):
    """src/domains/project_lifecycle — the largest block of gates in the repo."""

    _LAYOUT = ("SaveLayoutPreset", "LoadLayoutPreset", "UpdateLayoutPreset",
               "ExportLayoutPreset", "ImportLayoutPreset", "DeleteLayoutPreset")
    _RENDER_FILES = ("ImportRenderPreset", "ExportRenderPreset",
                     "ImportBurnInPreset", "ExportBurnInPreset")

    def test_layout_preset_map_tracks_exactly_what_the_object_exposes(self):
        present = ("SaveLayoutPreset", "LoadLayoutPreset", "ExportLayoutPreset")
        resolve_obj = _double({m: True for m in present}, name="resolve")

        report = project_lifecycle._project_capabilities(
            pm=_double({}), project=_double({}), resolve_obj=resolve_obj)
        layout = report["resolve"]["layout_presets"]

        self.assertEqual(
            {"save": True, "load": True, "update": False,
             "export": True, "import": False, "delete": False},
            layout)

    def test_render_preset_file_map_tracks_exactly_what_the_object_exposes(self):
        resolve_obj = _double({"ImportRenderPreset": True, "ExportBurnInPreset": True},
                              name="resolve")
        report = project_lifecycle._project_capabilities(
            pm=_double({}), project=_double({}), resolve_obj=resolve_obj)

        self.assertEqual(
            {"import_render": True, "export_render": False,
             "import_burnin": False, "export_burnin": True},
            report["resolve"]["render_presets"])

    def test_a_fully_featured_resolve_reports_every_preset_capability(self):
        resolve_obj = _double({m: True for m in self._LAYOUT + self._RENDER_FILES},
                              name="resolve")
        report = project_lifecycle._project_capabilities(
            pm=_double({}), project=_double({}), resolve_obj=resolve_obj)

        self.assertTrue(all(_flatten(report["resolve"]).values()))

    def test_a_bare_resolve_reports_none_of_them(self):
        report = project_lifecycle._project_capabilities(
            pm=_double({}), project=_double({}), resolve_obj=_double({}, name="resolve"))

        self.assertFalse(any(_flatten(report["resolve"]).values()))

    def test_project_and_project_manager_method_maps_track_the_object(self):
        pm_methods = list(project_lifecycle._PROJECT_MANAGER_METHODS)
        project_methods = list(project_lifecycle._PROJECT_METHODS)
        self.assertGreater(len(pm_methods), 1)
        self.assertGreater(len(project_methods), 1)

        pm = _double({pm_methods[0]: None}, name="projectManager")
        project = _double({project_methods[0]: None}, name="project")
        report = project_lifecycle._project_capabilities(
            pm=pm, project=project, resolve_obj=_double({}))

        self.assertEqual(
            {m: (m == pm_methods[0]) for m in pm_methods},
            report["project_manager_methods"])
        self.assertEqual(
            {m: (m == project_methods[0]) for m in project_methods},
            report["project_methods"])

    def test_absent_objects_report_optimistically_and_that_is_deliberate(self):
        """`if pm else True` — no object means 'unknown', not 'unavailable'."""
        report = project_lifecycle._project_capabilities(
            pm=None, project=None, resolve_obj=None)
        self.assertTrue(all(report["project_manager_methods"].values()))
        self.assertTrue(all(report["project_methods"].values()))


class DatabaseCapabilitiesTest(unittest.TestCase):
    def test_map_and_payload_both_follow_the_object(self):
        pm = _double({"GetCurrentDatabase": {"DbType": "Disk", "DbName": "Local"},
                      "GetDatabaseList": [{"DbName": "Local"}]},
                     name="projectManager")
        report = project_lifecycle._database_capabilities(pm)

        self.assertEqual({"get_current": True, "list": True, "set_current": False},
                         report["methods"])
        self.assertEqual({"DbType": "Disk", "DbName": "Local"}, report["current"])
        self.assertEqual([{"DbName": "Local"}], report["databases"])

    def test_a_bare_project_manager_reports_nothing_and_fetches_nothing(self):
        report = project_lifecycle._database_capabilities(_double({}, name="pm"))

        self.assertEqual({"get_current": False, "list": False, "set_current": False},
                         report["methods"])
        self.assertNotIn("current", report)
        self.assertNotIn("databases", report)

    def test_set_current_is_reported_independently_of_the_read_methods(self):
        pm = _double({"SetCurrentDatabase": True}, name="pm")
        self.assertEqual({"get_current": False, "list": False, "set_current": True},
                         project_lifecycle._database_capabilities(pm)["methods"])


class PresetLifecycleProbeTest(unittest.TestCase):
    def test_availability_flags_track_both_objects_independently(self):
        project = _double({"GetPresetList": ["Default"],
                           "GetRenderPresetList": ["H.264"]}, name="project")
        resolve_obj = _double({"GetFairlightPresets": ["Voice"],
                               "LoadLayoutPreset": True}, name="resolve")

        report = project_lifecycle._preset_lifecycle_probe(resolve_obj, project, {})

        self.assertTrue(report["project_presets"]["available"])
        self.assertTrue(report["render_presets"]["available"])
        self.assertFalse(report["quick_export_presets"]["available"])
        self.assertTrue(report["fairlight_presets"]["available"])
        self.assertEqual(
            {"save": False, "load": True, "update": False,
             "export": False, "import": False, "delete": False},
            report["layout_presets"])
        self.assertFalse(any(report["render_preset_files"].values()))

    def test_everything_absent_reports_everything_unavailable(self):
        report = project_lifecycle._preset_lifecycle_probe(
            _double({}, name="resolve"), _double({}, name="project"), {})

        flags = _flatten({k: v for k, v in report.items()
                          if k in ("layout_presets", "render_preset_files")})
        self.assertFalse(any(flags.values()))
        self.assertFalse(report["project_presets"]["available"])
        self.assertFalse(report["fairlight_presets"]["available"])


if __name__ == "__main__":
    unittest.main()
