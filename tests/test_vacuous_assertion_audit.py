"""Ratchet against the three vacuous-assertion shapes swept in #121 §3.

The sweep found, across 193 test files:

  * shape 1, assert-on-mock — 0 real (2 heuristic hits, both the deliberate
    "a MagicMock would have hidden this" demonstrations from #119).
  * shape 2, error-envelope-passes-for-the-wrong-reason — 82 sites, now pinned
    to the specific cause via `tests._error_envelope_helpers.assert_error_mentions`.
    Two of them were passing for a genuinely wrong reason: they patched
    `src.server._check`, which has not owned the dispatch since the #52
    restructure, so they were reaching a RUNNING Resolve and asserting on
    whatever error it happened to return.
  * shape 3, swallowed exceptions — 2 sites, both `try: ... except X: pass`
    around a deliberately-raised exception. Both now use `assertRaises`, which
    also fails if the code under test swallows the exception (the bug each test
    was nominally about).

This file freezes that result. It is the same ratchet shape as
`tests/test_discarded_mutator_returns.py`: it does not claim the accepted sites
are wrong, only that the set stops growing silently.

When this fails: pin the assertion to the actual cause
(`assert_error_mentions(self, result, "track_type", "required")`), or use
`assertRaises` instead of `try/except: pass`. Add to an allowlist below only
with a reason in the diff.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent

ASSERT_METHODS = frozenset({
    "assertTrue", "assertFalse", "assertEqual", "assertNotEqual", "assertIn",
    "assertNotIn", "assertIsNone", "assertIsNotNone", "assertGreater",
    "assertGreaterEqual", "assertLess", "assertLessEqual", "assertIs",
    "assertRaises", "assertDictEqual", "assertListEqual", "assertAlmostEqual",
})

# "<file>::<test>" -> why a bare `"error" in result` assertion is right here.
ACCEPTED_ERROR_ENVELOPE_ONLY = {
    "tests/core/test_capability_gate_behaviour.py::test_a_magicmock_folder_is_refused_by_every_gate":
        "the assertion IS 'it refused, for any reason' — the test exists to prove a "
        "MagicMock never gets past the gate, so pinning a message would narrow it",
}

# "<file>::<test>" -> why a try/except in the test body is right here.
ACCEPTED_SWALLOWED = {}


def _test_modules():
    return sorted(p for p in TESTS.rglob("test_*.py") if "__pycache__" not in p.parts)


def _test_functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node


def scan():
    """{'error_envelope_only': [key], 'swallowed': [key]} across tests/."""
    envelope_only, swallowed = [], []
    for path in _test_modules():
        if path.name == pathlib.Path(__file__).name:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in _test_functions(tree):
            key = f"{rel}::{fn.name}"
            asserts, error_only = 0, 0
            for node in ast.walk(fn):
                if isinstance(node, ast.Try):
                    for handler in node.handlers:
                        body = handler.body
                        if len(body) == 1 and isinstance(body[0], ast.Pass):
                            swallowed.append(key)
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in ASSERT_METHODS or not node.args:
                    continue
                asserts += 1
                first = node.args[0]
                if (node.func.attr == "assertIn"
                        and isinstance(first, ast.Constant) and first.value == "error"):
                    error_only += 1
            # Only vacuous when NOTHING else in the test pins the failure.
            if error_only and asserts == error_only:
                envelope_only.append(key)
    return {"error_envelope_only": envelope_only, "swallowed": swallowed}


class VacuousAssertionRatchetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.found = scan()

    def test_the_scan_reaches_the_suite(self):
        self.assertGreater(len(_test_modules()), 150)

    def test_no_new_error_envelope_only_tests(self):
        new = sorted(set(self.found["error_envelope_only"]) - set(ACCEPTED_ERROR_ENVELOPE_ONLY))
        self.assertEqual(
            [], new,
            "test(s) whose ONLY assertion is that 'error' is in the result. That passes "
            "whatever went wrong — a missing param, no timeline, a patch aimed at the "
            "wrong module — so it cannot fail for the reason it was written. Use "
            "tests._error_envelope_helpers.assert_error_mentions(self, result, ...):\n  "
            + "\n  ".join(new),
        )

    def test_no_new_swallowed_exceptions(self):
        new = sorted(set(self.found["swallowed"]) - set(ACCEPTED_SWALLOWED))
        self.assertEqual(
            [], new,
            "`except ...: pass` inside a test body also passes when the code under test "
            "SWALLOWS the exception, which is usually the bug being tested. Use "
            "assertRaises:\n  " + "\n  ".join(new),
        )

    def test_the_accepted_lists_have_no_stale_entries(self):
        stale = sorted(
            set(ACCEPTED_ERROR_ENVELOPE_ONLY) - set(self.found["error_envelope_only"])
        ) + sorted(set(ACCEPTED_SWALLOWED) - set(self.found["swallowed"]))
        self.assertEqual(
            [], stale,
            "allowlist entries that no longer match anything — prune them so the "
            f"allowlist keeps meaning something: {stale}",
        )


class RatchetIsNotVacuousTest(unittest.TestCase):
    """The scanner is fed both shapes, per #121 task 2's rule for every guard."""

    def _scan_source(self, body: str):
        import tempfile
        import textwrap
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "test_synthetic_vacuous.py").write_text(textwrap.dedent(body), encoding="utf-8")
            module = sys.modules[__name__]
            with mock.patch.object(module, "TESTS", root), \
                    mock.patch.object(module, "REPO_ROOT", root.parent):
                return scan()

    def test_detects_an_error_only_assertion(self):
        found = self._scan_source(
            """
            import unittest


            class T(unittest.TestCase):
                def test_it_fails_somehow(self):
                    result = {"error": {"message": "anything at all"}}
                    self.assertIn("error", result)
            """
        )
        self.assertTrue(
            any(k.endswith("::test_it_fails_somehow") for k in found["error_envelope_only"]),
            found,
        )

    def test_does_not_flag_an_assertion_that_pins_the_cause(self):
        found = self._scan_source(
            """
            import unittest


            class T(unittest.TestCase):
                def test_it_fails_for_the_stated_reason(self):
                    result = {"error": {"message": "track_type is required"}}
                    self.assertIn("error", result)
                    self.assertIn("track_type", result["error"]["message"])
            """
        )
        self.assertEqual([], found["error_envelope_only"])

    def test_detects_a_swallowed_exception(self):
        found = self._scan_source(
            """
            import unittest


            class T(unittest.TestCase):
                def test_swallows(self):
                    try:
                        raise ValueError("boom")
                    except ValueError:
                        pass
                    self.assertTrue(True)
            """
        )
        self.assertTrue(any(k.endswith("::test_swallows") for k in found["swallowed"]), found)


if __name__ == "__main__":
    unittest.main()
