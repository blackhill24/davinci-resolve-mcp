"""One definition per bridge helper — asserted, not hoped for (#119 task 6).

`_has_method` and `_api_constant` were defined twice: `src/core/envelope.py` and
`src/granular/common.py`. The copy in `common.py` is the one the export path binds
(`src/granular/timeline.py` star-imports it), so the #118 regression test — which
imports the `envelope` copy — pinned a function the broken code never calls. The
proof from the issue: break only `src/granular/common._api_constant` and the full
offline suite still reports `2153 passed, 0 failed`.

Copy-pasting the corrected function into the second module is what created the
problem, so this file guards the *structural* property instead: every module that
exposes one of these names must expose the **same object**. If a future edit
re-introduces a local `def _has_method` in any of them, these tests fail
immediately — regardless of whether the new copy happens to be correct today.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import src.core.envelope as envelope  # noqa: E402

# Modules that must all bind the canonical helper, listed explicitly so adding a
# new granular module with its own copy is a visible omission rather than a silent
# gap. `timeline` and `timeline_item` are the two the export regression ran through.
_BINDING_MODULES = (
    "src.granular.common",
    "src.granular.timeline",
    "src.granular.timeline_item",
)

_SHARED_NAMES = ("_has_method", "_api_constant")


class BridgeHelperIdentityTest(unittest.TestCase):
    def test_every_module_binds_the_canonical_helper(self):
        for mod_name in _BINDING_MODULES:
            module = importlib.import_module(mod_name)
            for helper in _SHARED_NAMES:
                with self.subTest(module=mod_name, helper=helper):
                    bound = getattr(module, helper, None)
                    self.assertIsNotNone(
                        bound, f"{mod_name} does not expose {helper}")
                    self.assertIs(
                        bound, getattr(envelope, helper),
                        f"{mod_name}.{helper} is a SECOND copy — import it from "
                        f"src.core.envelope instead of redefining it (#119 task 6)")

    def test_no_module_redefines_them_locally(self):
        """A `def _has_method` in a binding module's own source is the bug shape."""
        import ast

        for mod_name in _BINDING_MODULES:
            module = importlib.import_module(mod_name)
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            defined = {
                node.name
                for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef)
            }
            for helper in _SHARED_NAMES:
                with self.subTest(module=mod_name, helper=helper):
                    self.assertNotIn(
                        helper, defined,
                        f"{mod_name} defines its own {helper}; two copies is the bug")

    def test_the_export_path_binds_the_same_object_as_the_helper_module(self):
        """Restates the issue's own reproduction as an assertion."""
        import src.granular.common as common
        import src.granular.timeline as timeline

        self.assertIs(timeline._api_constant, common._api_constant)
        self.assertIs(timeline._api_constant, envelope._api_constant)


if __name__ == "__main__":
    unittest.main()
