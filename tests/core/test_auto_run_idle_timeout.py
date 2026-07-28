"""#142 finding 7: the B3 auto-run idle timeout never fired.

`ensure_auto_run_for_destructive` gates the idle close on
``if active and last and (now - last) > timeout``. `begin_run` never stamped
``_LAST_DESTRUCTIVE_AT["epoch"]``, which starts at 0.0, so for any explicitly
begun run `last` was falsy and the check short-circuited: a run left open by a
forgotten `end_run` was reused indefinitely instead of being auto-closed after
90s, and archives kept collapsing into a stale run — exactly what the auto-run
design exists to prevent.

The mirror-image defect: `end_run` did not reset the epoch, so a NEW run
inherited the previous run's stale timestamp and could be auto-closed on its
first destructive op.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from src.core import analysis_runs


class AutoRunIdleTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_root = os.path.join(self.tmp.name, "Example_Project")
        os.makedirs(self.project_root)
        # Leave process-level run state as we found it.
        previous = dict(analysis_runs._CURRENT_RUN)
        previous_epoch = analysis_runs._LAST_DESTRUCTIVE_AT["epoch"]

        def restore():
            analysis_runs._CURRENT_RUN.update(previous)
            analysis_runs._LAST_DESTRUCTIVE_AT["epoch"] = previous_epoch

        self.addCleanup(restore)
        analysis_runs._CURRENT_RUN.update({"id": None, "initiator": None, "label": None})
        analysis_runs._LAST_DESTRUCTIVE_AT["epoch"] = 0.0

    def _ensure(self):
        return analysis_runs.ensure_auto_run_for_destructive(
            project_root=self.project_root, idle_timeout_seconds=90.0)

    def test_begin_run_starts_the_idle_clock(self):
        self.assertEqual(0.0, analysis_runs._LAST_DESTRUCTIVE_AT["epoch"])
        analysis_runs.begin_run(project_root=self.project_root, label="explicit")
        self.assertGreater(
            analysis_runs._LAST_DESTRUCTIVE_AT["epoch"], 0.0,
            "an unstamped epoch is what made the idle check short-circuit",
        )

    def test_an_explicit_run_left_open_is_auto_closed_once_idle(self):
        opened = analysis_runs.begin_run(
            project_root=self.project_root, label="forgotten")["analysis_run_id"]

        # Simulate the run having been idle for well over the timeout.
        analysis_runs._LAST_DESTRUCTIVE_AT["epoch"] -= 600.0

        run_id = self._ensure()
        self.assertNotEqual(
            opened, run_id,
            "the stale run must be closed and a fresh auto-run opened",
        )

    def test_a_run_still_within_the_timeout_is_reused(self):
        opened = analysis_runs.begin_run(
            project_root=self.project_root, label="active")["analysis_run_id"]
        self.assertEqual(opened, self._ensure())
        self.assertEqual(opened, self._ensure())

    def test_end_run_clears_the_clock_so_the_next_run_starts_fresh(self):
        first = analysis_runs.begin_run(
            project_root=self.project_root, label="first")["analysis_run_id"]
        # Age it, then close it — the stale epoch must not survive.
        analysis_runs._LAST_DESTRUCTIVE_AT["epoch"] -= 600.0
        analysis_runs.end_run(project_root=self.project_root, analysis_run_id=first)
        self.assertEqual(0.0, analysis_runs._LAST_DESTRUCTIVE_AT["epoch"])

        second = analysis_runs.begin_run(
            project_root=self.project_root, label="second")["analysis_run_id"]
        self.assertEqual(
            second, self._ensure(),
            "a brand-new run must not be auto-closed on its first destructive op",
        )

    def test_a_failing_auto_close_does_not_block_the_destructive_call(self):
        analysis_runs.begin_run(project_root=self.project_root, label="forgotten")
        analysis_runs._LAST_DESTRUCTIVE_AT["epoch"] -= 600.0
        with mock.patch.object(analysis_runs, "end_run",
                               side_effect=RuntimeError("db locked")):
            run_id = self._ensure()  # must not raise
        self.assertTrue(run_id)


if __name__ == "__main__":
    unittest.main()
