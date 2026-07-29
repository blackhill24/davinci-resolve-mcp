"""A MediaPoolItem read must only call methods MediaPoolItem actually has.

`list_media_pool_clips` read a clip's duration with `clip.GetDuration()`. That
method is on **TimelineItem** — `docs/reference/resolve_scripting_api.txt` lists
it under TimelineItem, and `dir(MediaPoolItem)` on Studio 21.0.2.4 has no such
entry. The bridge fabricates any missing attribute as ``None``, so the call
raised ``TypeError: 'NoneType' object is not callable`` straight out of the
resource, for every project whose root folder holds at least one clip.

The offline suite could not catch it because a `MagicMock` clip answers
`GetDuration()` happily. `ResolveBridgeDouble` fabricates ``None`` the way the
real bridge does, so this test fails on the original code and passes on the fix.
"""

from __future__ import annotations

import unittest
from unittest import mock

from src.granular import media_pool as media_pool_mod
from tests.bridge_double import ResolveBridgeDouble, call_names

# Exactly what dir(MediaPoolItem) reports on Studio 21.0.2.4 for the two reads
# this resource performs. GetDuration is deliberately absent — that is the point.
_CLIP_METHODS = {
    "GetName": "shot_01.mov",
    "GetClipProperty": lambda key=None: {"Duration": "00:00:03:00", "FPS": 24.0}.get(key),
}


def _project_with_clips(clips):
    folder = ResolveBridgeDouble({"GetClipList": lambda: clips}, name="Folder")
    pool = ResolveBridgeDouble({"GetRootFolder": lambda: folder}, name="MediaPool")
    return ResolveBridgeDouble({"GetMediaPool": lambda: pool}, name="Project")


class ListMediaPoolClipsTest(unittest.TestCase):
    def _run(self, clips):
        project = _project_with_clips(clips)
        with mock.patch.object(media_pool_mod, "get_current_project",
                               return_value=(None, project)):
            return media_pool_mod.list_media_pool_clips()

    def test_a_clip_row_is_returned_without_calling_a_timelineitem_method(self):
        clip = ResolveBridgeDouble(_CLIP_METHODS, name="MediaPoolItem")
        rows = self._run([clip])
        self.assertEqual(
            [{"name": "shot_01.mov", "duration": "00:00:03:00", "fps": 24.0}], rows)
        self.assertNotIn(
            "GetDuration", call_names(clip),
            "GetDuration is a TimelineItem method; on a MediaPoolItem the bridge "
            "fabricates None and the call raises TypeError")

    def test_every_clip_is_reported(self):
        clips = [ResolveBridgeDouble(_CLIP_METHODS, name="MediaPoolItem") for _ in range(3)]
        self.assertEqual(3, len(self._run(clips)))

    def test_an_empty_root_folder_is_not_an_error(self):
        self.assertEqual([{"info": "No clips found in the root folder"}], self._run([]))


if __name__ == "__main__":
    unittest.main()
