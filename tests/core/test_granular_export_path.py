"""Export regressions pinned on the code path that actually broke (#119 task 7).

`tests/core/test_api_constant_resolution.py` (added by #118) tests
`src.core.envelope._api_constant` in isolation. The functions that regressed are
`src/granular/timeline.timeline_export` and `src/granular/timeline_item.ti_export_lut`,
and until #119 task 6 they bound a *second copy* of the helper — so that test could
not see a defect re-introduced in the copy they use.

These tests drive the two tool functions end to end against the faithful bridge
double and assert on what reaches `Timeline.Export()` / `TimelineItem.ExportLUT()`:
a **number**, never the literal constant name. That is the exact observable that
`cc007ef` got wrong and that eight live harnesses caught after the offline suite
stayed green.

Each test also carries its inverse — the defect shape — so a future edit that
resolves constants through `dir()` again fails here rather than in the live suite.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import src.granular.timeline as granular_timeline  # noqa: E402
import src.granular.timeline_item as granular_timeline_item  # noqa: E402
from tests.bridge_double import (  # noqa: E402
    RESOLVE_EXPORT_CONSTANTS,
    ResolveBridgeDouble,
    calls_of,
    make_resolve,
)


def _timeline_double(export_result=True):
    return ResolveBridgeDouble(methods={"Export": export_result}, name="timeline")


def _item_double(export_result=True):
    return ResolveBridgeDouble(methods={"ExportLUT": export_result}, name="timelineItem")


class TimelineExportConstantResolutionTest(unittest.TestCase):
    """src/granular/timeline.py — Timeline.Export() must receive numbers."""

    def _export(self, resolve, timeline, **kwargs):
        with mock.patch.object(granular_timeline, "resolve", resolve), \
             mock.patch.object(granular_timeline, "_get_timeline", autospec=True,
                               return_value=(None, timeline, None)):
            return granular_timeline.timeline_export(**kwargs)

    def test_drt_export_passes_the_numeric_constant_not_the_name(self):
        resolve, timeline = make_resolve(), _timeline_double()
        out = self._export(resolve, timeline,
                           file_path="/tmp/cut.drt", export_type="EXPORT_DRT")

        self.assertTrue(out["success"])
        (_name, args, _kw), = calls_of(timeline)
        path, etype, esub = args
        self.assertEqual("/tmp/cut.drt", path)
        self.assertEqual(RESOLVE_EXPORT_CONSTANTS["EXPORT_DRT"], etype)
        self.assertNotEqual("EXPORT_DRT", etype)   # the cc007ef symptom
        self.assertEqual(RESOLVE_EXPORT_CONSTANTS["EXPORT_NONE"], esub)

    def test_every_export_type_resolves_to_a_number(self):
        for name in sorted(RESOLVE_EXPORT_CONSTANTS):
            if name.startswith("EXPORT_LUT_") or name == "EXPORT_NONE":
                continue
            with self.subTest(export_type=name):
                resolve, timeline = make_resolve(), _timeline_double()
                self._export(resolve, timeline,
                             file_path=f"/tmp/out.{name.lower()}", export_type=name)
                _, args, _ = calls_of(timeline)[0]
                self.assertNotIsInstance(
                    args[1], str,
                    f"{name} reached Export() as a string — constants resolve by "
                    f"getattr, not dir()")

    def test_export_none_subtype_survives_being_zero(self):
        """EXPORT_NONE is 0.0; an `if value:` guard anywhere drops it back to a name."""
        resolve, timeline = make_resolve(), _timeline_double()
        self._export(resolve, timeline, file_path="/tmp/cut.aaf",
                     export_type="EXPORT_AAF", export_subtype="EXPORT_NONE")
        _, args, _ = calls_of(timeline)[0]
        self.assertEqual(0.0, args[2])
        self.assertNotEqual("EXPORT_NONE", args[2])

    def test_unknown_type_falls_back_to_the_literal_name(self):
        """Fabricated names must degrade, not crash — the API rejects them itself."""
        resolve, timeline = make_resolve(), _timeline_double(export_result=False)
        out = self._export(resolve, timeline, file_path="/tmp/x",
                           export_type="EXPORT_TOTALLY_FAKE")
        _, args, _ = calls_of(timeline)[0]
        self.assertEqual("EXPORT_TOTALLY_FAKE", args[1])
        self.assertFalse(out["success"])

    def test_a_dir_based_lookup_would_fail_this_test(self):
        """Pins the defect shape itself, in the module the export path binds."""
        resolve, timeline = make_resolve(), _timeline_double()
        self.assertNotIn("EXPORT_DRT", dir(resolve))   # dir() lists methods only

        with mock.patch.object(
                granular_timeline, "_api_constant", autospec=True,
                side_effect=lambda obj, n: (n in dir(obj)) and getattr(obj, n) or None):
            self._export(resolve, timeline,
                         file_path="/tmp/cut.drt", export_type="EXPORT_DRT")
        _, args, _ = calls_of(timeline)[0]
        self.assertEqual("EXPORT_DRT", args[1],
                         "the dir()-based lookup is expected to degrade to the name")

    def test_older_resolve_without_the_constants_still_exports(self):
        resolve = make_resolve(export_constants=False)
        timeline = _timeline_double(export_result=False)
        out = self._export(resolve, timeline, file_path="/tmp/cut.drt",
                           export_type="EXPORT_DRT")
        _, args, _ = calls_of(timeline)[0]
        self.assertEqual("EXPORT_DRT", args[1])
        self.assertIn("success", out)


class ExportLutConstantResolutionTest(unittest.TestCase):
    """src/granular/timeline_item.py — TimelineItem.ExportLUT() must receive numbers."""

    def _export_lut(self, resolve, item, **kwargs):
        with mock.patch.object(granular_timeline_item, "resolve", resolve), \
             mock.patch.object(granular_timeline_item, "_get_timeline_item", autospec=True,
                               return_value=(item, None)):
            return granular_timeline_item.ti_export_lut(**kwargs)

    def test_lut_type_passes_the_numeric_constant_not_the_name(self):
        resolve, item = make_resolve(), _item_double()
        out = self._export_lut(resolve, item,
                               export_type="EXPORT_LUT_33PTCUBE", path="/tmp/g.cube")

        self.assertTrue(out["success"])
        (_name, args, _kw), = calls_of(item)
        etype, path = args
        self.assertEqual(RESOLVE_EXPORT_CONSTANTS["EXPORT_LUT_33PTCUBE"], etype)
        self.assertNotEqual("EXPORT_LUT_33PTCUBE", etype)
        self.assertEqual("/tmp/g.cube", path)

    def test_every_lut_type_resolves_to_a_number(self):
        for name in sorted(n for n in RESOLVE_EXPORT_CONSTANTS
                           if n.startswith("EXPORT_LUT_")):
            with self.subTest(export_type=name):
                resolve, item = make_resolve(), _item_double()
                self._export_lut(resolve, item, export_type=name, path="/tmp/g.cube")
                _, args, _ = calls_of(item)[0]
                self.assertNotIsInstance(args[0], str)

    def test_unknown_lut_type_falls_back_to_the_literal_name(self):
        resolve, item = make_resolve(), _item_double(export_result=False)
        out = self._export_lut(resolve, item,
                               export_type="EXPORT_LUT_MADE_UP", path="/tmp/g.cube")
        _, args, _ = calls_of(item)[0]
        self.assertEqual("EXPORT_LUT_MADE_UP", args[0])
        self.assertFalse(out["success"])


class ExportPathBindsTheSharedHelperTest(unittest.TestCase):
    """The reason the #118 test could not see the regression (#119 §1)."""

    def test_both_export_modules_bind_the_canonical_api_constant(self):
        from src.core.envelope import _api_constant as canonical

        self.assertIs(granular_timeline._api_constant, canonical)
        self.assertIs(granular_timeline_item._api_constant, canonical)


if __name__ == "__main__":
    unittest.main()
