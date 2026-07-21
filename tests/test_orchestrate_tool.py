"""Offline tests for the orchestrate compound tool (src/server.py).

No Resolve required: every P1 action runs against an explicit analysis_root.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest

import src.server as s


def run(coro):
    return asyncio.run(coro)


def _files():
    return ["/tmp/does-not-need-to-exist-a.mov"]


class OrchestrateToolTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="orchestrate-tool-")
        self.addCleanup(shutil.rmtree, self.base, True)
        # Nested under a per-test base dir (never bare /tmp) so the default
        # global-index location (parent of analysis_root) stays test-isolated.
        self.root = os.path.join(self.base, "project")
        os.makedirs(self.root, exist_ok=True)
        # start_job's ffprobe-adjacent pre-flight only checks existence; give it
        # a real (empty) file so the offline path doesn't need Resolve or ffprobe.
        self.media = os.path.join(self.root, "clip.mov")
        with open(self.media, "wb"):
            pass

    def test_start_job_rejects_nonexistent_analysis_root(self):
        # Environment-independent: whether or not Resolve happens to be open,
        # an explicit analysis_root that isn't a real directory must refuse
        # rather than silently falling back to whatever project is current.
        out = run(s.orchestrate("start_job", {
            "files": [self.media], "analysis_root": "/nonexistent/does-not-exist",
        }))
        self.assertIn("error", out)

    def test_start_job_success(self):
        out = run(s.orchestrate("start_job", {
            "files": [self.media], "analysis_root": self.root,
        }))
        self.assertTrue(out.get("success"), out)
        self.assertEqual(out["job"]["cursor"], "ingest")
        self.assertEqual(out["job"]["stages"]["intake"]["status"], "done")

    def test_start_job_rejects_missing_file(self):
        out = run(s.orchestrate("start_job", {
            "files": ["/tmp/definitely-not-here.mov"], "analysis_root": self.root,
        }))
        self.assertIn("error", out)

    def test_job_status_round_trip(self):
        created = run(s.orchestrate("start_job", {
            "files": [self.media], "analysis_root": self.root,
        }))
        status = run(s.orchestrate("job_status", {
            "job_id": created["job_id"], "analysis_root": self.root,
        }))
        self.assertTrue(status.get("success"), status)
        self.assertEqual(status["job"]["job_id"], created["job_id"])

    def test_job_status_unknown_job(self):
        out = run(s.orchestrate("job_status", {
            "job_id": "nonexistent", "analysis_root": self.root,
        }))
        self.assertIn("error", out)

    def test_list_jobs_via_analysis_root(self):
        run(s.orchestrate("start_job", {"files": [self.media], "analysis_root": self.root}))
        out = run(s.orchestrate("list_jobs", {"analysis_root": self.root}))
        self.assertTrue(out.get("success"), out)
        self.assertEqual(len(out["jobs"]), 1)

    def test_unknown_action(self):
        out = run(s.orchestrate("explode", {"analysis_root": self.root}))
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
