"""Static guards over the destructive-action registries.

Two directions, because each catches a different class of bug:

SOUNDNESS (original) — every registry / token-gated action string must be a REAL
handler. The EX2 bug: DESTRUCTIVE_ACTIONS_BY_TOOL["media_pool"] listed granular
function names (delete_media_pool_clips, …) that the compound media_pool tool
never dispatches, so is_destructive() returned False and catastrophic deletes
silently skipped version-on-mutate archiving.

COMPLETENESS (#110 finding 14) — the soundness guard says nothing about actions
that destroy user data and are in NO registry, which is exactly how #110's
findings 2 (`project_manager delete`) and 3 (`render build_proxies` wiping the
render queue) shipped green. A destructive-*name* heuristic would not have caught
`build_proxies` either. So this file also pins the real invariant: the handful of
Resolve API methods that irreversibly destroy data outside the current timeline
may be called ONLY from an allowlist of reviewed sites. A new call anywhere else
— the exact shape of finding 3 — fails here.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from src.core.destructive_hook import DESTRUCTIVE_ACTIONS_BY_TOOL

import src.server as s

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "src" / "server.py"
# Domain tool functions moved out of server.py in the restructure epic (#52,
# Phase 3 / #46); @_destructive_op-decorated tools now live across these too.
DOMAIN_ACTION_FILES = sorted((ROOT / "src" / "domains").glob("*/actions.py"))


def _implemented_actions(fn):
    """Actions a tool function handles: `action ==` branches plus the actions it
    advertises in its _unknown(action, [...]) list (which includes actions it
    dispatches via delegated helpers)."""
    found = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "action":
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    found.add(comp.value)
                elif isinstance(comp, (ast.Set, ast.List, ast.Tuple)):
                    found.update(
                        elt.value for elt in comp.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_unknown":
            for arg in node.args:
                if isinstance(arg, (ast.List, ast.Tuple)):
                    found.update(
                        elt.value for elt in arg.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    )
    return found


def _all_tool_actions():
    """Map every compound tool function -> its implemented action strings.

    Unlike _destructive_op_tools() this does not require the @_destructive_op
    wrapper: a token-gated action is gated by src/server.py's dispatcher, not by
    the archiving decorator, so `folder`, `media_pool_item` and `project_settings`
    are legitimately gated without being wrapped.
    """
    out = {}
    for path in [SERVER] + DOMAIN_ACTION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args.args
            if not args or args[0].arg != "action":
                continue
            out.setdefault(node.name, set()).update(_implemented_actions(node))
    return out


def _destructive_op_tools():
    """Map @_destructive_op("tool") -> set of implemented action strings."""
    out = {}
    for path in [SERVER] + DOMAIN_ACTION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Name)
                    and dec.func.id == "_destructive_op"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                ):
                    out[dec.args[0].value] = _implemented_actions(node)
    return out


class RegistryDriftTest(unittest.TestCase):
    def setUp(self):
        self.tools = _destructive_op_tools()
        self.assertIn("media_pool", self.tools, "expected @_destructive_op('media_pool')")

    def test_registry_actions_are_real_handlers(self):
        # EX-REG: every registry action must be a real handler for its tool (impl
        # `action ==` branch or advertised in the tool's _unknown list). Tools not
        # wrapped with @_destructive_op have inert entries; none should remain.
        for tool, actions in DESTRUCTIVE_ACTIONS_BY_TOOL.items():
            impl = self.tools.get(tool)
            self.assertIsNotNone(
                impl, f"DESTRUCTIVE_ACTIONS_BY_TOOL has tool {tool!r} that is not @_destructive_op-wrapped"
            )
            for action in actions:
                self.assertIn(
                    action, impl,
                    f"DESTRUCTIVE_ACTIONS_BY_TOOL[{tool!r}] lists {action!r}, but it is not a "
                    f"real action of the {tool} tool (registry drift — governance would not fire).",
                )

    def test_token_gated_actions_are_real_handlers(self):
        # #110 finding 14: this used to `continue` when the tool was not
        # @_destructive_op-wrapped, silently validating nothing for 3 of the 17
        # pairs (folder/media_pool_item remove_motion_blur, project_settings
        # generate_speech). Token gating lives in the server dispatcher, so
        # every tool function counts, decorated or not.
        every_tool = _all_tool_actions()
        for tool, action in s._TOKEN_GATED_DESTRUCTIVE_ACTIONS:
            impl = every_tool.get(tool)
            self.assertIsNotNone(
                impl,
                f"_TOKEN_GATED_DESTRUCTIVE_ACTIONS has ({tool!r}, {action!r}) but there is "
                f"no {tool!r} tool function — the gate can never fire.",
            )
            self.assertIn(
                action, impl,
                f"_TOKEN_GATED_DESTRUCTIVE_ACTIONS has ({tool!r}, {action!r}) "
                f"but it is not a real handler in the {tool} tool.",
            )

    def test_media_pool_catastrophic_deletes_registered_and_gated(self):
        # Lock in EX2/EX3 specifically.
        mp = DESTRUCTIVE_ACTIONS_BY_TOOL["media_pool"]
        for action in ("delete_clips", "delete_folders", "delete_timelines"):
            self.assertIn(action, mp)
            self.assertIn(("media_pool", action), s._TOKEN_GATED_DESTRUCTIVE_ACTIONS)


# ── COMPLETENESS: catastrophic API sinks may only be called from reviewed sites ──
#
# Method name -> allowed receiver variable names (None = any receiver). These are
# the Resolve API calls that destroy user data OUTSIDE the current timeline, so a
# version-on-mutate archive cannot bring it back. Timeline-scoped deletes
# (tl.DeleteClips) are excluded on purpose: those are archived.
CATASTROPHIC_SINKS = {
    "DeleteAllRenderJobs": None,   # wipes the user's whole configured render queue
    "DeleteProject": None,         # removes a project from the database
    "DeleteFolders": None,         # media-pool bins
    "DeleteTimelines": None,       # timelines, from the pool
    "DeleteClips": {"mp", "media_pool", "pool"},  # POOL clips (source media), not timeline items
}

# "<path>::<enclosing function>" -> why this site is allowed to hold the knife.
# Adding an entry is a deliberate review step; that is the whole point.
CATASTROPHIC_SINK_ALLOWLIST = {
    "src/granular/media_pool.py::delete_timelines_by_id":
        "granular surface: the delete IS the user's named request",
    "src/granular/media_pool.py::delete_media_pool_clips":
        "granular surface: the delete IS the user's named request",
    "src/granular/media_pool.py::delete_media_pool_folders":
        "granular surface: the delete IS the user's named request",
    "src/domains/media_pool_ingest/actions.py::media_pool":
        "media_pool delete_clips/delete_folders/delete_timelines — registry + confirm token",
    "src/domains/media_pool_ingest/actions.py::_mp_rename_folder_live":
        "rename fallback: deletes only the temp bin it just created",
    "src/domains/orchestration/actions.py::_orchestrate_gc_snapshots_live":
        "garbage-collects only orchestrate's own snapshot timelines",
    "src/domains/orchestration/actions.py::_orchestrate_restore_snapshot":
        "replaces the working timeline with the snapshot the user asked to restore",
    "src/domains/project_lifecycle/utils/project_cleanup.py::delete_project_safely":
        "the one project-delete sink; callers gate it (#110 finding 2)",
    "src/domains/render_deliver/actions.py::_build_proxies":
        "deletes only the _proxy_build_* timeline it created (NOT the render queue — #110 finding 3)",
    "src/domains/render_deliver/actions.py::render":
        "render delete_all_jobs — the user's named action",
    "src/domains/timeline_conform_interchange/actions.py::_probe_interchange_roundtrip":
        "probe cleans up the timeline it imported",
    "src/core/timeline_versioning.py::prune_archived_versions":
        "prunes archived versions the versioning system itself created",
}


def _sink_call_sites():
    """All catastrophic-sink call sites in src/, as {"<path>::<fn>": [details]}."""
    sites = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack = []

        def walk(node, stack=stack, rel=rel):
            is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_fn:
                stack.append(node.name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                if attr in CATASTROPHIC_SINKS:
                    value = node.func.value
                    recv = value.id if isinstance(value, ast.Name) else (
                        value.attr if isinstance(value, ast.Attribute) else "?")
                    allowed_recv = CATASTROPHIC_SINKS[attr]
                    if allowed_recv is None or recv in allowed_recv:
                        key = f"{rel}::{'.'.join(stack) or '<module>'}"
                        sites.setdefault(key, []).append(f"{recv}.{attr}() line {node.lineno}")
            for child in ast.iter_child_nodes(node):
                walk(child)
            if is_fn:
                stack.pop()

        walk(tree)
    return sites


class CatastrophicSinkCoverageTest(unittest.TestCase):
    """#110 finding 14 — the completeness half the drift guard never had."""

    def test_no_unreviewed_catastrophic_sink_call_sites(self):
        sites = _sink_call_sites()
        unreviewed = {k: v for k, v in sites.items() if k not in CATASTROPHIC_SINK_ALLOWLIST}
        self.assertEqual(
            unreviewed, {},
            "New call site(s) for a catastrophic Resolve API sink. This is the shape of "
            "#110 finding 3 (render build_proxies silently wiped the user's render queue). "
            "Either gate the action (registry entry + confirm token) and add the site to "
            "CATASTROPHIC_SINK_ALLOWLIST with a one-line reason, or do not call the sink.",
        )

    def test_allowlist_has_no_stale_entries(self):
        sites = _sink_call_sites()
        stale = sorted(set(CATASTROPHIC_SINK_ALLOWLIST) - set(sites))
        self.assertEqual(
            stale, [],
            "CATASTROPHIC_SINK_ALLOWLIST entries no longer call any sink — prune them so "
            "the allowlist keeps meaning something.",
        )

    def test_project_delete_is_gated(self):
        # Finding 2: project_manager(action="delete") was in neither registry.
        self.assertIn(
            ("project_manager", "delete"), s._TOKEN_GATED_DESTRUCTIVE_ACTIONS,
            "project_manager delete must require a confirm token — it destroys a project "
            "irrecoverably (#110 finding 2).",
        )


