"""Concurrent writers must not lose an edit or splice a state file.

Two defects of one shape, both measured before the fix:

1. **Lost updates.** `_v2_update_field` (the corrections writer behind POST
   /api/clips/<id>/corrections) and `write_panel_state(merge=True)` (behind POST
   /api/panel_state) are read-modify-writes, and neither route takes a lock —
   unlike its `/transcript/regenerate` sibling, which takes `state.lock` for
   exactly this reason. The panel is a `ThreadingHTTPServer`, so concurrent
   saves interleaved and the last writer dropped the others. 24 concurrent
   corrections used to leave 9.

2. **Spliced files.** Every writer built its temp file at the *shared* name
   ``path + ".tmp"``, which defeats the atomicity `os.replace` is there to
   provide: two writers open the same temp file, the second truncates the
   first's buffer mid-write, and `os.replace` publishes the result. 1 in 8
   trials of 16 concurrent writes produced an unparseable corrections.json —
   and `_v2_read_corrections(strict=True)` then refuses every future correction
   for that clip until a human repairs the file by hand.

These tests are probabilistic by nature: they drive real threads and assert on
the invariant (nothing lost, always parseable) rather than on an interleaving.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

import src.server  # noqa: F401  — import order: server first, domains resolve through it
from src.domains.media_analysis.actions import _v2_update_field
from src.domains.media_analysis.utils.analysis_memory import (
    read_panel_state,
    write_panel_state,
)


def _run_threads(fn, count):
    threads = [threading.Thread(target=fn, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class ConcurrentCorrectionsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="corrections-race-")
        self.clip_dir = os.path.join(self.root, "clips", "clip-a")
        os.makedirs(self.clip_dir)
        with open(os.path.join(self.clip_dir, "analysis.json"), "w", encoding="utf-8") as fh:
            json.dump({"clip_id": "clip-a"}, fh)
        self.path = os.path.join(self.clip_dir, "corrections.json")

    def _correct(self, index, value="v"):
        _v2_update_field(
            self.root,
            {
                "clip_id": "clip-a",
                "clip_dir": self.clip_dir,
                "entity_uuid": f"shot-{index}",
                "field_path": "visual.shot_size",
                "new_value": f"{value}-{index}",
                "author": f"user-{index}",
            },
            entity_type="shot",
        )

    def test_no_concurrent_correction_is_lost(self):
        count = 24
        _run_threads(self._correct, count)
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(count, len(data["changelog"]),
                         "a correction's changelog entry was overwritten by a concurrent writer")
        self.assertEqual(count, len(data["current"]))

    def test_large_concurrent_writes_leave_the_file_parseable(self):
        # Big values widen the write window — this is what used to splice the
        # file through the shared "<path>.tmp" name.
        big = "x" * 100_000
        _run_threads(lambda i: self._correct(i, big), 12)
        with open(self.path, encoding="utf-8") as fh:
            json.load(fh)  # raises JSONDecodeError on a spliced file

    def test_no_temp_files_are_left_behind(self):
        _run_threads(self._correct, 8)
        strays = [name for name in os.listdir(self.clip_dir) if ".tmp" in name]
        self.assertEqual([], strays)


class ConcurrentPanelStateTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="panel-race-")

    def test_a_concurrent_partial_update_is_not_dropped(self):
        count = 20
        _run_threads(lambda i: write_panel_state(self.root, {f"field_{i}": i}), count)
        state = read_panel_state(self.root) or {}
        missing = [i for i in range(count) if state.get(f"field_{i}") != i]
        self.assertEqual([], missing,
                         "merge=True is a read-modify-write; these fields were "
                         "dropped by a concurrent writer")


if __name__ == "__main__":
    unittest.main()
