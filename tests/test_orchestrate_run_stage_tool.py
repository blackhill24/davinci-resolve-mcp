"""Offline tests for orchestrate's P3 tool actions (src/server.py):
run_stage, rollback_stage, finish_job.

Domain-tool delegates (timeline, timeline_item_color, timeline_markers,
media_pool helpers) are patched directly — this suite verifies orchestrate's
OWN logic (state transitions, gate checks, snapshot bookkeeping, foreign
keys), not the domain tools' internals (those have their own suites).
MagicMock's default magic methods (__iter__, __len__, __int__) make an
unconfigured `proj` safe to pass through _orchestrate_capture_fingerprint
without raising.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

import src.server as s
from src.utils import orchestrate


def run(coro):
    return asyncio.run(coro)


def _files():
    return ["/tmp/does-not-need-to-exist-a.mov"]


def _fp(items=10, grade="g1", media="m1"):
    return {"timeline_item_count": items, "grade_version_id": grade, "media_path_set_hash": media}


class OrchestrateRunStageBase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="orchestrate-runstage-tool-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)
        self.proj = mock.MagicMock()

    def _start_job(self, **kw):
        created = orchestrate.create_job(self.root, files=_files(), **kw)
        self.assertTrue(created["success"], created)
        return created["job_id"]

    def _advance_to_done(self, job_id, stages, fingerprints=None):
        fingerprints = fingerprints or {}
        for stage in stages:
            orchestrate.advance_stage(self.root, job_id, stage, "running")
            orchestrate.advance_stage(self.root, job_id, stage, "done")
            if stage in fingerprints:
                orchestrate.capture_stage_fingerprint(self.root, job_id, stage, fingerprints[stage])

    def _call(self, action, params):
        with mock.patch.object(
            s, "_destructive_versioning_provider",
            return_value=(None, self.proj, self.root, "P"),
        ):
            return run(s.orchestrate(action, params))

    def _run_stage(self, params):
        return self._call("run_stage", params)


class RunStageRefusalTests(OrchestrateRunStageBase):
    def test_unknown_job(self):
        out = self._run_stage({"job_id": "nonexistent", "stage": "ingest"})
        self.assertIn("error", out)

    def test_non_cursor_stage_refused(self):
        job_id = self._start_job()
        out = self._run_stage({"job_id": job_id, "stage": "grade"})
        self.assertIn("error", out)

    def test_unsupported_stage_refused(self):
        job_id = self._start_job(stages=["intake", "fusion"])
        out = self._run_stage({"job_id": job_id, "stage": "fusion"})
        self.assertIn("error", out)


class RunStageIngestTests(OrchestrateRunStageBase):
    def test_no_media_pool_fails_stage(self):
        job_id = self._start_job()
        self.proj.GetMediaPool.return_value = None
        out = self._run_stage({"job_id": job_id, "stage": "ingest"})
        self.assertIn("error", out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["ingest"]["status"], "failed")

    def test_successful_import_marks_done(self):
        job_id = self._start_job()
        with mock.patch.object(s, "_ensure_folder_path", return_value=(mock.MagicMock(), None)), \
             mock.patch.object(s, "_safe_import_media", return_value={"success": True, "imported": 1}):
            out = self._run_stage({"job_id": job_id, "stage": "ingest"})
        self.assertTrue(out.get("success"), out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["ingest"]["status"], "done")
        self.assertIsNotNone(job["stages"]["ingest"]["fingerprint"])
        self.assertEqual(job["cursor"], "analysis")

    def test_import_error_fails_stage(self):
        job_id = self._start_job()
        with mock.patch.object(s, "_ensure_folder_path", return_value=(mock.MagicMock(), None)), \
             mock.patch.object(s, "_safe_import_media", return_value={"error": {"message": "boom"}}):
            out = self._run_stage({"job_id": job_id, "stage": "ingest"})
        self.assertIn("error", out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["ingest"]["status"], "failed")


class RunStageAnalysisTests(OrchestrateRunStageBase):
    def test_talking_head_waits_on_analysis_first_call(self):
        job_id = self._start_job(genre="talking_head")
        self._advance_to_done(job_id, ["ingest"])
        started = {"success": True, "brief_id": "b1", "analysis_job_id": "aj1"}
        with mock.patch.object(s, "auto_edit", mock.AsyncMock(return_value=started)):
            out = self._run_stage({"job_id": job_id, "stage": "analysis"})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out.get("waiting_on"), "analysis")
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["analysis"]["status"], "running")

    def test_non_talking_head_starts_batch_job(self):
        job_id = self._start_job(genre="documentary")
        self._advance_to_done(job_id, ["ingest"])
        started = {"success": True, "job_id": "batch-1"}
        with mock.patch.object(s, "media_analysis", mock.AsyncMock(return_value=started)):
            out = self._run_stage({"job_id": job_id, "stage": "analysis"})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out.get("waiting_on"), "analysis")
        self.assertEqual(out.get("batch_job_id"), "batch-1")
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["analysis"]["foreign_keys"]["batch_job_id"], "batch-1")

    def test_non_talking_head_completes_when_batch_job_done(self):
        job_id = self._start_job(genre="documentary")
        self._advance_to_done(job_id, ["ingest"])
        orchestrate.set_stage_foreign_keys(self.root, job_id, "analysis", batch_job_id="batch-1")
        status = {"success": True, "status": "completed"}
        with mock.patch.object(s, "media_analysis", mock.AsyncMock(return_value=status)):
            out = self._run_stage({"job_id": job_id, "stage": "analysis"})
        self.assertTrue(out.get("success"), out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["analysis"]["status"], "done")


class RunStageConformTests(OrchestrateRunStageBase):
    def _to_conform(self, job_id):
        self._advance_to_done(job_id, ["ingest", "analysis", "edit"])

    def test_clean_conform_marks_done(self):
        job_id = self._start_job()
        self._to_conform(job_id)
        clean = {"gap_count": 0, "overlap_count": 0}
        missing = {"missing_count": 0}
        with mock.patch.object(s, "timeline", side_effect=[clean, missing]):
            out = self._run_stage({"job_id": job_id, "stage": "conform"})
        self.assertTrue(out.get("success"), out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["conform"]["status"], "done")

    def test_gaps_refuse_without_accept(self):
        job_id = self._start_job()
        self._to_conform(job_id)
        gaps = {"gap_count": 2, "overlap_count": 0}
        missing = {"missing_count": 0}
        with mock.patch.object(s, "timeline", side_effect=[gaps, missing]):
            out = self._run_stage({"job_id": job_id, "stage": "conform"})
        self.assertIn("error", out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["conform"]["status"], "failed")

    def test_gaps_accepted_marks_done(self):
        job_id = self._start_job()
        self._to_conform(job_id)
        gaps = {"gap_count": 2, "overlap_count": 0}
        missing = {"missing_count": 0}
        with mock.patch.object(s, "timeline", side_effect=[gaps, missing]):
            out = self._run_stage({"job_id": job_id, "stage": "conform", "accept_gaps": True})
        self.assertTrue(out.get("success"), out)


class RunStageGradeTests(OrchestrateRunStageBase):
    def _to_grade(self, job_id):
        self._advance_to_done(job_id, ["ingest", "analysis", "edit", "conform"])

    def test_no_grade_options_is_noop_done(self):
        job_id = self._start_job()
        self._to_grade(job_id)
        out = self._run_stage({"job_id": job_id, "stage": "grade"})
        self.assertTrue(out.get("success"), out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["grade"]["status"], "done")

    def test_drx_path_applies_via_safe_apply_drx(self):
        job_id = self._start_job()
        self._to_grade(job_id)
        applied = {"success": True, "path": "/tmp/x.drx"}
        with mock.patch.object(s, "timeline_item_color", return_value=applied) as mocked:
            out = self._run_stage({"job_id": job_id, "stage": "grade",
                                    "grade": {"drx_path": "/tmp/x.drx"}})
        self.assertTrue(out.get("success"), out)
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args[0][0], "safe_apply_drx")

    def test_cdl_applies_via_safe_set_cdl(self):
        job_id = self._start_job()
        self._to_grade(job_id)
        applied = {"success": True}
        with mock.patch.object(s, "timeline_item_color", return_value=applied) as mocked:
            out = self._run_stage({"job_id": job_id, "stage": "grade",
                                    "grade": {"cdl": {"slope": [1, 1, 1]}}})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(mocked.call_args[0][0], "safe_set_cdl")

    def test_confirmation_required_leaves_stage_running(self):
        job_id = self._start_job()
        self._to_grade(job_id)
        pending = {"status": "confirmation_required", "confirm_token": "tok"}
        with mock.patch.object(s, "timeline_item_color", return_value=pending):
            out = self._run_stage({"job_id": job_id, "stage": "grade",
                                    "grade": {"drx_path": "/tmp/x.drx"}})
        self.assertEqual(out.get("status"), "confirmation_required")
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["grade"]["status"], "running")

    def test_no_duplicate_snapshot_on_confirm_retry(self):
        job_id = self._start_job()
        self._to_grade(job_id)
        pending = {"status": "confirmation_required", "confirm_token": "tok"}
        applied = {"success": True}
        with mock.patch.object(s, "timeline_item_color", side_effect=[pending, applied]):
            self._run_stage({"job_id": job_id, "stage": "grade", "grade": {"drx_path": "/tmp/x.drx"}})
            self._run_stage({"job_id": job_id, "stage": "grade",
                              "grade": {"drx_path": "/tmp/x.drx"}, "confirm_token": "tok"})
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(len(job["stages"]["grade"]["snapshot_ids"]), 1)

    def test_failed_grade_leaves_snapshot_for_rollback(self):
        job_id = self._start_job()
        self._to_grade(job_id)
        failed = {"success": False, "error": "grade blew up"}
        with mock.patch.object(s, "timeline_item_color", return_value=failed):
            out = self._run_stage({"job_id": job_id, "stage": "grade",
                                    "grade": {"drx_path": "/tmp/x.drx"}})
        self.assertFalse(out.get("success"))
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["grade"]["status"], "failed")
        self.assertEqual(len(job["stages"]["grade"]["snapshot_ids"]), 1)


class RunStageAudioReviewTests(OrchestrateRunStageBase):
    def _approve_g2(self, job_id, fp):
        orchestrate.record_gate_approval(
            self.root, job_id, "G2",
            {"fingerprint": fp, "mode": "standard", "adopted": False, "forced": False,
             "vision_assessment": "fine", "preview_frame_path": "/tmp/frame.png"})

    def test_audio_requires_g2_first(self):
        job_id = self._start_job()
        self._advance_to_done(job_id, ["ingest", "analysis", "edit", "conform", "grade"])
        out = self._run_stage({"job_id": job_id, "stage": "audio"})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out.get("waiting_on"), "G2_approval")
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["audio"]["status"], "pending")

    def test_audio_noop_when_unspecified(self):
        job_id = self._start_job()
        self._advance_to_done(job_id, ["ingest", "analysis", "edit", "conform", "grade"])
        fp = _fp()
        with mock.patch.object(s, "_orchestrate_capture_fingerprint", return_value=fp):
            self._approve_g2(job_id, fp)
            out = self._run_stage({"job_id": job_id, "stage": "audio"})
        self.assertTrue(out.get("success"), out)
        self.assertNotEqual(out.get("waiting_on"), "G2_approval")
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["audio"]["status"], "done")

    def test_audio_applies_when_specified(self):
        job_id = self._start_job()
        self._advance_to_done(job_id, ["ingest", "analysis", "edit", "conform", "grade"])
        fp = _fp()
        applied = {"success": True}
        with mock.patch.object(s, "_orchestrate_capture_fingerprint", return_value=fp):
            self._approve_g2(job_id, fp)
            with mock.patch.object(s, "timeline", return_value=applied) as mocked:
                out = self._run_stage({"job_id": job_id, "stage": "audio",
                                        "audio": {"track_index": 1, "volume_db": -3}})
        self.assertTrue(out.get("success"), out)
        mocked.assert_called_once_with("safe_set_audio_properties", {"track_index": 1, "volume_db": -3})

    def test_review_marks_done(self):
        job_id = self._start_job()
        self._advance_to_done(
            job_id, ["ingest", "analysis", "edit", "conform", "grade", "audio", "deliver"])
        report = {"path": "/tmp/report.json"}
        with mock.patch.object(s, "timeline_markers", return_value=report):
            out = self._run_stage({"job_id": job_id, "stage": "review"})
        self.assertTrue(out.get("success"), out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["review"]["status"], "done")


class RunStageDeliverTests(OrchestrateRunStageBase):
    def _to_deliver(self, job_id, *, gate_fp=None):
        self._advance_to_done(
            job_id, ["ingest", "analysis", "edit", "conform", "grade", "audio"],
            {"audio": gate_fp or _fp()})

    def test_requires_g3_approved(self):
        job_id = self._start_job()
        self._to_deliver(job_id)
        out = self._run_stage({"job_id": job_id, "stage": "deliver"})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out.get("waiting_on"), "G3_approval")
        job = orchestrate.load_job(self.root, job_id)
        # Refused before even entering "running" — cursor stays at deliver.
        self.assertEqual(job["stages"]["deliver"]["status"], "pending")

    def test_render_success_marks_done_and_records_output_path(self):
        job_id = self._start_job()
        fp = _fp()
        self._to_deliver(job_id, gate_fp=fp)
        orchestrate.record_gate_approval(
            self.root, job_id, "G3", {"fingerprint": fp, "mode": "standard", "adopted": False, "forced": False})
        with tempfile.TemporaryDirectory() as target_dir:
            out_file = os.path.join(target_dir, "orchestrate_" + job_id + ".mov")
            with open(out_file, "wb"):
                pass
            prepared = {"success": True, "job_id": "render-1"}
            # Gate validity re-probes "now" — pin the probe to the fingerprint
            # the gate was approved against so it reads as still-valid.
            with mock.patch.object(s, "_orchestrate_capture_fingerprint", return_value=fp), \
                 mock.patch.object(s, "_prepare_render_job", return_value=prepared), \
                 mock.patch.object(s, "_run_maybe_background",
                                    return_value={"success": True, "job_id": "render-1",
                                                  "output_path": out_file}):
                out = self._run_stage({"job_id": job_id, "stage": "deliver",
                                        "render": {"target_dir": target_dir}})
        self.assertTrue(out.get("success"), out)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["deliver"]["status"], "done")
        self.assertEqual(job["stages"]["deliver"]["foreign_keys"]["output_path"], out_file)

    def test_prepare_failure_marks_failed_resumable(self):
        job_id = self._start_job()
        fp = _fp()
        self._to_deliver(job_id, gate_fp=fp)
        orchestrate.record_gate_approval(
            self.root, job_id, "G3", {"fingerprint": fp, "mode": "standard", "adopted": False, "forced": False})
        with mock.patch.object(s, "_orchestrate_capture_fingerprint", return_value=fp), \
             mock.patch.object(s, "_prepare_render_job",
                                return_value={"success": False, "error": "bad settings"}):
            out = self._run_stage({"job_id": job_id, "stage": "deliver",
                                    "render": {"target_dir": "/tmp"}})
        self.assertFalse(out.get("success"))
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["deliver"]["status"], "failed")
        self.assertIn("failed-resumable-via-Resolve", job["stages"]["deliver"]["notes"])


class RollbackStageTests(OrchestrateRunStageBase):
    def test_no_snapshot_refuses(self):
        job_id = self._start_job()
        out = self._call("rollback_stage", {"job_id": job_id, "stage": "grade"})
        self.assertIn("error", out)

    def test_timeline_duplicate_restore(self):
        job_id = self._start_job()
        orchestrate.advance_stage(self.root, job_id, "edit", "running")
        orchestrate.advance_stage(self.root, job_id, "edit", "failed")
        orchestrate.record_snapshot(self.root, job_id, "edit", "_orch_x_edit", kind="timeline_duplicate")
        snap_tl = mock.MagicMock()
        snap_tl.GetName.return_value = "_orch_x_edit"
        snap_tl.GetUniqueId.return_value = "snap-uid"
        current_tl = mock.MagicMock()
        current_tl.GetUniqueId.return_value = "current-uid"
        self.proj.GetCurrentTimeline.return_value = current_tl
        self.proj.GetTimelineCount.return_value = 1
        self.proj.GetTimelineByIndex.return_value = snap_tl
        out = self._call("rollback_stage", {"job_id": job_id, "stage": "edit"})
        self.assertTrue(out.get("success"), out)
        self.proj.SetCurrentTimeline.assert_called_once_with(snap_tl)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["edit"]["status"], "pending")
        self.assertEqual(job["stages"]["edit"]["snapshot_ids"], [])

    def test_grade_version_restore_keeps_snapshot(self):
        job_id = self._start_job()
        orchestrate.advance_stage(self.root, job_id, "ingest", "running")
        orchestrate.advance_stage(self.root, job_id, "ingest", "done")
        orchestrate.advance_stage(self.root, job_id, "analysis", "running")
        orchestrate.advance_stage(self.root, job_id, "analysis", "done")
        orchestrate.advance_stage(self.root, job_id, "edit", "running")
        orchestrate.advance_stage(self.root, job_id, "edit", "done")
        orchestrate.advance_stage(self.root, job_id, "conform", "running")
        orchestrate.advance_stage(self.root, job_id, "conform", "done")
        orchestrate.advance_stage(self.root, job_id, "grade", "running")
        orchestrate.advance_stage(self.root, job_id, "grade", "failed")
        orchestrate.record_snapshot(self.root, job_id, "grade", "_orch_x_grade", kind="grade_version")
        item = mock.MagicMock()
        item.LoadVersionByName.return_value = True
        tl = mock.MagicMock()
        tl.GetItemListInTrack.return_value = [item]
        self.proj.GetCurrentTimeline.return_value = tl
        out = self._call("rollback_stage", {"job_id": job_id, "stage": "grade"})
        self.assertTrue(out.get("success"), out)
        item.LoadVersionByName.assert_called_once_with("_orch_x_grade", 0)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(len(job["stages"]["grade"]["snapshot_ids"]), 1)


class FinishJobToolTests(OrchestrateRunStageBase):
    def _all_done(self, job_id, output_path=None):
        job = orchestrate.load_job(self.root, job_id)
        for stage in job["manifest"]:
            if stage == "intake":
                continue
            orchestrate.advance_stage(self.root, job_id, stage, "running")
            orchestrate.advance_stage(self.root, job_id, stage, "done")
        if output_path:
            orchestrate.set_stage_foreign_keys(self.root, job_id, "deliver", output_path=output_path)

    def test_refuses_when_incomplete(self):
        job_id = self._start_job()
        out = self._call("finish_job", {"job_id": job_id})
        self.assertIn("error", out)

    def test_finishes_and_verifies_output(self):
        job_id = self._start_job()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            output_path = f.name
        self.addCleanup(os.remove, output_path)
        self._all_done(job_id, output_path=output_path)
        out = self._call("finish_job", {"job_id": job_id})
        self.assertTrue(out.get("success"), out)
        self.assertTrue(out["output_verified"])
        self.assertEqual(out["output_path"], output_path)

    def test_unverified_output_still_finishes(self):
        job_id = self._start_job()
        self._all_done(job_id, output_path="/tmp/does-not-exist-anymore.mov")
        out = self._call("finish_job", {"job_id": job_id})
        self.assertTrue(out.get("success"), out)
        self.assertFalse(out["output_verified"])

    def test_purges_snapshots_by_default(self):
        job_id = self._start_job()
        self._all_done(job_id)
        orchestrate.record_snapshot(self.root, job_id, "grade", "_orch_x_grade", kind="grade_version")
        item = mock.MagicMock()
        item.DeleteVersionByName.return_value = True
        tl = mock.MagicMock()
        tl.GetItemListInTrack.return_value = [item]
        self.proj.GetCurrentTimeline.return_value = tl
        out = self._call("finish_job", {"job_id": job_id})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["purged_count"], 1)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["job_state"], "finished")
        self.assertEqual(job["stages"]["grade"]["snapshot_ids"], [])

    def test_keep_snapshots_opts_out_of_purge(self):
        job_id = self._start_job()
        self._all_done(job_id)
        orchestrate.record_snapshot(self.root, job_id, "grade", "_orch_x_grade", kind="grade_version")
        out = self._call("finish_job", {"job_id": job_id, "keep_snapshots": True})
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["purged_count"], 0)
        self.assertTrue(out["kept_snapshots"])


if __name__ == "__main__":
    unittest.main()
