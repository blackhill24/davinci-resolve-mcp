"""Unit tests for orchestrate.py's P3 pure additions: can_run_stage,
rollback_stage bookkeeping, finish_job bookkeeping. No Resolve required.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from src.domains.orchestration.utils import orchestrate


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


class _FakeItem:
    """Timeline item whose AddVersion succeeds only if it is `gradeable`.

    Mirrors the live behaviour: a title/generator refuses AddVersion with a bare
    False, exposing no reason and no version list.
    """

    def __init__(self, name: str, gradeable: bool = True):
        self.name = name
        self.gradeable = gradeable
        self.versions: list = []

    def GetName(self):
        return self.name

    def AddVersion(self, label, _type):
        if not self.gradeable:
            return False
        self.versions.append(label)
        return True

    def GetVersionNameList(self, _type):
        return list(self.versions) if self.gradeable else None


class _FakeTimeline:
    def __init__(self, items, current=None):
        self.items = items
        self.current = current
        self.duplicated_as = None

    def GetCurrentVideoItem(self):
        return self.current

    def GetItemListInTrack(self, track_type, index):
        return list(self.items) if (track_type, index) == ("video", 1) else []

    def DuplicateTimeline(self, label):
        self.duplicated_as = label
        return object()


class _FakeProject:
    def __init__(self, timeline):
        self.timeline = timeline

    def GetCurrentTimeline(self):
        return self.timeline


class GradeSnapshotPickerTests(unittest.TestCase):
    """`grade_version` snapshots must not silently lose rollback cover to a
    title card sitting first on V1 (the shape auto_edit builds)."""

    def _take(self, timeline):
        import src.server  # noqa: F401  (actions imports back through it)
        from src.domains.orchestration.actions import _orchestrate_take_snapshot

        return _orchestrate_take_snapshot(_FakeProject(timeline), job_id="job1", stage="grade")

    def test_skips_ungradeable_item_and_uses_the_next(self):
        title, footage = _FakeItem("Text", gradeable=False), _FakeItem("shot_01.mov")
        snap = self._take(_FakeTimeline([title, footage]))
        self.assertTrue(snap["success"], snap)
        self.assertEqual(snap["kind"], "grade_version")
        self.assertEqual(snap["snapshot_id"], "_orch_job1_grade")
        self.assertEqual(footage.versions, ["_orch_job1_grade"])

    def test_playhead_item_is_tried_first(self):
        under_playhead, other = _FakeItem("shot_02.mov"), _FakeItem("shot_01.mov")
        snap = self._take(_FakeTimeline([other], current=under_playhead))
        self.assertTrue(snap["success"], snap)
        self.assertEqual(under_playhead.versions, ["_orch_job1_grade"])
        self.assertEqual(other.versions, [])

    def test_falls_back_to_a_timeline_duplicate_when_nothing_is_gradeable(self):
        timeline = _FakeTimeline([_FakeItem("Text", gradeable=False),
                                  _FakeItem("Solid", gradeable=False)])
        snap = self._take(timeline)
        self.assertTrue(snap["success"], snap)
        self.assertEqual(snap["kind"], "timeline_duplicate")
        self.assertEqual(snap["downgraded_from"], "grade_version")
        self.assertIn("Text", snap["note"])
        self.assertEqual(timeline.duplicated_as, "_orch_job1_grade")

    def test_no_video_item_at_all_is_an_error(self):
        snap = self._take(_FakeTimeline([]))
        self.assertFalse(snap["success"], snap)
        self.assertIn("no video item", snap["error"])


if __name__ == "__main__":
    unittest.main()
