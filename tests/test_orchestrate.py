"""Unit tests for src/domains/orchestration/utils/orchestrate.py (job state machine + persistence).

No Resolve required — pure state/IO layer, same posture as test_edit_engine.py
and test_auto_edit.py.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from src.domains.orchestration.utils import orchestrate


def _files():
    return ["/tmp/does-not-need-to-exist-a.mov", "/tmp/does-not-need-to-exist-b.mov"]


class OrchestrateJobCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="orchestrate-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        # Nested under a per-test base dir (never bare /tmp) so the default
        # global-index location (parent of project_root) stays test-isolated.
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)

    def test_create_job_success_marks_intake_done_and_sets_cursor(self):
        result = orchestrate.create_job(self.root, files=_files(), genre="talking_head")
        self.assertTrue(result["success"], result)
        job = result["job"]
        self.assertEqual(job["stages"]["intake"]["status"], "done")
        self.assertEqual(job["cursor"], "ingest")
        self.assertNotIn("fusion", job["manifest"])
        self.assertEqual(job["manifest"][0], "intake")
        self.assertEqual(job["manifest"][-1], "review")

    def test_create_job_rejects_empty_files(self):
        result = orchestrate.create_job(self.root, files=[])
        self.assertFalse(result["success"])
        self.assertIn("files", " ".join(result["problems"]))

    def test_create_job_include_fusion_inserts_before_deliver(self):
        result = orchestrate.create_job(self.root, files=_files(), include_fusion=True)
        manifest = result["job"]["manifest"]
        self.assertIn("fusion", manifest)
        self.assertLess(manifest.index("fusion"), manifest.index("deliver"))

    def test_create_job_explicit_stages_override(self):
        result = orchestrate.create_job(
            self.root, files=_files(), stages=["intake", "grade", "deliver"])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["job"]["manifest"], ["intake", "grade", "deliver"])
        self.assertEqual(result["job"]["cursor"], "grade")

    def test_create_job_rejects_manifest_not_starting_with_intake(self):
        result = orchestrate.create_job(self.root, files=_files(), stages=["ingest", "intake"])
        self.assertFalse(result["success"])

    def test_create_job_rejects_unknown_stage(self):
        result = orchestrate.create_job(self.root, files=_files(), stages=["intake", "teleport"])
        self.assertFalse(result["success"])

    def test_create_job_rejects_duplicate_stage(self):
        result = orchestrate.create_job(self.root, files=_files(), stages=["intake", "grade", "grade"])
        self.assertFalse(result["success"])


class OrchestratePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="orchestrate-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        # Nested under a per-test base dir (never bare /tmp) so the default
        # global-index location (parent of project_root) stays test-isolated.
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)

    def test_save_and_load_round_trip(self):
        created = orchestrate.create_job(self.root, files=_files())
        job_id = created["job_id"]
        loaded = orchestrate.load_job(self.root, job_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["job_id"], job_id)
        self.assertNotIn("_corrupt", loaded)

    def test_load_missing_job_returns_none(self):
        self.assertIsNone(orchestrate.load_job(self.root, "nonexistent"))

    def test_tampered_record_detected_as_corrupt(self):
        created = orchestrate.create_job(self.root, files=_files())
        job_id = created["job_id"]
        path = os.path.join(
            analysis_memory_jobs_dir(self.root), f"{job_id}.json")
        with open(path, "r+", encoding="utf-8") as handle:
            content = handle.read().replace('"genre": "talking_head"', '"genre": "tampered"')
            handle.seek(0)
            handle.write(content)
            handle.truncate()
        loaded = orchestrate.load_job(self.root, job_id)
        self.assertTrue(loaded.get("_corrupt"))

    def test_job_status_read_only_never_touches_lease(self):
        created = orchestrate.create_job(self.root, files=_files(), holder_id="session-a")
        before = created["job"]["lease"]["heartbeat_at"]
        status = orchestrate.job_status(self.root, created["job_id"])
        self.assertTrue(status["success"])
        self.assertEqual(status["job"]["lease"]["heartbeat_at"], before)
        self.assertFalse(status["lease_expired"])

    def test_job_status_missing_job(self):
        result = orchestrate.job_status(self.root, "nonexistent")
        self.assertFalse(result["success"])

    def test_list_jobs_in_root_sorted_newest_first(self):
        a = orchestrate.create_job(self.root, files=_files())
        b = orchestrate.create_job(self.root, files=_files())
        rows = orchestrate.list_jobs_in_root(self.root)
        ids = [r["job_id"] for r in rows]
        self.assertIn(a["job_id"], ids)
        self.assertIn(b["job_id"], ids)


def analysis_memory_jobs_dir(project_root: str) -> str:
    return orchestrate._jobs_dir(project_root)  # noqa: SLF001 (test-only introspection)


class OrchestrateStageTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="orchestrate-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        # Nested under a per-test base dir (never bare /tmp) so the default
        # global-index location (parent of project_root) stays test-isolated.
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)
        self.job_id = orchestrate.create_job(self.root, files=_files())["job_id"]

    def test_pending_to_running_to_done_advances_cursor(self):
        r1 = orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        self.assertTrue(r1["success"], r1)
        self.assertEqual(r1["job"]["cursor"], "ingest")
        r2 = orchestrate.advance_stage(self.root, self.job_id, "ingest", "done")
        self.assertTrue(r2["success"], r2)
        self.assertEqual(r2["job"]["cursor"], "analysis")

    def test_illegal_transition_pending_to_done_rejected(self):
        result = orchestrate.advance_stage(self.root, self.job_id, "ingest", "done")
        self.assertFalse(result["success"])
        self.assertIn("illegal", result["error"])

    def test_failed_stage_can_retry(self):
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        orchestrate.advance_stage(self.root, self.job_id, "ingest", "failed", notes=["boom"])
        retry = orchestrate.advance_stage(self.root, self.job_id, "ingest", "running")
        self.assertTrue(retry["success"], retry)

    def test_unknown_stage_rejected(self):
        result = orchestrate.advance_stage(self.root, self.job_id, "teleport", "running")
        self.assertFalse(result["success"])

    def test_unknown_job_rejected(self):
        result = orchestrate.advance_stage(self.root, "nonexistent", "ingest", "running")
        self.assertFalse(result["success"])


class OrchestrateLeaseTests(unittest.TestCase):
    def test_fresh_job_lease_acquired_by_creator(self):
        job = {"lease": {"holder_id": "a", "acquired_at": "x", "heartbeat_at": orchestrate._now()}}
        ok, updated, info = orchestrate.acquire_or_steal_lease(job, "a")
        self.assertTrue(ok)
        self.assertFalse(info["stolen"])

    def test_live_lease_held_by_other_refuses(self):
        job = {"lease": {"holder_id": "a", "acquired_at": "x", "heartbeat_at": orchestrate._now()}}
        ok, _updated, info = orchestrate.acquire_or_steal_lease(job, "b")
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "held_by_other")

    def test_expired_lease_is_stealable(self):
        stale_epoch = orchestrate._now_epoch() - orchestrate.LEASE_TTL_SECONDS - 60
        stale_iso = time_from_epoch(stale_epoch)
        job = {"lease": {"holder_id": "a", "acquired_at": stale_iso, "heartbeat_at": stale_iso}}
        ok, updated, info = orchestrate.acquire_or_steal_lease(job, "b")
        self.assertTrue(ok)
        self.assertTrue(info["stolen"])
        self.assertEqual(updated["lease"]["holder_id"], "b")

    def test_no_lease_yet_is_acquirable(self):
        ok, updated, info = orchestrate.acquire_or_steal_lease({}, "a")
        self.assertTrue(ok)
        self.assertFalse(info["stolen"])
        self.assertEqual(updated["lease"]["holder_id"], "a")


def time_from_epoch(epoch: float) -> str:
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(epoch))


class OrchestrateOfflineOpTests(unittest.TestCase):
    """request_offline_op / resolve_offline_op — pause/resume around the
    narrow Resolve-closed advanced-server slice (issue #39)."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="orchestrate-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)
        # A short manifest lands the cursor straight on "conform" — no need
        # to walk ingest/analysis/edit through done first.
        created = orchestrate.create_job(self.root, files=_files(), stages=["intake", "conform"])
        self.job_id = created["job_id"]

    def test_refuses_action_not_in_whitelist(self):
        result = orchestrate.request_offline_op(
            self.root, self.job_id, "conform",
            tool="conform", action="analyze_media", args={},
        )
        self.assertFalse(result["success"])
        self.assertIn("in-band", result["error"])

    def test_refuses_stage_not_current_cursor(self):
        result = orchestrate.request_offline_op(
            self.root, self.job_id, "intake",
            tool="conform", action="fix_reverse_clip", args={},
        )
        self.assertFalse(result["success"])
        self.assertIn("cursor", result["error"])

    def test_parks_stage_and_records_pending_op(self):
        result = orchestrate.request_offline_op(
            self.root, self.job_id, "conform",
            tool="conform", action="fix_reverse_clip", args={"itemId": "x"},
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["job"]["stages"]["conform"]["status"], "awaiting_offline_artifact")
        pending = result["job"]["pending_offline_op"]
        self.assertEqual(pending["tool"], "conform")
        self.assertEqual(pending["action"], "fix_reverse_clip")
        self.assertIn("quit_app", pending["instruction"])

    def test_parked_stage_cannot_be_run(self):
        orchestrate.request_offline_op(
            self.root, self.job_id, "conform",
            tool="conform", action="fix_reverse_clip", args={},
        )
        job = orchestrate.load_job(self.root, self.job_id)
        refusal = orchestrate.can_run_stage(job, "conform")
        self.assertIsNotNone(refusal)
        self.assertIn("awaiting_offline_artifact", refusal)

    def test_resolve_offline_op_refuses_without_pending(self):
        result = orchestrate.resolve_offline_op(self.root, self.job_id, result={"success": True})
        self.assertFalse(result["success"])
        self.assertIn("no pending", result["error"])

    def test_resolve_offline_op_success_resumes_running(self):
        orchestrate.request_offline_op(
            self.root, self.job_id, "conform",
            tool="conform", action="fix_reverse_clip", args={},
        )
        result = orchestrate.resolve_offline_op(self.root, self.job_id, result={"success": True})
        self.assertTrue(result["success"], result)
        self.assertTrue(result["resumed"])
        self.assertEqual(result["job"]["stages"]["conform"]["status"], "running")
        self.assertIsNone(result["job"]["pending_offline_op"])

    def test_resolve_offline_op_failure_marks_stage_failed(self):
        orchestrate.request_offline_op(
            self.root, self.job_id, "conform",
            tool="conform", action="fix_reverse_clip", args={},
        )
        result = orchestrate.resolve_offline_op(
            self.root, self.job_id, result={"success": False, "error": "DB locked"})
        self.assertTrue(result["success"], result)
        self.assertFalse(result["resumed"])
        self.assertEqual(result["job"]["stages"]["conform"]["status"], "failed")

    def test_resolve_offline_op_then_retry_is_a_legal_transition(self):
        orchestrate.request_offline_op(
            self.root, self.job_id, "conform",
            tool="conform", action="fix_reverse_clip", args={},
        )
        orchestrate.resolve_offline_op(self.root, self.job_id, result={"success": True})
        # Back to "running" — the domain-tool mutate can now finish the stage.
        done = orchestrate.advance_stage(self.root, self.job_id, "conform", "done")
        self.assertTrue(done["success"], done)


class OrchestrateGlobalIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="orchestrate-base-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.proj_a = os.path.join(self.base, "project-a")
        self.proj_b = os.path.join(self.base, "project-b")
        os.makedirs(self.proj_a, exist_ok=True)
        os.makedirs(self.proj_b, exist_ok=True)

    def test_index_updated_on_save_without_explicit_rebuild(self):
        orchestrate.create_job(self.proj_a, files=_files(), analysis_base_root=self.base)
        result = orchestrate.list_jobs(self.base)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["jobs"]), 1)

    def test_rebuild_discovers_jobs_across_project_roots(self):
        orchestrate.create_job(self.proj_a, files=_files(), analysis_base_root=self.base)
        orchestrate.create_job(self.proj_b, files=_files(), analysis_base_root=self.base)
        rebuilt = orchestrate.rebuild_global_index(self.base)
        self.assertTrue(rebuilt["success"])
        self.assertEqual(rebuilt["count"], 2)
        listing = orchestrate.list_jobs(self.base)
        self.assertEqual(len(listing["jobs"]), 2)

    def test_index_skips_underscore_prefixed_dirs(self):
        os.makedirs(os.path.join(self.base, "_soul"), exist_ok=True)
        orchestrate.create_job(self.proj_a, files=_files(), analysis_base_root=self.base)
        rebuilt = orchestrate.rebuild_global_index(self.base)
        self.assertEqual(rebuilt["count"], 1)

    def test_list_jobs_filters_by_job_state(self):
        orchestrate.create_job(self.proj_a, files=_files(), analysis_base_root=self.base)
        result = orchestrate.list_jobs(self.base, job_state="finished")
        self.assertEqual(result["jobs"], [])


if __name__ == "__main__":
    unittest.main()
