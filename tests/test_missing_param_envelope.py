"""#142 finding 3: a missing required param is invalid_input, not a traceback.

An AST sweep found 88+ `p["key"]` reads across the compound domains with no
`"key" in p`, no `p.get(...)`, no `_validate_params` rule naming them and no
enclosing `try`. There is no global exception wrapper on tool bodies —
`_install_threaded_tool_dispatch` only offloads to a worker thread — so an
ordinary caller mistake escaped as a raw `KeyError` traceback rather than the
error envelope the rest of the codebase maintains.

The fix is at the seam rather than at 88 read sites: the params dict itself
knows it holds caller input (`ToolParams.__missing__` -> `MissingParam`) and
`@missing_param_envelope` converts that at the tool boundary. So the guard here
is structural — every compound tool that builds a params dict must carry the
decorator — plus the two worked examples from the issue.
"""

from __future__ import annotations

import ast
import os
import unittest

from src import server as _server  # noqa: F401 - import first: the domain action
# modules import back from src.server, so importing one directly leaves a
# partially-initialized module and a circular-import error.
from src.core.params import (
    MissingParam,
    ToolParams,
    missing_param_envelope,
    tool_params,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ToolParamsTest(unittest.TestCase):
    def test_a_missing_key_raises_missingparam_not_a_bare_keyerror(self):
        p = tool_params({"a": 1})
        with self.assertRaises(MissingParam) as caught:
            p["index"]
        self.assertEqual("index", caught.exception.key)

    def test_missingparam_is_still_a_keyerror_for_existing_handlers(self):
        # Any pre-existing `except KeyError` around a param read must keep
        # behaving exactly as before.
        self.assertTrue(issubclass(MissingParam, KeyError))
        try:
            tool_params({})["x"]
        except KeyError as exc:
            self.assertIsInstance(exc, MissingParam)
        else:  # pragma: no cover
            self.fail("expected a KeyError")

    def test_present_keys_and_none_params_behave_like_a_plain_dict(self):
        self.assertEqual(1, tool_params({"a": 1})["a"])
        self.assertEqual({}, tool_params(None))
        self.assertIsInstance(tool_params(None), ToolParams)
        self.assertIsNone(tool_params({}).get("missing"))


class MissingParamEnvelopeTest(unittest.TestCase):
    def test_the_decorator_converts_to_an_invalid_input_envelope(self):
        @missing_param_envelope
        def tool(params=None):
            p = tool_params(params)
            return {"success": True, "index": p["index"]}

        error = tool({})["error"]
        self.assertEqual("MISSING_PARAM", error["code"])
        self.assertEqual("invalid_input", error["category"])
        self.assertIn("index", error["message"])
        self.assertEqual("index", error["state"]["missing_param"])
        # And the happy path is untouched.
        self.assertEqual({"success": True, "index": 3}, tool({"index": 3}))

    def test_an_internal_keyerror_is_not_masked_as_bad_input(self):
        # The whole point of the typed exception: a genuine bug must still
        # surface as a bug, not be relabelled "the caller forgot a param".
        @missing_param_envelope
        def tool(params=None):
            _p = tool_params(params)
            internal = {"a": 1}
            return internal["definitely_missing"]

        with self.assertRaises(KeyError) as caught:
            tool({})
        self.assertNotIsInstance(caught.exception, MissingParam)


class EveryToolIsGuardedTest(unittest.TestCase):
    """Structural: a new compound tool must not reopen the hole."""

    def _functions_building_tool_params(self):
        for dirpath, _dirs, files in os.walk(os.path.join(_ROOT, "src")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as handle:
                    try:
                        tree = ast.parse(handle.read())
                    except SyntaxError:  # pragma: no cover
                        continue
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    builds = any(
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id in ("tool_params", "_tool_params")
                        for sub in ast.walk(node)
                    )
                    if builds:
                        yield os.path.relpath(path, _ROOT), node

    def test_every_tool_that_builds_a_params_dict_carries_the_decorator(self):
        undecorated = []
        seen = 0
        for relpath, node in self._functions_building_tool_params():
            seen += 1
            names = {
                (d.func if isinstance(d, ast.Call) else d).id
                for d in node.decorator_list
                if isinstance(d.func if isinstance(d, ast.Call) else d, ast.Name)
            }
            if not names & {"missing_param_envelope", "_missing_param_envelope"}:
                undecorated.append(f"{relpath}:{node.lineno} {node.name}")
        self.assertGreater(seen, 30, "the sweep found suspiciously few tools")
        self.assertEqual(
            [], undecorated,
            "these read caller params but would raise a raw KeyError:\n  "
            + "\n  ".join(undecorated),
        )

    def test_the_old_unguarded_idiom_is_gone(self):
        offenders = []
        for dirpath, _dirs, files in os.walk(os.path.join(_ROOT, "src")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as handle:
                    if "p = params or {}" in handle.read():
                        offenders.append(os.path.relpath(path, _ROOT))
        self.assertEqual(
            [], offenders,
            "use `p = _tool_params(params)` so a missing key is an envelope",
        )


class WorkedExamplesFromTheIssueTest(unittest.TestCase):
    """The exact call the issue named, driven far enough to hit the read.

    Stubbing the item lookup matters: without it the tool bails on "no timeline"
    long before `p["index"]`, and the test would pass on the wrong error — the
    vacuous-assertion failure mode tests/test_vacuous_assertion_audit.py exists
    to catch.
    """

    def test_timeline_item_takes_get_by_index_without_index(self):
        from unittest import mock

        from src.domains.timeline_edit import actions as timeline_edit_actions
        from tests.bridge_double import ResolveBridgeDouble

        item = ResolveBridgeDouble(methods={"GetTakeByIndex": {"take": 1}})
        fn = getattr(timeline_edit_actions.timeline_item_takes, "__wrapped__",
                     timeline_edit_actions.timeline_item_takes)

        with mock.patch.object(timeline_edit_actions, "_get_item",
                               return_value=(None, item, None)):
            missing = fn("get_by_index", {})
            supplied = fn("get_by_index", {"index": 1})

        error = missing["error"]
        self.assertEqual("MISSING_PARAM", error["code"])
        self.assertEqual("invalid_input", error["category"])
        self.assertEqual("index", error["state"]["missing_param"])
        # Not merely "some error": with the param supplied the same call works.
        self.assertNotIn("error", supplied)


if __name__ == "__main__":
    unittest.main()
