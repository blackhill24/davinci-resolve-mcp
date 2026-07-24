"""Unit tests for src/core/timeline_brain_db.py (C6).

No Resolve required.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from src.core import timeline_brain_db


class TimelineBrainDB(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="brain_db_test_")
        # `project_root` is the analysis project root; the DB lives under _soul/.
        self.project_root = os.path.join(self.tmp, "project")
        os.makedirs(self.project_root, exist_ok=True)

    def tearDown(self) -> None:
        timeline_brain_db.reset_for_test(self.project_root)
        # Best-effort cleanup.
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_db_file_created_at_expected_path(self) -> None:
        conn = timeline_brain_db.connect(self.project_root)
        self.assertIsNotNone(conn)
        expected = os.path.join(self.project_root, "_soul", "timeline_brain.sqlite")
        self.assertTrue(os.path.isfile(expected), msg=f"missing: {expected}")

    def test_schema_created(self) -> None:
        conn = timeline_brain_db.connect(self.project_root)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        self.assertIn("timeline_versions", names)
        self.assertIn("brain_edits", names)
        self.assertIn("timeline_clip_usage", names)
        self.assertIn("schema_metadata", names)

    def test_schema_version_recorded(self) -> None:
        conn = timeline_brain_db.connect(self.project_root)
        row = conn.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()
        self.assertEqual(row["value"], str(timeline_brain_db.SCHEMA_VERSION))

    def test_insert_version_and_latest_version(self) -> None:
        conn = timeline_brain_db.connect(self.project_root)
        self.assertIsNone(timeline_brain_db.latest_version(conn, "MyEdit"))
        with timeline_brain_db.transaction(self.project_root) as txn:
            txn.execute(
                "INSERT INTO timeline_versions(timeline_name, version, created_at, archived_timeline_name, archived_bin_path) "
                "VALUES (?, ?, ?, ?, ?)",
                ("MyEdit", 1, "2026-05-26T00:00:00Z", "MyEdit_archived_v01", "Master/Archive"),
            )
            txn.execute(
                "INSERT INTO timeline_versions(timeline_name, version, created_at, archived_timeline_name, archived_bin_path) "
                "VALUES (?, ?, ?, ?, ?)",
                ("MyEdit", 2, "2026-05-26T00:01:00Z", "MyEdit_archived_v02", "Master/Archive"),
            )
        self.assertEqual(timeline_brain_db.latest_version(conn, "MyEdit"), 2)
        self.assertIsNone(timeline_brain_db.latest_version(conn, "Different"))

    def test_run_archived_for_run_is_idempotent(self) -> None:
        conn = timeline_brain_db.connect(self.project_root)
        run_id = "run_xyz"
        self.assertFalse(timeline_brain_db.run_archived_for_run(conn, "Edit", run_id))
        with timeline_brain_db.transaction(self.project_root) as txn:
            txn.execute(
                "INSERT INTO timeline_versions(timeline_name, version, created_at, analysis_run_id, archived_timeline_name, archived_bin_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("Edit", 1, "2026-05-26T00:00:00Z", run_id, "Edit_archived_v01", "Master/Archive"),
            )
        self.assertTrue(timeline_brain_db.run_archived_for_run(conn, "Edit", run_id))
        self.assertFalse(
            timeline_brain_db.run_archived_for_run(conn, "Edit", "other_run"),
            msg="A different run should not be marked archived",
        )

    def test_transaction_rolls_back_on_exception(self) -> None:
        conn = timeline_brain_db.connect(self.project_root)
        try:
            with timeline_brain_db.transaction(self.project_root) as txn:
                txn.execute(
                    "INSERT INTO timeline_versions(timeline_name, version, created_at, archived_timeline_name, archived_bin_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("Rollback", 1, "2026-05-26T00:00:00Z", "X", "Master/Archive"),
                )
                raise RuntimeError("simulated")
        except RuntimeError:
            pass
        row = conn.execute(
            "SELECT 1 FROM timeline_versions WHERE timeline_name='Rollback'"
        ).fetchone()
        self.assertIsNone(row, msg="rolled-back transaction left rows behind")


class SchemaMigrations(unittest.TestCase):
    """Verify the migration runner advances older DBs to SCHEMA_VERSION."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="brain_migration_test_")
        self.project_root = os.path.join(self.tmp, "project")
        os.makedirs(self.project_root, exist_ok=True)
        self.saved_migrations = dict(timeline_brain_db._MIGRATIONS)
        self.saved_version = timeline_brain_db.SCHEMA_VERSION

    def tearDown(self) -> None:
        timeline_brain_db.reset_for_test(self.project_root)
        timeline_brain_db._MIGRATIONS.clear()
        timeline_brain_db._MIGRATIONS.update(self.saved_migrations)
        timeline_brain_db.SCHEMA_VERSION = self.saved_version
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_advances_existing_db(self) -> None:
        # First open: lands at current production SCHEMA_VERSION (whatever it is today).
        conn = timeline_brain_db.connect(self.project_root)
        base_version = timeline_brain_db._read_schema_version(conn)
        self.assertEqual(base_version, self.saved_version)
        # Close the connection cache so a "new process" re-opens.
        timeline_brain_db.close_all()
        # Register a synthetic future migration one version above current.
        future = base_version + 1
        applied: list[int] = []

        @timeline_brain_db.register_migration(future)
        def _future(conn: "timeline_brain_db.sqlite3.Connection") -> None:
            if not timeline_brain_db._column_exists(conn, "brain_edits", "test_column"):
                conn.execute("ALTER TABLE brain_edits ADD COLUMN test_column TEXT")
            applied.append(future)

        timeline_brain_db.SCHEMA_VERSION = future
        conn2 = timeline_brain_db.connect(self.project_root)
        self.assertEqual(applied, [future], msg="future migration should have run once")
        self.assertEqual(timeline_brain_db._read_schema_version(conn2), future)
        self.assertTrue(timeline_brain_db._column_exists(conn2, "brain_edits", "test_column"))

    def test_migration_is_idempotent_on_reopen(self) -> None:
        # Open a fresh DB at base version first.
        conn = timeline_brain_db.connect(self.project_root)
        base_version = timeline_brain_db._read_schema_version(conn)
        timeline_brain_db.close_all()
        # Register a synthetic future migration and bump the schema.
        future = base_version + 1
        calls: list[int] = []

        @timeline_brain_db.register_migration(future)
        def _future(conn: "timeline_brain_db.sqlite3.Connection") -> None:
            calls.append(future)

        timeline_brain_db.SCHEMA_VERSION = future
        timeline_brain_db.connect(self.project_root)
        timeline_brain_db.close_all()
        # Reopen — migration should NOT re-run because schema_version is already at future.
        timeline_brain_db.connect(self.project_root)
        self.assertEqual(calls, [future], msg="migration ran more than once")


