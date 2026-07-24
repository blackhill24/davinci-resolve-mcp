"""Render-scratch lifecycle helper (issue #92)."""

import os
import tempfile
import unittest
from unittest import mock

from tests import render_scratch


class RenderScratchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="render-scratch-test-")
        # Point the helper's ~/Videos at a disposable dir.
        self._patch = mock.patch.object(render_scratch, "_VIDEOS", self._tmp)
        self._patch.start()
        os.environ.pop("DRM_KEEP_RENDERS", None)

    def tearDown(self):
        self._patch.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        os.environ.pop("DRM_KEEP_RENDERS", None)

    def test_make_render_dir_creates_under_videos(self):
        d = render_scratch.make_render_dir("drm-x-render-")
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(os.path.dirname(d), self._tmp)
        self.assertTrue(os.path.basename(d).startswith("drm-x-render-"))

    def test_make_render_dir_sweeps_prior_run(self):
        stale = tempfile.mkdtemp(prefix="drm-x-render-", dir=self._tmp)
        keep = tempfile.mkdtemp(prefix="drm-other-", dir=self._tmp)  # different prefix, spared
        fresh = render_scratch.make_render_dir("drm-x-render-")
        self.assertFalse(os.path.exists(stale), "prior run's dir should be swept")
        self.assertTrue(os.path.isdir(fresh))
        self.assertTrue(os.path.isdir(keep), "unrelated dirs must survive the sweep")

    def test_sweep_spares_the_keep_path(self):
        current = render_scratch.make_render_dir("drm-x-render-")
        render_scratch.sweep_stale("drm-x-render-", keep=current)
        self.assertTrue(os.path.isdir(current))

    def test_cleanup_removes_dir(self):
        d = render_scratch.make_render_dir("drm-x-render-")
        render_scratch.cleanup_render_dir(d)
        self.assertFalse(os.path.exists(d))

    def test_cleanup_keeps_dir_when_env_set(self):
        d = render_scratch.make_render_dir("drm-x-render-")
        os.environ["DRM_KEEP_RENDERS"] = "1"
        render_scratch.cleanup_render_dir(d)
        self.assertTrue(os.path.isdir(d))

    def test_no_videos_dir_falls_back_to_tempdir(self):
        with mock.patch.object(render_scratch, "_VIDEOS", "/nonexistent-videos-xyz"):
            d = render_scratch.make_render_dir("drm-x-render-")
            self.assertTrue(os.path.isdir(d))
            self.assertNotIn("/nonexistent-videos-xyz", d)
        import shutil
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
