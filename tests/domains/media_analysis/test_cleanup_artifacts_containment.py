"""cleanup_artifacts(frames_only=False) must not delete arbitrary directories.

#111 finding 4: `cleanup_artifacts` is a dispatchable media_analysis action whose
frames_only=False branch was a bare `shutil.rmtree(root, ignore_errors=True)` on
any caller-supplied path, validated by nothing but `os.path.isdir`. media_analysis
has no entry in DESTRUCTIVE_ACTIONS_BY_TOOL, and cannot usefully get one: that
registry drives version-on-mutate *timeline* archiving, which cannot recover a
deleted directory tree. So the containment check lives at the call site, and
these tests hold it there.

frames_only=True (the default) was never the problem — it only removes
directories literally named "frames" — and must keep working unchanged.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from src.domains.media_analysis.utils.caps_gating import (  # noqa: E402
    ANALYSIS_DIR_NAME,
    ANALYSIS_REGISTRY_FILENAME,
    HIDDEN_ANALYSIS_DIR_NAME,
)
from src.domains.media_analysis.utils.reports import cleanup_artifacts  # noqa: E402


class CleanupArtifactsContainmentTest(unittest.TestCase):
    def test_refuses_directory_with_no_analysis_markers(self):
        """The finding: an unrelated directory named by the caller is deleted outright."""
        with tempfile.TemporaryDirectory() as tmp:
            victim = os.path.join(tmp, "important-user-data")
            os.makedirs(victim)
            with open(os.path.join(victim, "irreplaceable.txt"), "w", encoding="utf-8") as handle:
                handle.write("please do not delete me")

            result = cleanup_artifacts(victim, frames_only=False)

            self.assertFalse(result["success"])
            self.assertIn("Refusing to recursively delete", result["error"])
            self.assertTrue(os.path.isdir(victim), "guard must not delete the directory")
            self.assertTrue(os.path.isfile(os.path.join(victim, "irreplaceable.txt")))

    def test_refuses_home_directory(self):
        """A bare home dir has no markers, so the guard covers the worst case too."""
        with tempfile.TemporaryDirectory() as tmp:
            result = cleanup_artifacts(tmp, frames_only=False)
            self.assertFalse(result["success"])
            self.assertTrue(os.path.isdir(tmp))

    def test_allows_root_named_for_the_analysis_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, ANALYSIS_DIR_NAME, "some-project")
            os.makedirs(root)
            result = cleanup_artifacts(root, frames_only=False)
            self.assertTrue(result["success"], result.get("error"))
            self.assertEqual(result["removed"], [root])
            self.assertFalse(os.path.isdir(root))

    def test_allows_hidden_analysis_dir_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, HIDDEN_ANALYSIS_DIR_NAME, "some-project")
            os.makedirs(root)
            result = cleanup_artifacts(root, frames_only=False)
            self.assertTrue(result["success"], result.get("error"))
            self.assertFalse(os.path.isdir(root))

    def test_allows_root_carrying_the_registry_file(self):
        """An analysis root at a custom location is identified by its registry."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "custom-location")
            os.makedirs(root)
            with open(os.path.join(root, ANALYSIS_REGISTRY_FILENAME), "w", encoding="utf-8") as handle:
                handle.write("{}")
            result = cleanup_artifacts(root, frames_only=False)
            self.assertTrue(result["success"], result.get("error"))
            self.assertFalse(os.path.isdir(root))

    def test_allows_root_carrying_analysis_reports(self):
        """A half-built root with no registry is still identified by its reports."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "custom-location")
            clip_dir = os.path.join(root, "clips", "clip-a")
            os.makedirs(clip_dir)
            with open(os.path.join(clip_dir, "analysis.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            result = cleanup_artifacts(root, frames_only=False)
            self.assertTrue(result["success"], result.get("error"))
            self.assertFalse(os.path.isdir(root))

    def test_frames_only_is_unchanged_and_needs_no_markers(self):
        """The safe default keeps working on any directory — it only removes frames/."""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "no-markers-here")
            frames = os.path.join(root, "clips", "clip-a", "frames")
            os.makedirs(frames)
            with open(os.path.join(frames, "0001.png"), "w", encoding="utf-8") as handle:
                handle.write("x")
            keep = os.path.join(root, "clips", "clip-a", "analysis.json")
            with open(keep, "w", encoding="utf-8") as handle:
                handle.write("{}")

            result = cleanup_artifacts(root)

            self.assertTrue(result["success"])
            self.assertEqual(result["removed"], [frames])
            self.assertFalse(os.path.isdir(frames))
            self.assertTrue(os.path.isfile(keep), "frames_only must not touch reports")
            self.assertTrue(os.path.isdir(root))

    def test_missing_root_still_reports_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = cleanup_artifacts(os.path.join(tmp, "nope"), frames_only=False)
            self.assertFalse(result["success"])
            self.assertIn("not found", result["error"])


if __name__ == "__main__":
    unittest.main()
