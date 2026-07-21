"""Unit tests for orchestrate.py's P3 pure additions: can_run_stage,
rollback_stage bookkeeping, finish_job bookkeeping. No Resolve required.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from src.utils import orchestrate


def _files():
    return ["/tmp/does-not-need-to-exist-a.mov"]


class OrchestrateBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="orchestrate-runstage-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)
        self.job_id = orchestrate.create_job(self.root, files=_files())["job_id"]


class CanRunStageTests(OrchestrateBase):
    def test_cursor_stage_pending_is_runnable(self):
        job = orchestrate.load_job(self.root, self.job_id)
        self.assertIsNone(orchestrate.can_run_stage(job, "ingest"))

    def test_non_cursor_stage_refused(self):
        job = orchestrate.load_job(self.root, self.job_id)
        self.assertIsNotNone(orchestrate.can_run_stage(job, "grade"))

    def test_unknown_stage_refused(self):
        job = orchestrate.load_job(self.root, self.job_id)
        self.assertIsNotNone(orchestrate.can_run_stage(job, "teleport"))

    def test_running_stage_is_runnable_again(self):
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        job = orchestrate.load_job(self.root, self.job_id)
        self.assertIsNone(orchestrate.can_run_stage(job, "ingest"))

    def test_failed_stage_is_runnable_again(self):
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "failed")
        job = orchestrate.load_job(self.root, self.job_id)
        self.assertIsNone(orchestrate.can_run_stage(job, "ingest"))

    def test_done_stage_not_runnable(self):
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "done")
        job = orchestrate.load_job(self.root, self.job_id)
        # cursor has moved on to "analysis" now, so "ingest" is no longer
        # the cursor AND its own status is "done" — either reason refuses.
        self.assertIsNotNone(orchestrate.can_run_stage(job, "ingest"))


class RollbackStageTests(OrchestrateBase):
    def test_rollback_resets_failed_stage_to_pending(self):
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "failed", notes=["boom"])
        result = orchestrate.rollback_stage(self.root, self.job_id, "ingest", snapshot_consumed=False)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["job"]["stages"]["ingest"]["status"], "pending")

    def test_rollback_refuses_on_pending_stage(self):
        result = orchestrate.rollback_stage(self.root, self.job_id, "ingest", snapshot_consumed=False)
        self.assertFalse(result["success"])

    def test_rollback_refuses_on_done_stage(self):
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "done")
        result = orchestrate.rollback_stage(self.root, self.job_id, "ingest", snapshot_consumed=False)
        self.assertFalse(result["success"])

    def test_consumed_snapshot_cleared(self):
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "failed")
        orchestrate.record_snapshot(self.root, self.job_id, "ingest", "snap-1", kind="timeline_duplicate")
        result = orchestrate.rollback_stage(self.root, self.job_id, "ingest", snapshot_consumed=True)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["job"]["stages"]["ingest"]["snapshot_ids"], [])

    def test_unconsumed_snapshot_kept(self):
        orchestrate.advance_stage(self.root, self.job_id, "grade" if False else "ingest", "running")
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "failed")
        orchestrate.record_snapshot(self.root, self.job_id, "ingest", "snap-1", kind="grade_version")
        result = orchestrate.rollback_stage(self.root, self.job_id, "ingest", snapshot_consumed=False)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["job"]["stages"]["ingest"]["snapshot_ids"]), 1)

    def test_unknown_job(self):
        result = orchestrate.rollback_stage(self.root, "nonexistent", "ingest", snapshot_consumed=False)
        self.assertFalse(result["success"])


class FinishJobTests(OrchestrateBase):
    def _finish_all_stages(self):
        job = orchestrate.load_job(self.root, self.job_id)
        for stage in job["manifest"]:
            if stage == "intake":
                continue
            orchestrate.advance_stage(self.root, self.job_id, stage, "running")
            orchestrate.advance_stage(self.root, self.job_id, stage, "done")

    def test_refuses_when_stages_incomplete(self):
        result = orchestrate.finish_job(self.root, self.job_id)
        self.assertFalse(result["success"])
        self.assertIn("ingest", result["error"])

    def test_finishes_when_all_done(self):
        self._finish_all_stages()
        result = orchestrate.finish_job(self.root, self.job_id, output_path="/tmp/out.mov")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["job"]["job_state"], "finished")
        self.assertEqual(result["job"]["output_path"], "/tmp/out.mov")
        self.assertIsNotNone(result["job"]["finished_at"])

    def test_purges_snapshots_across_every_stage(self):
        self._finish_all_stages()
        orchestrate.record_snapshot(self.root, self.job_id, "edit", "snap-edit", kind="timeline_duplicate")
        orchestrate.record_snapshot(self.root, self.job_id, "grade", "snap-grade", kind="grade_version")
        result = orchestrate.finish_job(self.root, self.job_id)
        self.assertTrue(result["success"], result)
        ids = {s["id"] for s in result["snapshots_to_clean"]}
        self.assertEqual(ids, {"snap-edit", "snap-grade"})
        job = orchestrate.load_job(self.root, self.job_id)
        self.assertEqual(job["stages"]["edit"]["snapshot_ids"], [])
        self.assertEqual(job["stages"]["grade"]["snapshot_ids"], [])

    def test_no_snapshots_reports_empty_list(self):
        self._finish_all_stages()
        result = orchestrate.finish_job(self.root, self.job_id)
        self.assertEqual(result["snapshots_to_clean"], [])

    def test_unknown_job(self):
        result = orchestrate.finish_job(self.root, "nonexistent")
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
