"""Render / quick-export capability gates, driven both ways (#119 tasks 4, 5).

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
import src.domains.render_deliver.actions as render_deliver  # noqa: E402
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


class QuickExportCapabilitiesTest(unittest.TestCase):
    """src/domains/render_deliver — the gate decides whether presets are fetched."""

    def test_presets_are_fetched_when_the_method_exists(self):
        proj = _double({"GetQuickExportRenderPresets": ["H.265 Master", "ProRes"]},
                       name="project")
        report = render_deliver._quick_export_capabilities(proj)

        self.assertEqual(["H.265 Master", "ProRes"], report["presets"])
        self.assertEqual(2, report["preset_count"])

    def test_no_method_means_no_presets_and_no_fabricated_call(self):
        proj = _double({"GetRenderPresetList": []}, name="project")
        report = render_deliver._quick_export_capabilities(proj)

        self.assertEqual([], report["presets"])
        self.assertEqual(0, report["preset_count"])

    def test_a_magicmock_project_would_have_reported_no_presets(self):
        """Why this was untested before: the mock always takes the closed branch."""
        report = render_deliver._quick_export_capabilities(mock.MagicMock())
        self.assertEqual([], report["presets"])


if __name__ == "__main__":
    unittest.main()
