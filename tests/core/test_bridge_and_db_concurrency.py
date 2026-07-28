"""#141 findings 8 & 9: two ways two threads could collide.

Finding 8 — `background_jobs._run` wrapped only `resolve_busy.long_resolve_op`,
which is an ADVISORY registration, not a mutex. Its one enforcement point,
`wait_until_free()`, is called from `envelope._check()`, which 32 `get_resolve()`
call sites across the compound domains never reach — and the granular server and
the dashboard process never consult the sidecar at all. So `background=true`
plus any of those put two threads inside fusionscript at once, which per
`src/core/resolve_busy.py` "simply hangs with no feedback".

Finding 9 — `timeline_brain_db.connect()` cached ONE connection per database and
handed it to every thread. Writers were serialized by `_WRITE_LOCKS`, but a read
on another thread used that same connection, so it executed inside whatever
`BEGIN IMMEDIATE` was open and could observe uncommitted rows.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest

from src.core import background_jobs, timeline_brain_db
from src.core.live_connection import _bridge_lock


class BackgroundJobBridgeLockTest(unittest.TestCase):
    def test_a_job_body_runs_holding_the_bridge_lock(self):
        seen = {}

        def body():
            seen["held"] = _bridge_lock.locked()
            return "done"

        job_id = background_jobs.start_job("test.job", body)
        for _ in range(200):
            if background_jobs.job_status(job_id).get("status") != "running":
                break
            time.sleep(0.01)
        status = background_jobs.job_status(job_id)
        self.assertEqual("done", status["status"], status)
        self.assertTrue(seen.get("held"),
                        "the job body must hold _bridge_lock, not merely the "
                        "advisory long_resolve_op record")
        self.assertFalse(_bridge_lock.locked(), "the lock must be released after")

    def test_a_job_cannot_run_while_a_sync_body_holds_the_bridge(self):
        # The collision the finding describes: a sync tool body is inside the
        # bridge (the threaded dispatch wrapper holds _bridge_lock) while a
        # background job tries to enter it.
        entered = threading.Event()
        release = threading.Event()

        def body():
            entered.set()
            return "done"

        with _bridge_lock:
            job_id = background_jobs.start_job("test.blocked", body)
            # Give the worker a real chance to run; it must NOT get in.
            self.assertFalse(entered.wait(timeout=0.5),
                             "job entered the bridge while a sync body held it")
        release.set()

        for _ in range(200):
            if background_jobs.job_status(job_id).get("status") != "running":
                break
            time.sleep(0.01)
        self.assertEqual("done", background_jobs.job_status(job_id)["status"],
                         "the job must proceed once the lock is released")

    def test_a_failing_job_still_releases_the_lock(self):
        def body():
            raise RuntimeError("boom")

        job_id = background_jobs.start_job("test.failing", body)
        for _ in range(200):
            if background_jobs.job_status(job_id).get("status") != "running":
                break
            time.sleep(0.01)
        self.assertEqual("error", background_jobs.job_status(job_id)["status"])
        self.assertFalse(_bridge_lock.locked())


class BrainDbPerThreadConnectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_root = os.path.join(self.tmp.name, "Example_Project")
        os.makedirs(self.project_root)
        self.addCleanup(timeline_brain_db.close_all)

    def test_each_thread_gets_its_own_connection(self):
        main_conn = timeline_brain_db.connect(self.project_root)
        other = {}

        def worker():
            other["conn"] = timeline_brain_db.connect(self.project_root)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertIsNot(main_conn, other["conn"],
                         "a shared connection is what let a reader land inside "
                         "another thread's open transaction")
        # Same thread still reuses its own.
        self.assertIs(main_conn, timeline_brain_db.connect(self.project_root))

    def test_a_reader_does_not_see_a_writers_uncommitted_rows(self):
        timeline_brain_db.connect(self.project_root)
        observed = {}
        inside_txn = threading.Event()
        reader_done = threading.Event()

        def reader():
            inside_txn.wait(timeout=5)
            conn = timeline_brain_db.connect(self.project_root)
            observed["count"] = conn.execute(
                "SELECT COUNT(*) AS c FROM brain_edits").fetchone()["c"]
            reader_done.set()

        thread = threading.Thread(target=reader)
        thread.start()
        with timeline_brain_db.transaction(self.project_root) as txn:
            txn.execute(
                "INSERT INTO brain_edits(analysis_run_id, edit_type, tool_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("run_x", "timeline.delete_clips", "timeline", "2026-07-28T00:00:00Z"))
            inside_txn.set()
            self.assertTrue(reader_done.wait(timeout=5), "reader blocked")
        thread.join()

        self.assertEqual(0, observed["count"],
                         "the reader saw a row from a transaction that had not "
                         "committed yet")
        conn = timeline_brain_db.connect(self.project_root)
        self.assertEqual(
            1, conn.execute("SELECT COUNT(*) AS c FROM brain_edits").fetchone()["c"],
            "and it must see the row once committed")

    def test_close_all_reaches_connections_opened_on_other_threads(self):
        opened = {}

        def worker():
            opened["conn"] = timeline_brain_db.connect(self.project_root)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        timeline_brain_db.close_all()
        with self.assertRaises(sqlite3.ProgrammingError):
            opened["conn"].execute("SELECT 1")

    def test_concurrent_writers_are_still_serialized(self):
        errors = []

        def writer(n):
            try:
                for i in range(5):
                    with timeline_brain_db.transaction(self.project_root) as txn:
                        txn.execute(
                            "INSERT INTO brain_edits(analysis_run_id, edit_type, tool_name, created_at) "
                            "VALUES (?, ?, ?, ?)",
                            (f"run_{n}_{i}", "t", "timeline", "2026-07-28T00:00:00Z"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([], errors)
        conn = timeline_brain_db.connect(self.project_root)
        self.assertEqual(
            20, conn.execute("SELECT COUNT(*) AS c FROM brain_edits").fetchone()["c"])


if __name__ == "__main__":
    unittest.main()
