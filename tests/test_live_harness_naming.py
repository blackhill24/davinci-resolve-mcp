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
import unittest

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


if __name__ == "__main__":
    unittest.main()
