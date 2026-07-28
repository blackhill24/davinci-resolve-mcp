"""#142 findings 5 & 6: the brain-edits registry writer and the fps convention.

Finding 5 — three defects in `update_brain_edits_registry`:

- a fixed `<path>.tmp` name, while the MCP server and the dashboard are separate
  processes that BOTH write this file and `log_brain_edit` fires on every
  destructive op — two writers interleave on the same temp path and `os.replace`
  publishes a half-merged payload;
- `payload.setdefault("entries", [])` / `payload["registry_path"] = path` assume
  the top-level JSON is a dict, and the surrounding `except` only catches
  `OSError` / `json.JSONDecodeError`;
- `os.makedirs(os.path.dirname(path))` on a single-component relative project
  root yields `makedirs("")` -> `FileNotFoundError`, which the `if not
  project_root` guard does not cover.

Finding 6 — `float(GetSetting("timelineFrameRate"))` where the rest of the
codebase regex-extracts, so a ValueError was swallowed and the metric silently
recorded nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

from src.core import brain_edits


class _FpsTimeline:
    def __init__(self, setting):
        self._setting = setting

    def GetStartFrame(self):
        return 0

    def GetEndFrame(self):
        return 48

    def GetSetting(self, _name):
        return self._setting


class RegistryWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # _registry_path_for takes dirname() of the project root, so the
        # registry lands beside it.
        self.project_root = os.path.join(self.tmp.name, "base", "Example_Project")
        os.makedirs(self.project_root)

    def _update(self, **summary):
        return brain_edits.update_brain_edits_registry(
            project_root=self.project_root,
            project_name="Example Project",
            summary=summary or {"edit_count": 1},
        )

    def test_a_normal_write_round_trips(self):
        result = self._update(edit_count=2)
        self.assertTrue(result["success"])
        with open(result["registry_path"], encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(1, len(payload["entries"]))
        self.assertEqual(2, payload["entries"][0]["edit_count"])

    def test_the_temp_file_is_unique_per_writer(self):
        # A fixed "<path>.tmp" is what let two processes clobber each other.
        from unittest import mock

        seen = []
        real_replace = os.replace

        def spy_replace(src, dst, *a, **k):
            seen.append(str(src))
            return real_replace(src, dst, *a, **k)

        with mock.patch.object(brain_edits.os, "replace", side_effect=spy_replace):
            self._update()
            self._update()

        self.assertEqual(2, len(seen), f"expected two temp writes, saw {seen}")
        self.assertNotEqual(seen[0], seen[1], "temp path must not be a fixed name")
        for path in seen:
            self.assertNotEqual(
                os.path.basename(path),
                brain_edits.REGISTRY_FILENAME + ".tmp",
                "the fixed temp name is the concurrent-clobber bug",
            )
            self.assertIn(str(os.getpid()), path)

    def test_no_temp_file_is_left_behind(self):
        result = self._update()
        leftovers = [
            name for name in os.listdir(os.path.dirname(result["registry_path"]))
            if ".tmp" in name
        ]
        self.assertEqual([], leftovers)

    def test_concurrent_writers_do_not_publish_a_half_merged_payload(self):
        errors = []

        def writer(n):
            try:
                for _ in range(5):
                    self._update(edit_count=n)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([], errors)
        path = brain_edits._registry_path_for(self.project_root)
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)  # must be parseable, never truncated
        self.assertIsInstance(payload["entries"], list)

    def test_a_non_dict_registry_file_is_treated_as_corruption(self):
        path = brain_edits._registry_path_for(self.project_root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for garbage in ("[1, 2, 3]", '"a string"', "42"):
            with self.subTest(garbage=garbage):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(garbage)
                result = self._update()
                self.assertTrue(result["success"], "must not raise AttributeError/TypeError")
                self.assertEqual(1, result["entry_count"])

    def test_a_non_list_entries_value_is_replaced_not_appended_to(self):
        path = brain_edits._registry_path_for(self.project_root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"entries": "not-a-list"}, handle)
        self.assertTrue(self._update()["success"])

    def test_reading_a_non_dict_registry_returns_the_empty_shape(self):
        path = brain_edits._registry_path_for(self.project_root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]")
        self.assertEqual(
            {"entries": [], "registry_path": path},
            brain_edits.read_brain_edits_registry(self.project_root),
        )

    def test_a_single_component_relative_root_does_not_raise(self):
        # dirname("Project") == "" and makedirs("") is FileNotFoundError.
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            result = brain_edits.update_brain_edits_registry(
                project_root="Example_Project",
                project_name="Example Project",
                summary={"edit_count": 1},
            )
        finally:
            os.chdir(cwd)
        self.assertTrue(result["success"])


class TimelineFpsConventionTest(unittest.TestCase):
    def test_a_bare_numeral_still_works(self):
        self.assertEqual(24.0, brain_edits._timeline_fps_or_default(_FpsTimeline("24")))
        self.assertEqual(23.976, brain_edits._timeline_fps_or_default(_FpsTimeline("23.976")))

    def test_a_formatted_setting_is_extracted_not_float_ed(self):
        # This is the case float() raised ValueError on, which the enclosing
        # `except Exception` then swallowed into a silent None metric.
        for setting in ("24 fps", "23.976 DF", "​25"):
            with self.subTest(setting=setting):
                fps = brain_edits._timeline_fps_or_default(_FpsTimeline(setting))
                self.assertIsNotNone(fps)
                self.assertGreater(fps, 0)

    def test_duration_is_computed_rather_than_silently_dropped(self):
        # 49 frames (0..48 inclusive) at 24 fps - the shared, end-inclusive
        # timeline_frame_duration (#141 finding 6). What matters here is that a
        # formatted fps setting yields a NUMBER at all rather than None.
        duration = brain_edits.capture_timeline_duration_seconds(_FpsTimeline("24 fps"))
        self.assertAlmostEqual(49 / 24.0, duration)

    def test_an_unreadable_setting_falls_back_to_the_default(self):
        self.assertEqual(24.0, brain_edits._timeline_fps_or_default(_FpsTimeline(None)))
        self.assertEqual(24.0, brain_edits._timeline_fps_or_default(_FpsTimeline("")))

    def test_a_zero_frame_rate_is_none_not_a_division_by_zero(self):
        self.assertIsNone(brain_edits._timeline_fps_or_default(_FpsTimeline("0")))
        self.assertIsNone(brain_edits.capture_timeline_duration_seconds(_FpsTimeline("0")))


if __name__ == "__main__":
    unittest.main()
