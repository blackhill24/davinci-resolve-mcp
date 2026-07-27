"""Static guard: no `test_*.py` under tests/ may reach a live Resolve.

CLAUDE.md fixes the convention: `tests/test_*.py` = offline unit, collected by
`pytest tests/`; `tests/live_*.py` = requires a running Resolve, run by hand and
gated by tests/preflight.py. pytest's default collection globs `test_*.py`, so
the filename is the only thing keeping live harnesses out of the offline suite.

Two files broke that (audit #111, findings 1-3):

  * `tests/test_live_api.py` and `tests/test_resolve20_api.py` did a
    module-level `import DaVinciResolveScript`, so `pytest tests/` died at
    COLLECTION on any machine without Resolve — including the ubuntu-latest
    runner in .github/workflows/npm-publish.yml. The publish workflow's full
    offline-suite step exited 2 on every tag push and never reached the ruff /
    Node / smoke steps below it.
  * Because their names were `test_*`, neither went through preflight, so they
    were the only two live harnesses in the tree reaching Resolve with no gate
    at all. `test_live_api.py::test_api` was additionally collected and RUN by
    pytest, where it created a timeline in the user's open project.

Both were renamed to `live_*`. This guard is what stops the class recurring:
it fails if any `test_*.py` imports the Resolve scripting module or reaches for
a live handle, regardless of whether Resolve happens to be installed on the
machine running it. It is a source-text check, so it is itself offline-safe.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent

# Importing this is what hard-fails collection on a Resolve-less runner.
SCRIPTING_MODULE = "DaVinciResolveScript"

# Entry points that only exist on a live Resolve handle. `scriptapp` is the
# connection call itself; the rest are the roots every live harness starts from.
LIVE_HANDLE_CALLS = {"scriptapp"}

# Offline suites legitimately name these in mock/patch targets and in assertions
# about the connector's own behaviour, so a bare substring grep would be noisy.
# Only real imports and real calls count — hence the AST walk below.


def _test_modules():
    """Every pytest-collected module under tests/ (recursive), by default globs."""
    return sorted(
        path
        for path in TESTS.rglob("test_*.py")
        if "__pycache__" not in path.parts
    )


class LiveHarnessNamingTest(unittest.TestCase):
    def test_the_scan_actually_matches_files(self):
        # Both checks below are "no offenders found". If the rglob stopped
        # matching — tests/ moved, the naming convention changed — that reads
        # identically to "clean". Pin the scan set (#121 task 2).
        self.assertGreater(len(_test_modules()), 100)
        self.assertGreater(
            len([p for p in TESTS.rglob("live_*.py") if "__pycache__" not in p.parts]), 40
        )

    def test_no_test_module_imports_the_scripting_module(self):
        """`import DaVinciResolveScript` in a test_*.py breaks Resolve-less collection."""
        offenders = []
        for path in _test_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == SCRIPTING_MODULE:
                            offenders.append(f"{path.relative_to(TESTS)}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    if root == SCRIPTING_MODULE:
                        offenders.append(f"{path.relative_to(TESTS)}:{node.lineno}")

        self.assertEqual(
            [],
            offenders,
            "test_*.py is collected by `pytest tests/` on machines without DaVinci "
            "Resolve, where importing "
            f"{SCRIPTING_MODULE} raises ModuleNotFoundError at collection time and "
            "aborts the whole suite (exit 2). Rename these to live_*.py and gate "
            f"them on tests/preflight.py: {offenders}",
        )

    def test_no_test_module_opens_a_live_resolve_handle(self):
        """A live handle in a test_*.py means an ungated, uncollected-by-preflight harness."""
        offenders = []
        for path in _test_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in LIVE_HANDLE_CALLS:
                    offenders.append(f"{path.relative_to(TESTS)}:{node.lineno} {name}()")

        self.assertEqual(
            [],
            offenders,
            "these call a live Resolve connection from an offline-tier test_*.py, so "
            "they bypass the tests/preflight.py gate that every live_*.py goes "
            f"through and can mutate the user's open project: {offenders}",
        )

    def test_every_live_harness_is_preflight_gated(self):
        """The invariant the renames restore: all live_*.py reference preflight."""
        ungated = [
            str(path.relative_to(TESTS))
            for path in sorted(TESTS.rglob("live_*.py"))
            if "__pycache__" not in path.parts
            and "preflight" not in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(
            [],
            ungated,
            "every live_*.py harness must gate on tests/preflight.py (see #30) so a "
            "missing/closed Resolve exits 2/3 instead of half-running against the "
            f"user's project: {ungated}",
        )


class NamingGuardIsNotVacuousTest(unittest.TestCase):
    """Reintroduce audit #111's three findings and confirm the guard fires.

    The guard's whole value is that it would have caught `tests/test_live_api.py`
    before it broke every publish run and started creating timelines in the
    user's open project. That claim is only worth anything if the detection
    still works, so each of the three checks gets its offender back — in a temp
    tree, with the module's `TESTS` root pointed at it.
    """

    def _guard_against(self, files: dict, method: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name, body in files.items():
                (root / name).write_text(textwrap.dedent(body), encoding="utf-8")
            with mock.patch.object(sys.modules[__name__], "TESTS", root):
                with self.assertRaises(AssertionError) as caught:
                    LiveHarnessNamingTest(method).debug()
            return str(caught.exception)

    def test_detects_a_scripting_module_import_in_a_test_file(self):
        message = self._guard_against(
            {"test_synthetic_live.py": "import DaVinciResolveScript\n"},
            "test_no_test_module_imports_the_scripting_module",
        )
        self.assertIn("test_synthetic_live.py:1", message)

    def test_detects_a_live_handle_opened_from_a_test_file(self):
        message = self._guard_against(
            {
                "test_synthetic_handle.py": """
                import bmd


                def test_thing():
                    resolve = bmd.scriptapp("Resolve")
                    return resolve
                """
            },
            "test_no_test_module_opens_a_live_resolve_handle",
        )
        self.assertIn("scriptapp()", message)

    def test_detects_an_ungated_live_harness(self):
        message = self._guard_against(
            {"live_synthetic_probe.py": "def main():\n    return 0\n"},
            "test_every_live_harness_is_preflight_gated",
        )
        self.assertIn("live_synthetic_probe.py", message)

    def test_a_clean_tree_passes(self):
        # The converse: a guard that fails on everything is not a guard either.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "test_synthetic_clean.py").write_text(
                "def test_ok():\n    assert True\n", encoding="utf-8"
            )
            (root / "live_synthetic_clean.py").write_text(
                "from tests import preflight\n\n\ndef main():\n    return 0\n",
                encoding="utf-8",
            )
            with mock.patch.object(sys.modules[__name__], "TESTS", root):
                for method in (
                    "test_no_test_module_imports_the_scripting_module",
                    "test_no_test_module_opens_a_live_resolve_handle",
                    "test_every_live_harness_is_preflight_gated",
                ):
                    LiveHarnessNamingTest(method).debug()


if __name__ == "__main__":
    unittest.main()
