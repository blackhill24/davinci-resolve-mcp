"""Unit tests for orchestrate.py's P2 additions: fingerprints, drift-refuse,
snapshot bookkeeping, and the G1/G2/G3 gate ceremony. No Resolve required —
fingerprints are injected directly (the live probe is server.py's job).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from src.utils import orchestrate


def _files():
    return ["/tmp/does-not-need-to-exist-a.mov"]


def _fp(items=10, grade="g1", media="m1"):
    return {"timeline_item_count": items, "grade_version_id": grade, "media_path_set_hash": media}


class OrchestrateBase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="orchestrate-gates-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)

    def _job_through(self, *, done_stages, fingerprints=None, gates="standard"):
        """Create a job and advance the given stages (in manifest order) to
        done, optionally capturing a fingerprint on each. Returns job_id."""
        created = orchestrate.create_job(self.root, files=_files(), gates=gates)
        job_id = created["job_id"]
        fingerprints = fingerprints or {}
        for stage in done_stages:
            if stage == "intake":
                continue  # already done by create_job
            orchestrate.advance_stage(self.root, job_id, stage, "running")
            orchestrate.advance_stage(self.root, job_id, stage, "done")
            if stage in fingerprints:
                orchestrate.capture_stage_fingerprint(self.root, job_id, stage, fingerprints[stage])
        return job_id


class FingerprintTests(OrchestrateBase):
    def test_fingerprints_equal_identical_dicts(self):
        self.assertTrue(orchestrate.fingerprints_equal(_fp(), _fp()))

    def test_fingerprints_equal_false_on_any_key_diff(self):
        self.assertFalse(orchestrate.fingerprints_equal(_fp(items=10), _fp(items=11)))
        self.assertFalse(orchestrate.fingerprints_equal(_fp(grade="a"), _fp(grade="b")))
        self.assertFalse(orchestrate.fingerprints_equal(_fp(media="a"), _fp(media="b")))

    def test_fingerprints_equal_false_on_non_dict(self):
        self.assertFalse(orchestrate.fingerprints_equal(None, _fp()))
        self.assertFalse(orchestrate.fingerprints_equal(_fp(), None))


class CheckResumeTests(OrchestrateBase):
    def test_no_baseline_yet_never_drifts(self):
        job_id = self._job_through(done_stages=["intake"])
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.check_resume(job, _fp())
        self.assertFalse(result["drifted"])
        self.assertIsNone(result["checked_stage"])

    def test_matching_fingerprint_no_drift(self):
        fp = _fp()
        job_id = self._job_through(done_stages=["intake", "ingest"], fingerprints={"ingest": fp})
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.check_resume(job, fp)
        self.assertFalse(result["drifted"])
        self.assertEqual(result["checked_stage"], "ingest")

    def test_mismatched_fingerprint_drifts(self):
        job_id = self._job_through(
            done_stages=["intake", "ingest"], fingerprints={"ingest": _fp(items=5)})
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.check_resume(job, _fp(items=99))
        self.assertTrue(result["drifted"])
        self.assertEqual(result["checked_stage"], "ingest")

    def test_checks_only_the_last_done_stage_with_a_fingerprint(self):
        # ingest has a stale fingerprint; analysis has the current one — the
        # frontier checkpoint (analysis) is what matters, not ingest's.
        job_id = self._job_through(
            done_stages=["intake", "ingest", "analysis"],
            fingerprints={"ingest": _fp(items=1), "analysis": _fp(items=2)},
        )
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.check_resume(job, _fp(items=2))
        self.assertFalse(result["drifted"])
        self.assertEqual(result["checked_stage"], "analysis")


class ForceReplanTests(OrchestrateBase):
    def test_replan_resets_done_stage_to_pending_and_clears_gate_fingerprint(self):
        job_id = self._job_through(done_stages=["intake", "ingest"], fingerprints={"ingest": _fp()})
        result = orchestrate.force_replan_stage(self.root, job_id, "ingest")
        self.assertTrue(result["success"], result)
        stage = result["job"]["stages"]["ingest"]
        self.assertEqual(stage["status"], "pending")
        self.assertIsNone(stage["fingerprint"])
        self.assertEqual(result["job"]["cursor"], "ingest")

    def test_replan_refuses_on_non_done_stage(self):
        job_id = self._job_through(done_stages=["intake"])
        result = orchestrate.force_replan_stage(self.root, job_id, "ingest")
        self.assertFalse(result["success"])

    def test_replan_unknown_job(self):
        result = orchestrate.force_replan_stage(self.root, "nonexistent", "ingest")
        self.assertFalse(result["success"])


class SnapshotBookkeepingTests(OrchestrateBase):
    def test_snapshot_label_namespaced(self):
        self.assertEqual(orchestrate.snapshot_label("job1", "grade"), "_orch_job1_grade")

    def test_record_snapshot_appends(self):
        job_id = self._job_through(done_stages=["intake"])
        r1 = orchestrate.record_snapshot(self.root, job_id, "ingest", "snap-a", kind="timeline_duplicate")
        self.assertTrue(r1["success"], r1)
        r2 = orchestrate.record_snapshot(self.root, job_id, "ingest", "snap-b", kind="timeline_duplicate")
        ids = [s["id"] for s in r2["job"]["stages"]["ingest"]["snapshot_ids"]]
        self.assertEqual(ids, ["snap-a", "snap-b"])

    def test_record_snapshot_unknown_stage(self):
        job_id = self._job_through(done_stages=["intake"])
        result = orchestrate.record_snapshot(self.root, job_id, "teleport", "x", kind="timeline_duplicate")
        self.assertFalse(result["success"])


class GateEvaluationTests(OrchestrateBase):
    def _edit_done_job(self, *, gates="standard", fp=None):
        fp = fp or _fp()
        job_id = self._job_through(
            done_stages=["intake", "ingest", "analysis", "edit"],
            fingerprints={"edit": fp}, gates=gates,
        )
        return job_id, fp

    def test_gate_refuses_when_stage_not_done(self):
        job_id = self._job_through(done_stages=["intake"])
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.evaluate_gate_request(job, "G1", current_fingerprint=_fp())
        self.assertFalse(result["success"])

    def test_gate_unknown_name(self):
        job_id, fp = self._edit_done_job()
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.evaluate_gate_request(job, "G99", current_fingerprint=fp)
        self.assertFalse(result["success"])

    def test_standard_mode_needs_confirm(self):
        job_id, fp = self._edit_done_job(gates="standard")
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.evaluate_gate_request(job, "G1", current_fingerprint=fp)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["needs_confirm"])

    def test_auto_mode_skips_confirm_but_still_drift_halts(self):
        job_id, fp = self._edit_done_job(gates="auto")
        job = orchestrate.load_job(self.root, job_id)
        ok = orchestrate.evaluate_gate_request(job, "G1", current_fingerprint=fp)
        self.assertTrue(ok["success"])
        self.assertFalse(ok["needs_confirm"])
        drifted = orchestrate.evaluate_gate_request(job, "G1", current_fingerprint=_fp(items=999))
        self.assertFalse(drifted["success"])

    def test_force_bypasses_drift_halt_only(self):
        job_id, fp = self._edit_done_job(gates="standard")
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.evaluate_gate_request(
            job, "G1", current_fingerprint=_fp(items=999), force=True)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["record"]["forced"])

    def test_g2_requires_vision_assessment_and_frame(self):
        job_id = self._job_through(
            done_stages=["intake", "ingest", "analysis", "edit", "conform", "grade"],
            fingerprints={"grade": _fp()},
        )
        job = orchestrate.load_job(self.root, job_id)
        missing_both = orchestrate.evaluate_gate_request(job, "G2", current_fingerprint=_fp())
        self.assertFalse(missing_both["success"])
        missing_frame = orchestrate.evaluate_gate_request(
            job, "G2", current_fingerprint=_fp(), vision_assessment="looks warm and even")
        self.assertFalse(missing_frame["success"])
        ok = orchestrate.evaluate_gate_request(
            job, "G2", current_fingerprint=_fp(),
            vision_assessment="looks warm and even", preview_frame_path="/tmp/frame.png")
        self.assertTrue(ok["success"], ok)

    def test_g2_vision_requirement_survives_force(self):
        job_id = self._job_through(
            done_stages=["intake", "ingest", "analysis", "edit", "conform", "grade"],
            fingerprints={"grade": _fp()},
        )
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.evaluate_gate_request(job, "G2", current_fingerprint=_fp(), force=True)
        self.assertFalse(result["success"])

    def test_adopted_inner_gate_skips_confirm(self):
        job_id, fp = self._edit_done_job(gates="standard")
        orchestrate.set_stage_foreign_keys(
            self.root, job_id, "edit", inner_gate_approved_at="2026-07-21T00:00:00Z")
        job = orchestrate.load_job(self.root, job_id)
        result = orchestrate.evaluate_gate_request(job, "G1", current_fingerprint=fp)
        self.assertTrue(result["success"], result)
        self.assertFalse(result["needs_confirm"])
        self.assertTrue(result["record"]["adopted"])

    def test_already_approved_matching_fingerprint_is_idempotent_in_standard_mode(self):
        job_id, fp = self._edit_done_job(gates="standard")
        first = orchestrate.record_gate_approval(
            self.root, job_id, "G1", {"fingerprint": fp, "mode": "standard", "adopted": False, "forced": False})
        self.assertTrue(first["success"], first)
        job = orchestrate.load_job(self.root, job_id)
        second = orchestrate.evaluate_gate_request(job, "G1", current_fingerprint=fp)
        self.assertTrue(second["success"])
        self.assertTrue(second.get("already_approved"))

    def test_paranoid_mode_never_short_circuits(self):
        job_id, fp = self._edit_done_job(gates="paranoid")
        orchestrate.record_gate_approval(
            self.root, job_id, "G1", {"fingerprint": fp, "mode": "paranoid", "adopted": False, "forced": False})
        job = orchestrate.load_job(self.root, job_id)
        second = orchestrate.evaluate_gate_request(job, "G1", current_fingerprint=fp)
        self.assertTrue(second["success"])
        self.assertFalse(second.get("already_approved"))
        self.assertTrue(second["needs_confirm"])

    def test_stale_approval_voids_and_reopens(self):
        job_id, fp = self._edit_done_job(gates="standard")
        orchestrate.record_gate_approval(
            self.root, job_id, "G1", {"fingerprint": fp, "mode": "standard", "adopted": False, "forced": False})
        job = orchestrate.load_job(self.root, job_id)
        drifted_fp = _fp(items=12345)
        result = orchestrate.evaluate_gate_request(job, "G1", current_fingerprint=drifted_fp)
        # Drift check compares against the *edit* stage's own recorded
        # fingerprint (the frontier); a drifted current_fingerprint refuses
        # outright rather than silently re-opening the gate.
        self.assertFalse(result["success"])


class RecordGateApprovalTests(OrchestrateBase):
    def test_record_gate_approval_gcs_snapshots(self):
        job_id = self._job_through(
            done_stages=["intake", "ingest", "analysis", "edit"], fingerprints={"edit": _fp()})
        orchestrate.record_snapshot(self.root, job_id, "edit", "snap-1", kind="timeline_duplicate")
        result = orchestrate.record_gate_approval(
            self.root, job_id, "G1", {"fingerprint": _fp(), "mode": "standard", "adopted": False, "forced": False})
        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["snapshots_to_clean"]), 1)
        self.assertEqual(result["snapshots_to_clean"][0]["id"], "snap-1")
        self.assertEqual(result["job"]["stages"]["edit"]["snapshot_ids"], [])

    def test_record_gate_approval_unknown_gate(self):
        job_id = self._job_through(done_stages=["intake"])
        result = orchestrate.record_gate_approval(self.root, job_id, "G99", {})
        self.assertFalse(result["success"])


class VoidStageGateTests(OrchestrateBase):
    def test_void_clears_gate(self):
        job_id, fp = (lambda: (self._job_through(
            done_stages=["intake", "ingest", "analysis", "edit"], fingerprints={"edit": _fp()}), _fp()))()
        orchestrate.record_gate_approval(
            self.root, job_id, "G1", {"fingerprint": fp, "mode": "standard", "adopted": False, "forced": False})
        result = orchestrate.void_stage_gate(self.root, job_id, "edit")
        self.assertTrue(result["success"], result)
        self.assertIsNone(result["job"]["stages"]["edit"]["gate"])


class ForeignKeysTests(OrchestrateBase):
    def test_set_and_merge_foreign_keys(self):
        job_id = self._job_through(done_stages=["intake"])
        r1 = orchestrate.set_stage_foreign_keys(self.root, job_id, "edit", brief_id="b1")
        self.assertTrue(r1["success"], r1)
        r2 = orchestrate.set_stage_foreign_keys(self.root, job_id, "edit", plan_id="p1")
        fks = r2["job"]["stages"]["edit"]["foreign_keys"]
        self.assertEqual(fks, {"brief_id": "b1", "plan_id": "p1"})

    def test_unknown_stage(self):
        job_id = self._job_through(done_stages=["intake"])
        result = orchestrate.set_stage_foreign_keys(self.root, job_id, "teleport", x=1)
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
