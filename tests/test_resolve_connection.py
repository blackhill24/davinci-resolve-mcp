"""Offline tests for src/utils/resolve_connection.py — the connector core.

DaVinciResolveScript is faked via sys.modules so no Resolve is needed.
"""
import sys
import types
import unittest
from unittest import mock

from src.utils import resolve_connection as rc


def _fake_dvr(scriptapp_result):
    m = types.ModuleType("DaVinciResolveScript")
    m.scriptapp = lambda name: scriptapp_result
    return m


class InitializeResolveTest(unittest.TestCase):
    def test_success_returns_resolve_object(self):
        fake_resolve = mock.Mock()
        fake_resolve.GetProductName.return_value = "DaVinci Resolve Studio"
        fake_resolve.GetVersionString.return_value = "21.0"
        with mock.patch.dict(sys.modules, {"DaVinciResolveScript": _fake_dvr(fake_resolve)}):
            self.assertIs(rc.initialize_resolve(), fake_resolve)

    def test_scriptapp_none_returns_none(self):
        with mock.patch.dict(sys.modules, {"DaVinciResolveScript": _fake_dvr(None)}):
            self.assertIsNone(rc.initialize_resolve())

    def test_import_error_returns_none(self):
        # Force the import inside initialize_resolve to fail.
        with mock.patch.dict(sys.modules, {"DaVinciResolveScript": None}):
            self.assertIsNone(rc.initialize_resolve())

    def test_unexpected_error_returns_none(self):
        boom = mock.Mock()
        boom.GetProductName.side_effect = RuntimeError("api exploded")
        with mock.patch.dict(sys.modules, {"DaVinciResolveScript": _fake_dvr(boom)}):
            self.assertIsNone(rc.initialize_resolve())


class EnvironmentCheckTest(unittest.TestCase):
    def test_all_set(self):
        env = {"RESOLVE_SCRIPT_API": "/api", "RESOLVE_SCRIPT_LIB": "/lib.so"}
        with mock.patch.dict("os.environ", env, clear=False):
            out = rc.check_environment_variables()
        self.assertTrue(out["all_set"])
        self.assertEqual(out["missing"], [])
        self.assertEqual(out["resolve_script_api"], "/api")

    def test_missing_reported(self):
        import os
        cleaned = {k: v for k, v in os.environ.items()
                   if k not in ("RESOLVE_SCRIPT_API", "RESOLVE_SCRIPT_LIB")}
        with mock.patch.dict("os.environ", cleaned, clear=True):
            out = rc.check_environment_variables()
        self.assertFalse(out["all_set"])
        self.assertEqual(sorted(out["missing"]),
                         ["RESOLVE_SCRIPT_API", "RESOLVE_SCRIPT_LIB"])


if __name__ == "__main__":
    unittest.main()
