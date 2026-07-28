"""No module under `src/` may CONNECT to Resolve at import time (#158).

Importing the bridge module is fine; calling `scriptapp()` while the module
body runs is not. When Resolve is up but **wedged** — still running, holding
its scripting socket, never answering, which #153 documents as a real failure
mode — that call blocks forever, and because `src/granular/common.py` is
reachable from ordinary offline tests it took out the entire pytest
**collection**: no output, no timeout, no indication of the cause.

What makes this worth a guard rather than a comment is that it is invisible
under every normal condition. With Resolve closed (CI, most dev machines) or
Resolve healthy, the suite is green in ~45s either way — so the defect can be
reintroduced and nothing anywhere goes red. It is the same family as #111,
where two drift guards had `ImportError`'d out of every CI run unnoticed.

The guard is static on purpose. Detecting it dynamically would mean importing
the modules and watching for a connection, which is exactly the hang.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# `scriptapp(...)` is the connect. Loading the module (`import
# DaVinciResolveScript`) is allowed — that is what every lazy connector does.
CONNECT_CALL = "scriptapp"


def _module_level_connect(tree: ast.Module) -> list:
    """Line numbers of `scriptapp(...)` calls that run when the module is imported.

    Walks only the module body and the statements nested inside it — `try`,
    `if`, `with`, loops — deliberately NOT into function or class bodies, which
    run on call, not on import. The original defect sat inside a module-level
    `try:`, so a scan of only bare top-level statements would have missed it.
    """
    found = []

    def visit(node):
        # A def/class body runs on call, not on import — that deferral IS the
        # fix, so never descend into one. Skipping it here (rather than at the
        # top-level loop) is what keeps a function defined inside a module-level
        # `try:` from reading as an offender.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == CONNECT_CALL):
            found.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for statement in tree.body:
        visit(statement)
    return sorted(set(found))


class ImportTimeConnectGuard(unittest.TestCase):
    def _python_files(self) -> list:
        return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)

    def test_the_scan_actually_sees_the_tree(self):
        """A guard whose scan silently matches nothing reads as safety while
        providing none (#110). Pin the file count and that the sentinel module
        is in it."""
        files = self._python_files()
        self.assertGreater(len(files), 100, "the src/ scan found suspiciously few files")
        self.assertIn(SRC / "granular" / "common.py", files)

    def test_the_detector_catches_a_connect_nested_in_a_module_level_try(self):
        """The exact shape of the #158 defect — inside `try:`, not bare."""
        source = (
            "import DaVinciResolveScript as dvr_script\n"
            "try:\n"
            "    resolve = dvr_script.scriptapp('Resolve')\n"
            "except Exception:\n"
            "    resolve = None\n"
        )
        self.assertEqual(_module_level_connect(ast.parse(source)), [3])

    def test_the_detector_allows_a_connect_inside_a_function(self):
        source = (
            "import DaVinciResolveScript as dvr_script\n"
            "def get_resolve():\n"
            "    return dvr_script.scriptapp('Resolve')\n"
        )
        self.assertEqual(_module_level_connect(ast.parse(source)), [])

    def test_a_function_defined_inside_a_module_level_try_is_not_an_offender(self):
        """The false positive a whole-subtree walk would produce: the `def` runs
        at import, its body does not."""
        source = (
            "try:\n"
            "    import DaVinciResolveScript as dvr_script\n"
            "    def _try_connect():\n"
            "        return dvr_script.scriptapp('Resolve')\n"
            "except ImportError:\n"
            "    dvr_script = None\n"
        )
        self.assertEqual(_module_level_connect(ast.parse(source)), [])

    def test_the_detector_catches_a_connect_in_an_except_handler(self):
        source = (
            "try:\n"
            "    pass\n"
            "except Exception:\n"
            "    resolve = dvr_script.scriptapp('Resolve')\n"
        )
        self.assertEqual(_module_level_connect(ast.parse(source)), [4])

    def test_no_src_module_connects_at_import_time(self):
        offenders = []
        for path in self._python_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # not our problem here; other guards cover it
                continue
            for line in _module_level_connect(tree):
                offenders.append(f"{path.relative_to(SRC.parent)}:{line}")
        self.assertEqual(
            offenders, [],
            "scriptapp() runs at import time in these modules. A wedged Resolve "
            "then hangs the importer forever, including pytest collection (#158). "
            "Load the bridge module at import if you must, but connect lazily — "
            "see get_resolve() in src/core/live_connection.py.")


if __name__ == "__main__":
    unittest.main()
