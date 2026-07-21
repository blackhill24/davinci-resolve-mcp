"""Offline tests for orchestrate's P2 tool actions (src/server.py):
check_resume, force_replan_stage, approve_gate, plan_stage, revise_stage.

Fingerprints are always passed explicitly (current_fingerprint) so these
tests are deterministic regardless of whether Resolve happens to be running
in this environment.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest

import src.server as s
from src.utils import auto_edit, orchestrate
from tests.test_auto_edit_tool import make_plan


def run(coro):
    return asyncio.run(coro)


def _files():
    return ["/tmp/does-not-need-to-exist-a.mov"]


def _fp(items=10, grade="g1", media="m1"):
    return {"timeline_item_count": items, "grade_version_id": grade, "media_path_set_hash": media}


class OrchestrateGatesToolBase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="orchestrate-gates-tool-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)

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


class CheckResumeToolTests(OrchestrateGatesToolBase):
    def test_no_baseline(self):
        job_id = self._start_job()
        out = run(s.orchestrate("check_resume", {
            "job_id": job_id, "analysis_root": self.root, "current_fingerprint": _fp(),
        }))
        self.assertTrue(out.get("success"), out)
        self.assertFalse(out["drifted"])

    def test_matches_baseline(self):
        job_id = self._start_job()
        fp = _fp()
        self._advance_to_done(job_id, ["ingest"], {"ingest": fp})
        out = run(s.orchestrate("check_resume", {
            "job_id": job_id, "analysis_root": self.root, "current_fingerprint": fp,
        }))
        self.assertFalse(out["drifted"])
        self.assertEqual(out["checked_stage"], "ingest")

    def test_drift_detected(self):
        job_id = self._start_job()
        self._advance_to_done(job_id, ["ingest"], {"ingest": _fp(items=1)})
        out = run(s.orchestrate("check_resume", {
            "job_id": job_id, "analysis_root": self.root, "current_fingerprint": _fp(items=2),
        }))
        self.assertTrue(out["drifted"])

    def test_unknown_job(self):
        out = run(s.orchestrate("check_resume", {
            "job_id": "nonexistent", "analysis_root": self.root, "current_fingerprint": _fp(),
        }))
        self.assertIn("error", out)


class ForceReplanToolTests(OrchestrateGatesToolBase):
    def test_replan_success(self):
        job_id = self._start_job()
        self._advance_to_done(job_id, ["ingest"], {"ingest": _fp()})
        out = run(s.orchestrate("force_replan_stage", {
            "job_id": job_id, "stage": "ingest", "analysis_root": self.root,
        }))
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["job"]["stages"]["ingest"]["status"], "pending")

    def test_replan_refuses_pending_stage(self):
        job_id = self._start_job()
        out = run(s.orchestrate("force_replan_stage", {
            "job_id": job_id, "stage": "ingest", "analysis_root": self.root,
        }))
        self.assertIn("error", out)


class ApproveGateToolTests(OrchestrateGatesToolBase):
    def _deliver_ready_job(self, **kw):
        job_id = self._start_job(**kw)
        fp = _fp()
        self._advance_to_done(
            job_id, ["ingest", "analysis", "edit", "conform", "grade", "audio", "deliver"],
            {"deliver": fp})
        return job_id, fp

    def test_unknown_gate(self):
        job_id, fp = self._deliver_ready_job()
        out = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G99", "analysis_root": self.root, "current_fingerprint": fp,
        }))
        self.assertIn("error", out)

    def test_gate_refuses_when_stage_not_done(self):
        job_id = self._start_job()
        out = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G3", "analysis_root": self.root, "current_fingerprint": _fp(),
        }))
        self.assertIn("error", out)

    def test_g3_standard_mode_token_round_trip(self):
        job_id, fp = self._deliver_ready_job()
        first = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G3", "analysis_root": self.root, "current_fingerprint": fp,
        }))
        self.assertEqual(first.get("status"), "confirmation_required")
        second = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G3", "analysis_root": self.root, "current_fingerprint": fp,
            "confirm_token": first["confirm_token"],
        }))
        self.assertTrue(second.get("success"), second)
        self.assertIsNotNone(second["approved_at"])
        job = orchestrate.load_job(self.root, job_id)
        self.assertIsNotNone(job["stages"]["deliver"]["gate"])

    def test_g3_auto_mode_skips_confirm(self):
        job_id, fp = self._deliver_ready_job(gates="auto")
        out = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G3", "analysis_root": self.root, "current_fingerprint": fp,
        }))
        self.assertTrue(out.get("success"), out)
        self.assertNotEqual(out.get("status"), "confirmation_required")

    def test_g3_drifted_fingerprint_refuses(self):
        job_id, _fp0 = self._deliver_ready_job()
        out = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G3", "analysis_root": self.root,
            "current_fingerprint": _fp(items=999999),
        }))
        self.assertIn("error", out)

    def test_g2_requires_vision_assessment_and_frame(self):
        job_id = self._start_job()
        fp = _fp()
        self._advance_to_done(job_id, ["ingest", "analysis", "edit", "conform", "grade"], {"grade": fp})
        missing = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G2", "analysis_root": self.root, "current_fingerprint": fp,
        }))
        self.assertIn("error", missing)
        first = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G2", "analysis_root": self.root, "current_fingerprint": fp,
            "vision_assessment": "warm, even skin tones", "preview_frame_path": "/tmp/frame.png",
        }))
        self.assertEqual(first.get("status"), "confirmation_required")
        second = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G2", "analysis_root": self.root, "current_fingerprint": fp,
            "vision_assessment": "warm, even skin tones", "preview_frame_path": "/tmp/frame.png",
            "confirm_token": first["confirm_token"],
        }))
        self.assertTrue(second.get("success"), second)

    def test_g3_snapshot_gc_reported(self):
        job_id, fp = self._deliver_ready_job()
        orchestrate.record_snapshot(self.root, job_id, "deliver", "snap-1", kind="timeline_duplicate")
        first = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G3", "analysis_root": self.root, "current_fingerprint": fp,
        }))
        second = run(s.orchestrate("approve_gate", {
            "job_id": job_id, "gate": "G3", "analysis_root": self.root, "current_fingerprint": fp,
            "confirm_token": first["confirm_token"],
        }))
        self.assertTrue(second.get("success"), second)
        self.assertIn("snapshots_cleaned", second)
        job = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job["stages"]["deliver"]["snapshot_ids"], [])


class PlanStageToolTests(OrchestrateGatesToolBase):
    def test_unsupported_stage_refuses(self):
        job_id = self._start_job()
        out = run(s.orchestrate("plan_stage", {
            "job_id": job_id, "stage": "grade", "analysis_root": self.root,
        }))
        self.assertIn("error", out)

    def test_non_talking_head_genre_refuses_with_byo_message(self):
        job_id = self._start_job(genre="documentary")
        out = run(s.orchestrate("plan_stage", {
            "job_id": job_id, "analysis_root": self.root,
        }))
        self.assertIn("error", out)
        self.assertIn("bring-your-own-timeline", out["error"]["message"])

    def test_already_planned_returns_summary(self):
        job_id = self._start_job()
        plan = make_plan(self.root)
        orchestrate.set_stage_foreign_keys(self.root, job_id, "edit", plan_id=plan["plan_id"])
        out = run(s.orchestrate("plan_stage", {
            "job_id": job_id, "analysis_root": self.root,
        }))
        self.assertTrue(out.get("success"), out)
        self.assertTrue(out.get("already_planned"))
        self.assertEqual(out["plan_id"], plan["plan_id"])


class ReviseStageToolTests(OrchestrateGatesToolBase):
    def test_no_plan_yet_refuses(self):
        job_id = self._start_job()
        out = run(s.orchestrate("revise_stage", {
            "job_id": job_id, "analysis_root": self.root, "notes": "drop segment 1",
        }))
        self.assertIn("error", out)

    def test_revision_updates_plan_id_and_voids_gate(self):
        job_id = self._start_job()
        created = auto_edit.create_brief(self.root, files=["/media/a.mp4"])
        plan = make_plan(self.root)
        auto_edit.advance_brief(self.root, created["brief_id"], "ready")
        auto_edit.advance_brief(self.root, created["brief_id"], "planned",
                                 latest_plan_id=plan["plan_id"])
        orchestrate.set_stage_foreign_keys(
            self.root, job_id, "edit", brief_id=created["brief_id"], plan_id=plan["plan_id"])
        fp = _fp()
        self._advance_to_done(job_id, ["ingest", "analysis", "edit"], {"edit": fp})
        orchestrate.record_gate_approval(
            self.root, job_id, "G1",
            {"fingerprint": fp, "mode": "standard", "adopted": False, "forced": False})
        job_before = orchestrate.load_job(self.root, job_id)
        self.assertIsNotNone(job_before["stages"]["edit"]["gate"])

        out = run(s.orchestrate("revise_stage", {
            "job_id": job_id, "analysis_root": self.root,
            "notes": "drop the first segment", "edits": [{"op": "drop", "index": 0}],
        }))
        self.assertTrue(out.get("success"), out)
        self.assertNotEqual(out["plan_id"], plan["plan_id"])

        job_after = orchestrate.load_job(self.root, job_id)
        self.assertEqual(job_after["stages"]["edit"]["foreign_keys"]["plan_id"], out["plan_id"])
        self.assertIsNone(job_after["stages"]["edit"]["gate"])


if __name__ == "__main__":
    unittest.main()