class ConcurrencyHardening(unittest.TestCase):
    """Verify busy_timeout PRAGMA + retry-on-busy in transaction()."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="brain_concurrency_test_")
        self.project_root = os.path.join(self.tmp, "project")
        os.makedirs(self.project_root, exist_ok=True)

    def tearDown(self) -> None:
        timeline_brain_db.reset_for_test(self.project_root)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_busy_timeout_pragma_is_set(self) -> None:
        conn = timeline_brain_db.connect(self.project_root)
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        self.assertEqual(int(row[0]), timeline_brain_db.BUSY_TIMEOUT_MS)

    def _insert(self, conn, name: str, version: int) -> None:
        conn.execute(
            "INSERT INTO timeline_versions(timeline_name, version, created_at, "
            "archived_timeline_name, archived_bin_path) VALUES (?, ?, ?, ?, ?)",
            (name, version, "2026-07-24T00:00:00Z", "x", "Master/Archive"),
        )

    def test_concurrent_threads_do_not_nest_transactions(self) -> None:
        """Two threads writing through the SAME cached connection must serialize.

        Before the per-database write lock, thread B's BEGIN IMMEDIATE landed
        inside thread A's open transaction and raised "cannot start a
        transaction within a transaction" — which the busy-retry loop does not
        catch, so it surfaced as a hard error.
        """
        import threading as _threading

        timeline_brain_db.connect(self.project_root)
        errors: list = []
        start = _threading.Barrier(4)

        def writer(n: int) -> None:
            try:
                start.wait(timeout=5)
                for i in range(10):
                    with timeline_brain_db.transaction(self.project_root) as txn:
                        self._insert(txn, f"T{n}", i)
                        # Widen the window in which a second BEGIN could land.
                        txn.execute("SELECT 1").fetchone()
            except Exception as exc:  # noqa: BLE001 - the assertion is the point
                errors.append(exc)

        threads = [_threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [], msg=f"concurrent writers raised: {errors}")
        conn = timeline_brain_db.connect(self.project_root)
        count = conn.execute("SELECT COUNT(*) FROM timeline_versions").fetchone()[0]
        self.assertEqual(count, 40, msg="every writer's rows should have committed")

    def test_nested_transaction_joins_the_outer_one(self) -> None:
        """A nested transaction() must not issue a second BEGIN or self-deadlock."""
        timeline_brain_db.connect(self.project_root)
        with timeline_brain_db.transaction(self.project_root) as outer:
            self._insert(outer, "Outer", 1)
            with timeline_brain_db.transaction(self.project_root) as inner:
                self.assertIs(inner, outer)
                self._insert(inner, "Inner", 1)

        conn = timeline_brain_db.connect(self.project_root)
        names = {
            r[0] for r in conn.execute("SELECT timeline_name FROM timeline_versions").fetchall()
        }
        self.assertEqual(names, {"Outer", "Inner"})

    def test_nested_transaction_failure_rolls_back_the_whole_thing(self) -> None:
        """The outermost block owns the rollback, so an inner failure discards both writes."""
        timeline_brain_db.connect(self.project_root)
        with self.assertRaises(RuntimeError):
            with timeline_brain_db.transaction(self.project_root) as outer:
                self._insert(outer, "Outer", 1)
                with timeline_brain_db.transaction(self.project_root) as inner:
                    self._insert(inner, "Inner", 1)
                    raise RuntimeError("boom")

        conn = timeline_brain_db.connect(self.project_root)
        count = conn.execute("SELECT COUNT(*) FROM timeline_versions").fetchone()[0]
        self.assertEqual(count, 0)

        # And the depth bookkeeping must be clean afterwards, or the next
        # transaction on this thread would silently skip its BEGIN/COMMIT.
        with timeline_brain_db.transaction(self.project_root) as txn:
            self._insert(txn, "After", 1)
        count = conn.execute("SELECT COUNT(*) FROM timeline_versions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_transaction_retries_when_lock_briefly_held(self) -> None:
        """Hold a write lock for ~120ms from a second connection; transaction() should retry past it."""
        import sqlite3 as _sqlite3
        import threading as _threading

        # Make sure the DB exists.
        timeline_brain_db.connect(self.project_root)
        db_path = timeline_brain_db.db_path_for_project(self.project_root)

        lock_held = _threading.Event()
        release = _threading.Event()

        def hold_lock() -> None:
            other = _sqlite3.connect(db_path, isolation_level=None)
            other.execute(f"PRAGMA busy_timeout={timeline_brain_db.BUSY_TIMEOUT_MS}")
            other.execute("BEGIN IMMEDIATE")
            lock_held.set()
            release.wait(timeout=2.0)
            other.execute("COMMIT")
            other.close()

        t = _threading.Thread(target=hold_lock)
        t.start()
        lock_held.wait(timeout=1.0)
        # Schedule release after ~120ms so the busy_timeout/retry path triggers.
        timer = _threading.Timer(0.12, release.set)
        timer.start()
        try:
            with timeline_brain_db.transaction(self.project_root) as txn:
                txn.execute(
                    "INSERT INTO timeline_versions(timeline_name, version, created_at, archived_timeline_name, archived_bin_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("Concurrent", 1, "2026-05-26T00:00:00Z", "x", "Master/Archive"),
                )
        finally:
            release.set()
            t.join(timeout=3.0)
            timer.cancel()

        conn = timeline_brain_db.connect(self.project_root)
        row = conn.execute(
            "SELECT 1 FROM timeline_versions WHERE timeline_name='Concurrent'"
        ).fetchone()
        self.assertIsNotNone(row, msg="row didn't land after busy retry")


if __name__ == "__main__":
    unittest.main()
