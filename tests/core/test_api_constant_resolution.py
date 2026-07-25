"""Resolve API CONSTANTS need getattr, not dir() — the opposite of methods.

Found by running the full live suite: 8 harnesses failed with
`drt export failed`, and `Timeline.Export()` was being handed the literal string
`"EXPORT_DRT"` instead of the numeric constant `1.0`.

Root cause, introduced in `cc007ef` (the #110 finding-11 fix). That finding was
correct about METHODS — the Python bridge fabricates an attribute for any name,
so `hasattr(obj, 'TotallyMadeUp')` is always True and capability detection has to
test `name in dir(obj)`. The fix then applied the same `dir()` test to CONSTANT
lookups, where it is wrong in the other direction: verified live on Resolve
Studio 21.0.2.4,

    dir(resolve)                          -> 25 entries, all methods, no EXPORT_*
    getattr(resolve, 'EXPORT_DRT')        -> 1.0
    getattr(resolve, 'TOTALLY_FAKE')      -> None
    hasattr(resolve, 'TOTALLY_FAKE')      -> True

so `'EXPORT_DRT' in dir(resolve)` is False, the code fell back to passing the
name, and `Timeline.Export(path, 'EXPORT_DRT', 'EXPORT_NONE')` returned False.
Every .drt / AAF / EDL / XML timeline export and every LUT export was broken.

The discriminator is the fabrication behaviour itself: a real constant is a
number, a fabricated one is None. That is what `_api_constant` uses.

`ResolveBridgeStub` below reproduces all four behaviours, so this is pinned
offline and cannot regress on a machine without Resolve.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.core.envelope import _api_constant, _has_method  # noqa: E402


class ResolveBridgeStub:
    """Mimics BlackmagicFusion.PyRemoteObject.

    Real methods and constants both answer via __getattr__; anything else is
    fabricated as None; dir() lists ONLY the methods.
    """

    _METHODS = ("GetProjectManager", "GetMediaStorage", "OpenPage")
    _CONSTANTS = {"EXPORT_DRT": 1.0, "EXPORT_AAF": 2.0, "EXPORT_NONE": 0.0,
                  "EXPORT_LUT_CUBE": 5.0}

    def __getattr__(self, name):
        if name in self._CONSTANTS:
            return self._CONSTANTS[name]
        if name in self._METHODS:
            return lambda *a, **k: None
        return None          # fabricated — the bridge never raises AttributeError

    def __dir__(self):
        return list(self._METHODS)


class BridgeStubFidelityTest(unittest.TestCase):
    """The stub must reproduce the real bridge, or the tests below prove nothing."""

    def setUp(self):
        self.r = ResolveBridgeStub()

    def test_dir_lists_methods_but_not_constants(self):
        self.assertIn("GetProjectManager", dir(self.r))
        self.assertNotIn("EXPORT_DRT", dir(self.r))

    def test_getattr_returns_constants_and_fabricates_everything_else(self):
        self.assertEqual(1.0, getattr(self.r, "EXPORT_DRT"))
        self.assertIsNone(getattr(self.r, "TOTALLY_FAKE"))

    def test_hasattr_is_useless(self):
        self.assertTrue(hasattr(self.r, "EXPORT_DRT"))
        self.assertTrue(hasattr(self.r, "TOTALLY_FAKE"))


class ApiConstantTest(unittest.TestCase):
    def setUp(self):
        self.r = ResolveBridgeStub()

    def test_resolves_real_constants(self):
        self.assertEqual(1.0, _api_constant(self.r, "EXPORT_DRT"))
        self.assertEqual(0.0, _api_constant(self.r, "EXPORT_NONE"))
        self.assertEqual(5.0, _api_constant(self.r, "EXPORT_LUT_CUBE"))

    def test_zero_valued_constant_is_not_lost(self):
        """EXPORT_NONE is 0.0 — a truthiness test would drop it."""
        self.assertIsNotNone(_api_constant(self.r, "EXPORT_NONE"))

    def test_fabricated_name_is_rejected(self):
        self.assertIsNone(_api_constant(self.r, "EXPORT_TOTALLY_FAKE"))
        self.assertIsNone(_api_constant(self.r, "NotAConstant"))

    def test_method_is_not_a_constant(self):
        self.assertIsNone(_api_constant(self.r, "GetProjectManager"))

    def test_none_and_empty_are_safe(self):
        self.assertIsNone(_api_constant(None, "EXPORT_DRT"))
        self.assertIsNone(_api_constant(self.r, ""))
        self.assertIsNone(_api_constant(self.r, None))

    def test_dir_membership_would_have_failed(self):
        """Pin the actual regression: the old test can never see a constant."""
        self.assertNotIn("EXPORT_DRT", dir(self.r))
        self.assertIsNotNone(_api_constant(self.r, "EXPORT_DRT"))

    def test_has_method_still_uses_dir_and_still_works(self):
        """The #110 finding-11 fix was right for methods — don't undo it."""
        self.assertTrue(_has_method(self.r, "GetProjectManager"))
        self.assertFalse(_has_method(self.r, "TotallyMadeUpMethod"))


class ExportSpecResolutionTest(unittest.TestCase):
    """End of the chain: the spec must carry the NUMBER, not the name."""

    def test_timeline_export_spec_resolves_drt_to_a_number(self):
        import src.server  # noqa: F401  (breaks the actions<->server import cycle)
        from src.domains.timeline_conform_interchange.actions import _timeline_export_spec

        spec = _timeline_export_spec({"format": "drt"}, ResolveBridgeStub())

        self.assertEqual(1.0, spec["export_type"],
                         "export_type must be the numeric constant, not 'EXPORT_DRT'")
        self.assertEqual(0.0, spec["export_subtype"])
        self.assertEqual("EXPORT_DRT", spec["export_type_name"])

    def test_unknown_constant_still_degrades_to_the_name(self):
        import src.server  # noqa: F401
        from src.domains.timeline_conform_interchange.actions import _timeline_export_spec

        spec = _timeline_export_spec(
            {"format": "drt", "export_subtype": "EXPORT_MADE_UP"}, ResolveBridgeStub())

        self.assertEqual(1.0, spec["export_type"])
        self.assertEqual("EXPORT_MADE_UP", spec["export_subtype"])

    def test_no_resolve_object_degrades_to_the_name(self):
        import src.server  # noqa: F401
        from src.domains.timeline_conform_interchange.actions import _timeline_export_spec

        spec = _timeline_export_spec({"format": "drt"}, None)
        self.assertEqual("EXPORT_DRT", spec["export_type"])


if __name__ == "__main__":
    unittest.main()