class DestructiveGuardsAreNotVacuousTest(unittest.TestCase):
    """Both guards above are fed their own drift (#121 task 2).

    These two are the last line between a mis-registered action and an
    unarchived, unconfirmed delete of the user's media — #110 findings 2 and 3
    shipped precisely because nothing here fired. A guard in that position that
    silently stopped detecting would be worse than not having it, so the drift
    is reintroduced here and the failure asserted.
    """

    def test_registry_soundness_guard_detects_a_phantom_action(self):
        # The EX2 shape: a registry entry naming something the tool never dispatches.
        drifted = dict(DESTRUCTIVE_ACTIONS_BY_TOOL)
        drifted["media_pool"] = tuple(drifted["media_pool"]) + ("delete_everything_now",)
        with mock.patch.dict(DESTRUCTIVE_ACTIONS_BY_TOOL, drifted, clear=True):
            with self.assertRaises(AssertionError) as caught:
                RegistryDriftTest("test_registry_actions_are_real_handlers").debug()
        self.assertIn("delete_everything_now", str(caught.exception))

    def test_registry_soundness_guard_detects_an_undecorated_tool(self):
        drifted = dict(DESTRUCTIVE_ACTIONS_BY_TOOL)
        drifted["not_a_tool_at_all"] = ("delete_clips",)
        with mock.patch.dict(DESTRUCTIVE_ACTIONS_BY_TOOL, drifted, clear=True):
            with self.assertRaises(AssertionError) as caught:
                RegistryDriftTest("test_registry_actions_are_real_handlers").debug()
        self.assertIn("not_a_tool_at_all", str(caught.exception))

    def test_token_gate_guard_detects_a_gate_that_can_never_fire(self):
        drifted = list(s._TOKEN_GATED_DESTRUCTIVE_ACTIONS) + [("media_pool", "no_such_action")]
        with mock.patch.object(s, "_TOKEN_GATED_DESTRUCTIVE_ACTIONS", drifted):
            with self.assertRaises(AssertionError) as caught:
                RegistryDriftTest("test_token_gated_actions_are_real_handlers").debug()
        self.assertIn("no_such_action", str(caught.exception))

    def _sink_tree(self, body: str):
        """A temp repo root holding one src/ file, for the sink scanner."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "src").mkdir()
        (root / "src" / "synthetic_sink.py").write_text(textwrap.dedent(body), encoding="utf-8")
        return root

    def test_sink_guard_detects_a_new_unreviewed_call_site(self):
        # #110 finding 3 exactly: a fresh DeleteAllRenderJobs() somewhere new.
        root = self._sink_tree(
            """
            def some_new_helper(project):
                project.DeleteAllRenderJobs()
            """
        )
        with mock.patch.object(sys.modules[__name__], "ROOT", root):
            with self.assertRaises(AssertionError) as caught:
                CatastrophicSinkCoverageTest("test_no_unreviewed_catastrophic_sink_call_sites").debug()
        message = str(caught.exception)
        self.assertIn("src/synthetic_sink.py::some_new_helper", message)
        self.assertIn("DeleteAllRenderJobs", message)

    def test_sink_guard_ignores_timeline_scoped_deletes(self):
        # tl.DeleteClips() is archived by version-on-mutate, so it must NOT trip
        # the guard. A guard that flagged it would be retuned into uselessness.
        root = self._sink_tree(
            """
            def trim(tl, items):
                tl.DeleteClips(items)
            """
        )
        with mock.patch.object(sys.modules[__name__], "ROOT", root):
            with mock.patch.dict(CATASTROPHIC_SINK_ALLOWLIST, {}, clear=True):
                CatastrophicSinkCoverageTest(
                    "test_no_unreviewed_catastrophic_sink_call_sites"
                ).debug()

    def test_sink_guard_detects_a_stale_allowlist_entry(self):
        root = self._sink_tree("def nothing():\n    return None\n")
        with mock.patch.object(sys.modules[__name__], "ROOT", root):
            with self.assertRaises(AssertionError) as caught:
                CatastrophicSinkCoverageTest("test_allowlist_has_no_stale_entries").debug()
        self.assertIn("no longer call any sink", str(caught.exception))

    def test_the_real_sink_scan_is_not_empty(self):
        sites = _sink_call_sites()
        self.assertGreaterEqual(
            len(sites), len(CATASTROPHIC_SINK_ALLOWLIST),
            "sink scanner found fewer sites than the allowlist has entries — scan broken?",
        )


if __name__ == "__main__":
    unittest.main()
