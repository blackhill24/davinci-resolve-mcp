"""Tests for media_pool_item extract_frames (guards; happy path is live-validated)."""
import os
import tempfile
import unittest
from unittest import mock

import src.server as s
import src.domains.media_pool_ingest.actions as _dom_media_pool_ingest


class FakeClip:
    def __init__(self, path):
        self._p = path

    def GetClipProperty(self, key):
        return self._p if key == "File Path" else ""


def _call(clip, params):
    mp = mock.Mock()
    with mock.patch.object(_dom_media_pool_ingest, "_get_mp", return_value=(None, None, mp, None)), \
         mock.patch.object(_dom_media_pool_ingest, "_find_clip", return_value=clip):
        return s.media_pool_item("extract_frames", params)


class ExtractFramesGuardTest(unittest.TestCase):
    def test_no_source_file(self):
        out = _call(FakeClip("/no/such/file.mov"), {"clip_id": "x", "timestamps": [1.0]})
        self.assertIn("error", out)

    def test_no_timestamps(self):
        fd, real = tempfile.mkstemp(suffix=".mov")
        os.close(fd)
        try:
            out = _call(FakeClip(real), {"clip_id": "x"})
            self.assertIn("error", out)
            self.assertIn("timestamps", str(out).lower())
        finally:
            os.remove(real)


if __name__ == "__main__":
    unittest.main()
