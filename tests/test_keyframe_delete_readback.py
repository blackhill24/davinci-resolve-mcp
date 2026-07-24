"""Keyframe edits must honour DeleteKeyframe(), not just AddKeyframe().

#111 finding 6: modify_keyframe() and set_keyframe_interpolation() are
delete-then-re-add operations, but the bool returned by DeleteKeyframe was
discarded and success was derived from AddKeyframe alone. When the delete fails
and the add succeeds, the item is left carrying BOTH the old and the new
keyframe — silent animation corruption — while the caller is told the edit
succeeded.

Secondary, same finding: set_keyframe_interpolation() left `value` as None when
the property read came back empty and passed it straight into
AddKeyframe(property, frame, None, ...) with no guard — after the old keyframe
had already been deleted.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.granular import timeline_item as ti  # noqa: E402

ITEM_ID = "item-1"
PROP = "ZoomX"


class KeyframeItemStub:
    """Minimal TimelineItem exposing only what the keyframe paths call."""

    def __init__(self, *, frames=(10,), delete_ok=True, add_ok=True, value=1.0):
        self.keyframes = list(frames)
        self.delete_ok = delete_ok
        self.add_ok = add_ok
        self.value = value
        self.added = []
        self.delete_calls = []

    def GetUniqueId(self):
        return ITEM_ID

    def GetStart(self):
        return 0

    def GetEnd(self):
        return 1000

    def GetKeyframeCount(self, _property_name):
        return len(self.keyframes)

    def GetKeyframeAtIndex(self, _property_name, index):
        return {"frame": self.keyframes[index]}

    def GetPropertyAtKeyframeIndex(self, _property_name, _index):
        return self.value

    def DeleteKeyframe(self, property_name, frame):
        self.delete_calls.append((property_name, frame))
        if self.delete_ok:
            if frame in self.keyframes:
                self.keyframes.remove(frame)
            return True
        return False

    def AddKeyframe(self, property_name, frame, value, *interpolation):
        if not self.add_ok:
            return False
        self.added.append((property_name, frame, value, interpolation))
        self.keyframes.append(frame)
        return True


class TimelineStub:
    def __init__(self, item):
        self._item = item

    def GetTrackCount(self, track_type):
        return 1 if track_type == "video" else 0

    def GetItemListInTrack(self, track_type, _index):
        return [self._item] if track_type == "video" else []


class ProjectStub:
    def __init__(self, timeline):
        self._timeline = timeline

    def GetCurrentTimeline(self):
        return self._timeline


def _patch_project(item):
    return mock.patch.object(
        ti, "get_current_project", return_value=(object(), ProjectStub(TimelineStub(item)))
    )


class ModifyKeyframeDeleteReadbackTest(unittest.TestCase):
    def test_failed_delete_does_not_fork_the_keyframe_when_moving(self):
        """The finding: delete fails, add succeeds, item keeps BOTH keyframes."""
        item = KeyframeItemStub(frames=(10,), delete_ok=False)

        with _patch_project(item):
            result = ti.modify_keyframe(ITEM_ID, PROP, 10, new_frame=20)

        self.assertIn("Failed to move keyframe", result)
        self.assertIn("could not delete", result)
        self.assertEqual([], item.added, "must not add a second keyframe after a failed delete")
        self.assertEqual([10], item.keyframes, "the item must be left exactly as it was")

    def test_failed_delete_does_not_fork_the_keyframe_when_changing_value(self):
        item = KeyframeItemStub(frames=(10,), delete_ok=False)

        with _patch_project(item):
            result = ti.modify_keyframe(ITEM_ID, PROP, 10, new_value=2.5)

        self.assertIn("Failed to update keyframe value", result)
        self.assertIn("could not delete", result)
        self.assertEqual([], item.added)
        self.assertEqual([10], item.keyframes)

    def test_successful_move_still_works(self):
        item = KeyframeItemStub(frames=(10,), value=1.0)

        with _patch_project(item):
            result = ti.modify_keyframe(ITEM_ID, PROP, 10, new_frame=20)

        self.assertIn("Successfully moved keyframe", result)
        self.assertEqual([(PROP, 10)], item.delete_calls)
        self.assertEqual([20], item.keyframes)
        self.assertEqual(1.0, item.added[0][2], "carries the existing value forward")

    def test_successful_value_change_still_works(self):
        item = KeyframeItemStub(frames=(10,))

        with _patch_project(item):
            result = ti.modify_keyframe(ITEM_ID, PROP, 10, new_value=2.5)

        self.assertIn("Successfully updated keyframe value", result)
        self.assertEqual([(PROP, 10, 2.5, ())], item.added)

    def test_failed_add_after_successful_delete_reports_the_removal(self):
        """Honest reporting: the old keyframe really is gone in this case."""
        item = KeyframeItemStub(frames=(10,), add_ok=False)

        with _patch_project(item):
            result = ti.modify_keyframe(ITEM_ID, PROP, 10, new_frame=20)

        self.assertIn("Failed to move keyframe", result)
        self.assertIn("was removed but could not be re-added", result)


class SetKeyframeInterpolationTest(unittest.TestCase):
    def test_failed_delete_does_not_fork_the_keyframe(self):
        item = KeyframeItemStub(frames=(10,), delete_ok=False)

        with _patch_project(item):
            result = ti.set_keyframe_interpolation(ITEM_ID, PROP, 10, "Bezier")

        self.assertIn("Failed to set interpolation", result)
        self.assertIn("could not delete", result)
        self.assertEqual([], item.added)
        self.assertEqual([10], item.keyframes)

    def test_none_value_is_refused_before_anything_is_deleted(self):
        """Secondary finding: None used to be passed straight into AddKeyframe."""
        item = KeyframeItemStub(frames=(10,), value=None)

        with _patch_project(item):
            result = ti.set_keyframe_interpolation(ITEM_ID, PROP, 10, "Bezier")

        self.assertIn("could not read the current value", result)
        self.assertEqual([], item.delete_calls, "must refuse BEFORE destroying the keyframe")
        self.assertEqual([], item.added)
        self.assertEqual([10], item.keyframes)

    def test_successful_interpolation_change_still_works(self):
        item = KeyframeItemStub(frames=(10,), value=1.0)

        with _patch_project(item):
            result = ti.set_keyframe_interpolation(ITEM_ID, PROP, 10, "Bezier")

        self.assertIn("Successfully set interpolation", result)
        self.assertEqual([(PROP, 10)], item.delete_calls)
        self.assertEqual(1, item.added[0][3][0], "Bezier maps to 1")


if __name__ == "__main__":
    unittest.main()
