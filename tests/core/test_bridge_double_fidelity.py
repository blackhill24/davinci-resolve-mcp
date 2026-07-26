"""Meta-test: the shared bridge double must behave like the real bridge (#119 task 2).

Every other test that uses `ResolveBridgeDouble` is only as trustworthy as this
file. `tests/core/test_api_constant_resolution.py:BridgeStubFidelityTest` proved the
pattern for one local stub; this generalises it to the one shared double.

The four behaviours pinned here were verified live on Resolve Studio 21.0.2.4 and
are restated in `tests/bridge_double.py`:

    dir(resolve)                     -> methods only, no EXPORT_*
    getattr(resolve, 'EXPORT_DRT')   -> a number
    getattr(resolve, 'TOTALLY_FAKE') -> None      (fabricated, never raises)
    hasattr(resolve, 'TOTALLY_FAKE') -> True      (so hasattr is useless)

A fifth assertion set pins the *consequence*: `_has_method` and `_api_constant`
must both answer correctly when handed this double — and, critically, a plain
`MagicMock` must answer both of them WRONG. That last test is the anti-regression
for the whole issue: if someone "fixes" the double by making it MagicMock-like,
it fails here rather than silently going green everywhere else.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.core.envelope import _api_constant, _has_method  # noqa: E402
from src.granular.common import _api_constant as _granular_api_constant  # noqa: E402
from src.granular.common import _has_method as _granular_has_method  # noqa: E402
from tests.bridge_double import (  # noqa: E402
    RESOLVE_EXPORT_CONSTANTS,
    ResolveBridgeDouble,
    call_names,
    calls_of,
    make_resolve,
    method_returning,
    reset_calls,
)


class BridgeDoubleFidelityTest(unittest.TestCase):
    """The four load-bearing behaviours of BlackmagicFusion.PyRemoteObject."""

    def setUp(self):
        self.r = make_resolve(methods={"GetProjectManager": None, "OpenPage": True})

    def test_dir_lists_methods_and_only_methods(self):
        listed = dir(self.r)
        self.assertIn("GetProjectManager", listed)
        self.assertIn("OpenPage", listed)
        self.assertNotIn("EXPORT_DRT", listed)
        self.assertNotIn("EXPORT_NONE", listed)

    def test_dir_does_not_leak_the_doubles_own_machinery(self):
        """`_rbd_*` bookkeeping must never look like a Resolve method."""
        self.assertEqual([], [n for n in dir(self.r) if n.startswith("_rbd_")])

    def test_getattr_returns_real_constants(self):
        self.assertEqual(RESOLVE_EXPORT_CONSTANTS["EXPORT_DRT"],
                         getattr(self.r, "EXPORT_DRT"))
        self.assertEqual(0.0, getattr(self.r, "EXPORT_NONE"))

    def test_getattr_fabricates_none_and_never_raises(self):
        self.assertIsNone(getattr(self.r, "TOTALLY_FAKE"))
        self.assertIsNone(self.r.SomeMethodThatDoesNotExist)
        try:
            self.r.AnotherFakeName
        except AttributeError:  # pragma: no cover - the bridge never does this
            self.fail("the bridge fabricates; it must not raise AttributeError")

    def test_hasattr_is_useless(self):
        self.assertTrue(hasattr(self.r, "GetProjectManager"))
        self.assertTrue(hasattr(self.r, "EXPORT_DRT"))
        self.assertTrue(hasattr(self.r, "TOTALLY_FAKE"))

    def test_constants_are_non_callable_numbers(self):
        """`_api_constant` rejects callables — every constant must survive it."""
        for name in RESOLVE_EXPORT_CONSTANTS:
            with self.subTest(constant=name):
                value = getattr(self.r, name)
                self.assertFalse(callable(value))
                self.assertIsInstance(value, float)

    def test_export_none_is_zero_so_truthiness_bugs_still_fail(self):
        self.assertEqual(0.0, RESOLVE_EXPORT_CONSTANTS["EXPORT_NONE"])
        self.assertIsNotNone(_api_constant(self.r, "EXPORT_NONE"))


class BridgeDoubleAgainstProductionHelpersTest(unittest.TestCase):
    """The double must drive the REAL branch of both helper copies."""

    def setUp(self):
        self.r = make_resolve(methods={"GetProjectManager": None})

    def test_has_method_sees_real_methods_and_rejects_fabricated_ones(self):
        for helper in (_has_method, _granular_has_method):
            with self.subTest(helper=helper.__module__):
                self.assertTrue(helper(self.r, "GetProjectManager"))
                self.assertFalse(helper(self.r, "TotallyMadeUpMethod"))

    def test_api_constant_resolves_constants_and_rejects_fabrications(self):
        for helper in (_api_constant, _granular_api_constant):
            with self.subTest(helper=helper.__module__):
                self.assertEqual(RESOLVE_EXPORT_CONSTANTS["EXPORT_DRT"],
                                 helper(self.r, "EXPORT_DRT"))
                self.assertIsNone(helper(self.r, "EXPORT_TOTALLY_FAKE"))
                self.assertIsNone(helper(self.r, "GetProjectManager"))


class MagicMockIsUnfaithfulTest(unittest.TestCase):
    """Pin *why* the double exists: a MagicMock answers both probes wrong.

    If this test ever fails, either MagicMock changed or someone made the shared
    double MagicMock-shaped — both mean the fidelity guarantee is gone.
    """

    def test_magicmock_reports_every_real_method_as_absent(self):
        m = mock.MagicMock()
        self.assertFalse(_has_method(m, "GetProjectManager"))   # real bridge: True

    def test_magicmock_cannot_resolve_a_constant(self):
        m = mock.MagicMock()
        self.assertIsNone(_api_constant(m, "EXPORT_DRT"))       # real bridge: a number

    def test_the_shared_double_gets_both_right_where_magicmock_does_not(self):
        r = make_resolve(methods={"GetProjectManager": None})
        self.assertTrue(_has_method(r, "GetProjectManager"))
        self.assertIsNotNone(_api_constant(r, "EXPORT_DRT"))


class BridgeDoubleCallRecordingTest(unittest.TestCase):
    """The recording surface tests assert against."""

    def test_records_name_args_and_kwargs_in_order(self):
        tl = ResolveBridgeDouble(methods={"Export": True, "SetName": False})
        self.assertTrue(tl.Export("/tmp/x.drt", 2.0, subtype=0.0))
        self.assertFalse(tl.SetName("cut"))
        self.assertEqual(["Export", "SetName"], call_names(tl))
        self.assertEqual(("Export", ("/tmp/x.drt", 2.0), {"subtype": 0.0}),
                         calls_of(tl)[0])

    def test_callable_spec_receives_the_arguments(self):
        seen = []
        tl = ResolveBridgeDouble(methods={"Export": lambda *a, **k: seen.append(a) or True})
        tl.Export("/tmp/y.edl", 3.0, 0.0)
        self.assertEqual([("/tmp/y.edl", 3.0, 0.0)], seen)

    def test_reset_calls_clears_the_log(self):
        tl = ResolveBridgeDouble(methods={"Export": True})
        tl.Export("/tmp/z.drt")
        reset_calls(tl)
        self.assertEqual([], calls_of(tl))

    def test_method_returning_sugar(self):
        tl = ResolveBridgeDouble(methods={"GetName": method_returning("cut_v3")})
        self.assertEqual("cut_v3", tl.GetName())

    def test_fabricated_names_are_not_recorded_as_calls(self):
        """A fabricated attribute is None — calling it is a TypeError, as live."""
        tl = ResolveBridgeDouble(methods={"Export": True})
        self.assertIsNone(tl.NotAMethod)
        with self.assertRaises(TypeError):
            tl.NotAMethod()
        self.assertEqual([], calls_of(tl))


if __name__ == "__main__":
    unittest.main()
