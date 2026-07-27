"""#143 findings 8 and 9: a tool must not promise what it discards.

Both are the same shape — a declared parameter the underlying Resolve API has no
slot for, or a payload fetched and then thrown away:

- `timeline_get_current_clip_thumbnail` declared and documented `width`/`height`
  (GetCurrentClipThumbnailImage() takes no arguments) and returned only
  `{"success": true, "has_data": true}`, so a caller migrating from the compound
  `timeline_markers(action="get_thumbnail")` lost the image.
- `export_folder(export_type="DRT")` returned "Successfully exported folder ...
  to '/tmp/x.drt'" while DRB content had been written.
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from src.granular import folder as folder_mod
from src.granular import timeline as timeline_mod


def _unwrapped(tool):
    return getattr(tool, "__wrapped__", tool)


class CurrentClipThumbnailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fn = _unwrapped(timeline_mod.timeline_get_current_clip_thumbnail)

    def test_the_phantom_size_parameters_are_gone(self):
        params = inspect.signature(self.fn).parameters
        self.assertNotIn("width", params, "the Resolve API takes no width")
        self.assertNotIn("height", params, "the Resolve API takes no height")

    def test_the_thumbnail_payload_is_returned_not_discarded(self):
        payload = {"width": 320, "height": 180, "format": "RGB", "data": "AAAA"}
        timeline = mock.MagicMock()
        timeline.GetCurrentClipThumbnailImage.return_value = payload
        with mock.patch.object(timeline_mod, "_get_timeline",
                               return_value=(None, timeline, None)):
            result = self.fn()
        self.assertTrue(result["success"])
        self.assertEqual(payload, result["thumbnail"])

    def test_a_missing_thumbnail_explains_itself(self):
        timeline = mock.MagicMock()
        timeline.GetCurrentClipThumbnailImage.return_value = None
        with mock.patch.object(timeline_mod, "_get_timeline",
                               return_value=(None, timeline, None)):
            result = self.fn()
        self.assertFalse(result["success"])
        self.assertIsNone(result["thumbnail"])
        self.assertIn("Color page", result["error"])


class ExportFolderTypeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fn = _unwrapped(folder_mod.export_folder)

    def test_a_non_drb_export_type_is_refused_not_silently_ignored(self):
        # Must refuse BEFORE touching the project — the old code reported a
        # successful DRT export having written DRB.
        with mock.patch.object(folder_mod, "get_current_project") as ctx:
            result = self.fn("Selects", "/tmp/x.drt", export_type="DRT")
            ctx.assert_not_called()
        self.assertIn("Error", result)
        self.assertIn("DRB", result)

    def test_drb_is_accepted_in_any_case_and_when_omitted(self):
        media_pool = mock.MagicMock()
        target = mock.MagicMock()
        target.GetName.return_value = "Selects"
        target.Export.return_value = True
        media_pool.GetRootFolder.return_value = target
        project = mock.MagicMock()
        project.GetMediaPool.return_value = media_pool

        for kwargs in ({}, {"export_type": "DRB"}, {"export_type": "drb"}):
            with mock.patch.object(folder_mod, "get_current_project",
                                   return_value=(None, project)):
                result = self.fn("root", "/tmp/exports/x.drb", **kwargs)
            self.assertIn("Successfully exported", result, kwargs)


if __name__ == "__main__":
    unittest.main()
