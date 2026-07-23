"""Regression tests for media_pool folder-by-id resolution (delete_folders /
move_folders).

Before the fix, both actions matched folder_ids only against the ROOT's direct
children: a nested folder id was silently skipped, so a mixed id list produced
a partial delete/move that still reported success. Now `_resolve_folder_ids`
walks the whole tree and any unmatched id is an error BEFORE mutation.
"""
import unittest
from unittest import mock

import src.server as s
import src.domains.media_pool_ingest.actions as _dom_media_pool_ingest


class FakeFolder:
    def __init__(self, name, uid, subfolders=None, clips=None):
        self._name = name
        self._uid = uid
        self._subfolders = list(subfolders or [])
        self._clips = list(clips or [])

    def GetName(self):
        return self._name

    def GetUniqueId(self):
        return self._uid

    def GetSubFolderList(self):
        return list(self._subfolders)

    def GetClipList(self):
        return list(self._clips)


def _tree():
    """root -> A(a1) -> Nested(n1); root -> B(b1)."""
    nested = FakeFolder("Nested", "n1")
    a = FakeFolder("A", "a1", subfolders=[nested])
    b = FakeFolder("B", "b1")
    root = FakeFolder("Master", "root", subfolders=[a, b])
    return root, a, b, nested


class ResolveFolderIdsTest(unittest.TestCase):
    def test_finds_top_level_and_nested(self):
        root, a, b, nested = _tree()
        folders, missing = s._resolve_folder_ids(root, ["a1", "n1", "b1"])
        self.assertEqual(missing, [])
        self.assertEqual([f.GetUniqueId() for f in folders], ["a1", "n1", "b1"])

    def test_reports_missing_ids(self):
        root, *_ = _tree()
        folders, missing = s._resolve_folder_ids(root, ["a1", "ghost"])
        self.assertEqual(missing, ["ghost"])
        self.assertEqual([f.GetUniqueId() for f in folders], ["a1"])

    def test_deeply_nested(self):
        deep = FakeFolder("Deep", "d1")
        mid = FakeFolder("Mid", "m1", subfolders=[deep])
        top = FakeFolder("Top", "t1", subfolders=[mid])
        root = FakeFolder("Master", "root", subfolders=[top])
        self.assertIs(s._find_folder_by_id(root, "d1"), deep)
        self.assertIsNone(s._find_folder_by_id(root, "zzz"))


class MediaPoolFolderActionsTest(unittest.TestCase):
    """Dispatch-level: nested ids resolve; missing ids block the mutation."""

    def _run(self, action, params, root):
        mp = mock.Mock()
        mp.GetRootFolder.return_value = root
        mp.DeleteFolders.return_value = True
        mp.MoveFolders.return_value = True
        with mock.patch.object(_dom_media_pool_ingest, "_get_mp", return_value=(mock.Mock(), mock.Mock(), mp, None)), \
             mock.patch.object(_dom_media_pool_ingest, "_confirm_token_required", return_value=False), \
             mock.patch.object(_dom_media_pool_ingest, "_consume_confirm_token", return_value=None):
            return mp, s.media_pool(action, params)

    def test_delete_folders_resolves_nested_id(self):
        root, a, b, nested = _tree()
        mp, out = self._run("delete_folders", {"folder_ids": ["n1"]}, root)
        self.assertTrue(out.get("success"))
        (deleted,), _ = mp.DeleteFolders.call_args
        self.assertEqual([f.GetUniqueId() for f in deleted], ["n1"])

    def test_delete_folders_missing_id_blocks_all(self):
        root, *_ = _tree()
        mp, out = self._run("delete_folders", {"folder_ids": ["a1", "ghost"]}, root)
        self.assertIn("error", out)
        self.assertIn("ghost", out["error"]["message"])
        self.assertEqual(out["error"]["state"]["missing"], ["ghost"])
        mp.DeleteFolders.assert_not_called()

    def test_move_folders_resolves_nested_id(self):
        root, a, b, nested = _tree()
        mp, out = self._run(
            "move_folders", {"folder_ids": ["n1"], "target_path": "Master/B"}, root)
        self.assertTrue(out.get("success"))
        (moved, target), _ = mp.MoveFolders.call_args
        self.assertEqual([f.GetUniqueId() for f in moved], ["n1"])
        self.assertEqual(target.GetUniqueId(), "b1")

    def test_move_folders_missing_id_blocks_all(self):
        root, *_ = _tree()
        mp, out = self._run(
            "move_folders", {"folder_ids": ["ghost"], "target_path": "Master/B"}, root)
        self.assertIn("error", out)
        mp.MoveFolders.assert_not_called()

    def test_unknown_action_lists_rename_folder(self):
        root, *_ = _tree()
        _, out = self._run("definitely_not_an_action", {}, root)
        self.assertIn("rename_folder", out["error"]["message"])


if __name__ == "__main__":
    unittest.main()
