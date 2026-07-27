"""#143 finding 4: the .drt export scratch dirs must not leak.

`_advanced_timeline_edit` (every trim / move / split / place_transition),
`_timeline_list_transitions_impl` and auto-edit's polish step each
`tempfile.mkdtemp()` a scratch dir and write a whole timeline export into it.
Nothing removed them, on success or on failure. On a tmpfs /tmp (systemd's
default) a long editing session exhausts RAM-backed temp space and every later
export starts failing.

These tests drive the early-return paths — the ones that leaked most readily,
since they bail before the round trip even starts — and assert the directory is
gone whichever way the call exits.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from src import server as _server  # noqa: F401 - import first: the domain
# action modules import back from src.server, so importing them directly leaves
# a partially-initialized module and a circular-import error.
from src.domains.auto_edit import actions as auto_edit_actions
from src.domains.timeline_edit import actions as timeline_edit_actions


# Bound before any patching: the modules under test reference the shared
# tempfile module, so patching `module.tempfile.mkdtemp` replaces it globally and
# the watcher would otherwise call itself.
_REAL_MKDTEMP = tempfile.mkdtemp


class _ScratchWatcher:
    """Redirects mkdtemp into a sandbox and records every dir it hands out."""

    def __init__(self, sandbox: str) -> None:
        self.sandbox = sandbox
        self.created: list[str] = []

    def mkdtemp(self, *args, **kwargs):
        kwargs["dir"] = self.sandbox
        path = _REAL_MKDTEMP(*args, **kwargs)
        self.created.append(path)
        return path

    def assert_all_removed(self, case: unittest.TestCase) -> None:
        case.assertTrue(self.created, "expected a scratch dir to have been created")
        leaked = [p for p in self.created if os.path.exists(p)]
        case.assertEqual(leaked, [], f"scratch dirs leaked: {leaked}")


class _TimelineStub:
    def GetName(self):
        return "Example Timeline"


class ScratchDirCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.watcher = _ScratchWatcher(self.tmp.name)

    def test_list_transitions_removes_its_scratch_on_export_failure(self) -> None:
        conform = mock.MagicMock()
        conform._export_timeline_checked.return_value = {"success": False}
        with mock.patch.object(timeline_edit_actions._advanced_bridge,
                               "node_available", return_value=True), \
             mock.patch.object(timeline_edit_actions.tempfile, "mkdtemp",
                               side_effect=self.watcher.mkdtemp), \
             mock.patch.dict(
                 "sys.modules",
                 {"src.domains.timeline_conform_interchange.actions": conform}):
            result = timeline_edit_actions._timeline_list_transitions_impl(
                object(), _TimelineStub(), {})
        self.assertFalse(result.get("success"))
        self.watcher.assert_all_removed(self)

    def test_list_transitions_removes_its_scratch_on_an_unexpected_raise(self) -> None:
        conform = mock.MagicMock()
        conform._export_timeline_checked.side_effect = RuntimeError("bridge blew up")
        with mock.patch.object(timeline_edit_actions._advanced_bridge,
                               "node_available", return_value=True), \
             mock.patch.object(timeline_edit_actions.tempfile, "mkdtemp",
                               side_effect=self.watcher.mkdtemp), \
             mock.patch.dict(
                 "sys.modules",
                 {"src.domains.timeline_conform_interchange.actions": conform}):
            with self.assertRaises(RuntimeError):
                timeline_edit_actions._timeline_list_transitions_impl(
                    object(), _TimelineStub(), {})
        self.watcher.assert_all_removed(self)

    def test_advanced_timeline_edit_removes_its_scratch_on_export_failure(self) -> None:
        conform = mock.MagicMock()
        conform._export_timeline_checked.return_value = {"success": False}
        conform._timeline_media_coverage.return_value = {"linked": 0}
        with mock.patch.object(timeline_edit_actions._advanced_bridge,
                               "node_available", return_value=True), \
             mock.patch.object(timeline_edit_actions, "_confirm_token_required",
                               return_value=False), \
             mock.patch.object(timeline_edit_actions, "_consume_confirm_token",
                               return_value=None), \
             mock.patch.object(timeline_edit_actions.tempfile, "mkdtemp",
                               side_effect=self.watcher.mkdtemp), \
             mock.patch.dict(
                 "sys.modules",
                 {"src.domains.timeline_conform_interchange.actions": conform}):
            result = timeline_edit_actions._advanced_timeline_edit(
                object(), _TimelineStub(), {},
                action_name="trim_clip", ops=[], warning="w")
        self.assertFalse(result.get("success"))
        self.watcher.assert_all_removed(self)


class AutoEditPolishScratchCleanup(unittest.TestCase):
    def test_polish_removes_its_scratch_on_export_failure(self) -> None:
        # auto_edit's polish step shares the export -> op-chain -> reimport shape
        # and leaked the same way; assert the module now imports shutil and that
        # its mkdtemp call is inside a try/finally that removes the dir.
        import ast
        import inspect

        source = inspect.getsource(auto_edit_actions)
        tree = ast.parse(source)
        cleaned = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.finalbody:
                text = ast.dump(handler)
                if "rmtree" in text and "scratch" in text:
                    cleaned = True
        self.assertTrue(
            cleaned,
            "auto_edit polish scratch dir is not removed in a finally block",
        )


if __name__ == "__main__":
    unittest.main()
