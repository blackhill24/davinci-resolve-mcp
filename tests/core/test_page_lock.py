"""Tests for the page-switch serialization lock."""
import threading
import time
import unittest
from unittest import mock

from src.core import page_lock
from src.core.page_lock import page_lock as plock, open_page_serialized


class PageLockTest(unittest.TestCase):
    def test_reentrant_no_deadlock(self):
        # Nested page_lock() in the same thread must not deadlock (the fcntl
        # lock is only taken at the outermost level).
        with plock():
            with plock():
                self.assertEqual(page_lock._depth, 2)
        self.assertEqual(page_lock._depth, 0)

    def test_open_page_serialized_calls_openpage(self):
        r = mock.Mock()
        r.OpenPage.return_value = True
        self.assertTrue(open_page_serialized(r, "color"))
        r.OpenPage.assert_called_once_with("color")

    def test_serializes_across_threads(self):
        order = []

        def worker():
            with plock():
                order.append("worker")

        with plock():
            t = threading.Thread(target=worker)
            t.start()
            time.sleep(0.05)  # give the worker a chance to (try to) acquire
            order.append("main")
        t.join()
        # The worker must have waited until main released the lock.
        self.assertEqual(order, ["main", "worker"])

    def test_depth_resets_on_exception(self):
        try:
            with plock():
                raise ValueError("boom")
        except ValueError:
            pass
        self.assertEqual(page_lock._depth, 0)


@unittest.skipUnless(page_lock._HAS_FCNTL, "fcntl-only behaviour")
class FileLockTimeoutTest(unittest.TestCase):
    """The inter-process flock must be bounded (#104 finding 8).

    A *hung* holder (not a dead one — the kernel reaps those) used to freeze
    every other MCP process's page switches forever, with no diagnostic.
    """

    def test_acquire_stamps_the_holder_pid(self):
        import os

        fh = page_lock._acquire_file_lock()
        self.addCleanup(fh.close)
        self.assertIsNotNone(fh)
        self.assertEqual(page_lock._holder_pid(fh), str(os.getpid()))

    def test_gives_up_after_the_timeout_instead_of_blocking_forever(self):
        """A never-releasing holder must degrade to no guard, not hang."""
        with mock.patch.object(
            page_lock.fcntl, "flock", side_effect=BlockingIOError(11, "would block")
        ), mock.patch.object(page_lock.time, "sleep") as sleep:
            with self.assertLogs("resolve-mcp.page-lock", level="WARNING") as logs:
                start = time.monotonic()
                fh = page_lock._acquire_file_lock(timeout=0.3)
        self.assertIsNone(fh, "must return None rather than block")
        self.assertLess(time.monotonic() - start, 5, "must not have really waited")
        self.assertTrue(sleep.called, "should have polled rather than spun")
        joined = "\n".join(logs.output)
        self.assertIn("proceeding WITHOUT the inter-process guard", joined)
        self.assertIn(page_lock._LOCKFILE, joined)

    def test_page_lock_still_runs_the_body_when_the_file_lock_is_unavailable(self):
        ran = []
        with mock.patch.object(page_lock, "_acquire_file_lock", return_value=None):
            with plock():
                ran.append(True)
        self.assertEqual(ran, [True])
        self.assertEqual(page_lock._depth, 0)

    def test_timeout_is_configurable_by_env(self):
        import importlib

        with mock.patch.dict("os.environ", {"DAVINCI_MCP_PAGE_LOCK_TIMEOUT": "7.5"}):
            reloaded = importlib.reload(page_lock)
            try:
                self.assertEqual(reloaded.PAGE_LOCK_TIMEOUT_SECONDS, 7.5)
            finally:
                importlib.reload(page_lock)


if __name__ == "__main__":
    unittest.main()
