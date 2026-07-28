"""#141 minor bucket — five small defects, each with its own failure mode.

1. Unreachable dispatch branches in the `timeline` tool (dead code: identical
   bodies already handled before the `tl` gate).
2. `GetTrackCount()` unguarded inside `range(1, ... + 1)` — `None + 1` raises
   TypeError, while neighbouring sites use `or 0`.
3. Unguarded `int(p.get(...))` / `float(p.get(...))` — non-numeric caller input
   escapes as a raw traceback rather than an `invalid_input` envelope.
4. `readback._STATS` incremented without a lock.
5. `get_all_media_pool_folders` recursing into a `None` root folder.
"""

from __future__ import annotations

import ast
import collections
import os
import threading
import unittest

from src import server  # noqa: F401 - import first (circular-import guard)
from src.core import readback
from src.core.params import InvalidParam, MissingParam, as_float, as_int
from src.granular.common import get_all_media_pool_folders
from tests.bridge_double import ResolveBridgeDouble

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DeadDispatchBranchTest(unittest.TestCase):
    def test_the_timeline_tool_has_no_unreachable_duplicate_branches(self):
        path = os.path.join(_ROOT, "src", "domains", "timeline_edit", "actions.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        offenders = {}
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            seen = collections.defaultdict(list)
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Compare)
                        and isinstance(node.left, ast.Name)
                        and node.left.id == "action"):
                    continue
                # A branch guarded by extra conditions is a deliberate
                # specialisation, not a duplicate — `compare_timelines` has a
                # snapshot-guarded early form and a general late one.
                parent_guarded = any(
                    isinstance(p, ast.BoolOp) and node in ast.walk(p)
                    for p in ast.walk(fn)
                )
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                        seen[comparator.value].append((node.lineno, parent_guarded))
            for action, hits in seen.items():
                if len(hits) > 1 and not any(guarded for _line, guarded in hits):
                    offenders[f"{fn.name}:{action}"] = [line for line, _g in hits]

        self.assertEqual({}, offenders,
                         f"unreachable duplicate dispatch branches: {offenders}")


class TrackCountGuardTest(unittest.TestCase):
    def test_no_range_call_dereferences_an_unguarded_track_count(self):
        offenders = []
        for domain_dir, _dirs, files in os.walk(os.path.join(_ROOT, "src")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(domain_dir, name)
                with open(path, encoding="utf-8") as handle:
                    try:
                        tree = ast.parse(handle.read())
                    except SyntaxError:  # pragma: no cover
                        continue
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "range"):
                        continue
                    for arg in node.args:
                        # range(1, X + 1) where X is a bare GetTrackCount call
                        if not (isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add)):
                            continue
                        left = arg.left
                        if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                                and left.func.attr == "GetTrackCount"):
                            offenders.append(
                                f"{os.path.relpath(path, _ROOT)}:{node.lineno}")
        self.assertEqual(
            [], offenders,
            "GetTrackCount() can return None and `None + 1` is a TypeError; "
            "use `(tl.GetTrackCount(tt) or 0) + 1`:\n  " + "\n  ".join(offenders),
        )


class NumericParamCoercionTest(unittest.TestCase):
    def test_a_valid_value_coerces(self):
        self.assertEqual(3, as_int({"index": "3"}, "index"))
        self.assertEqual(3, as_int({"index": 3.7}, "index"))
        self.assertAlmostEqual(1.5, as_float({"timeout": "1.5"}, "timeout"))

    def test_a_missing_value_uses_the_default(self):
        self.assertEqual(7, as_int({}, "index", 7))
        self.assertEqual(7, as_int({"index": None}, "index", 7))

    def test_a_missing_value_with_no_default_is_a_missing_param(self):
        with self.assertRaises(MissingParam):
            as_int({}, "index")

    def test_non_numeric_input_is_typed_not_a_bare_valueerror(self):
        for bad in ("V1", [1], {"a": 1}, object()):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidParam) as caught:
                    as_int({"index": bad}, "index")
                self.assertEqual("index", caught.exception.key)
                self.assertIn("an integer", str(caught.exception))

    def test_a_bool_is_refused_rather_than_read_as_0_or_1(self):
        with self.assertRaises(InvalidParam):
            as_int({"track_index": True}, "track_index")

    def test_invalidparam_is_still_a_valueerror(self):
        self.assertTrue(issubclass(InvalidParam, ValueError))

    def test_no_unguarded_numeric_coercion_of_caller_input_remains(self):
        offenders = []
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
                for fn in [n for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                    # A site already inside a try has its own handling.
                    if any(isinstance(n, ast.Try) for n in ast.walk(fn)):
                        continue
                    for node in ast.walk(fn):
                        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                and node.func.id in ("int", "float") and node.args):
                            continue
                        arg = node.args[0]
                        if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                                and arg.func.attr == "get"
                                and isinstance(arg.func.value, ast.Name)
                                and arg.func.value.id in ("p", "params")):
                            offenders.append(
                                f"{os.path.relpath(path, _ROOT)}:{node.lineno}")
        self.assertEqual(
            [], offenders,
            "use params.as_int/as_float so bad input is an envelope:\n  "
            + "\n  ".join(offenders),
        )


class ReadbackStatsLockTest(unittest.TestCase):
    def test_concurrent_verifications_do_not_lose_a_count(self):
        readback.reset_verification_stats()
        self.addCleanup(readback.reset_verification_stats)

        def worker():
            for _ in range(200):
                readback.verify_by_readback(
                    mutate=lambda: True,
                    observe=lambda: True,
                )

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stats = readback.verification_stats()
        self.assertEqual(800, stats["total"])
        self.assertEqual(
            stats["total"],
            stats["verified"] + stats["contradicted"] + stats["unverified"],
            "the buckets must sum to the total",
        )

    def test_the_stats_lock_exists(self):
        self.assertIsNotNone(getattr(readback, "_STATS_LOCK", None))


class MediaPoolFolderGuardTest(unittest.TestCase):
    def test_a_none_root_folder_yields_an_empty_list(self):
        media_pool = ResolveBridgeDouble(methods={"GetRootFolder": None})
        self.assertEqual([], get_all_media_pool_folders(media_pool))

    def test_a_none_media_pool_yields_an_empty_list(self):
        self.assertEqual([], get_all_media_pool_folders(None))

    def test_a_none_subfolder_list_does_not_break_the_walk(self):
        root = ResolveBridgeDouble(methods={"GetSubFolderList": None, "GetName": "Master"})
        media_pool = ResolveBridgeDouble(methods={"GetRootFolder": root})
        self.assertEqual([root], get_all_media_pool_folders(media_pool))

    def test_nested_folders_are_still_walked(self):
        leaf = ResolveBridgeDouble(methods={"GetSubFolderList": [], "GetName": "Leaf"})
        root = ResolveBridgeDouble(methods={"GetSubFolderList": [leaf], "GetName": "Master"})
        media_pool = ResolveBridgeDouble(methods={"GetRootFolder": root})
        self.assertEqual([root, leaf], get_all_media_pool_folders(media_pool))


if __name__ == "__main__":
    unittest.main()
