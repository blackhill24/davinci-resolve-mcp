"""ensure_timeline() must not report success when the frame rate is rejected.

#111 finding 5: the SetSetting("timelineFrameRate", ...) result was discarded
inside a try/except that swallowed everything, and True was returned
unconditionally. Resolve returns False when it rejects the rate (unsupported
value, or a timeline that already carries clips), so the caller was told the
timeline had been created at the requested fps when it was actually created at
the project default. For a conform/delivery tool a silently wrong timeline fps
mis-times everything downstream.

The correct idiom already lived six lines below in the same class:
set_timeline_setting() does `return bool(tl.SetSetting(key, str(value)))`.

Re-applying an unchanged spec must still succeed: `fps` is creation-time only
(Resolve refuses SetSetting("timelineFrameRate") once a timeline exists), so an
already-present timeline is a no-op, not a false failure.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

# src.domains.project_lifecycle.actions and src.server import each other; going
# through src.server first is the house pattern for breaking that cycle
# (see tests/domains/project_lifecycle/test_cloud_settings.py).
import src.server  # noqa: E402,F401

from src.domains.project_lifecycle.actions import _SpecLiveExecutor  # noqa: E402


class TimelineStub:
    def __init__(self, name, *, set_setting_result=True, set_setting_raises=False):
        self._name = name
        self.set_setting_result = set_setting_result
        self.set_setting_raises = set_setting_raises
        self.set_calls = []

    def GetName(self):
        return self._name

    def SetSetting(self, key, value):
        self.set_calls.append((key, value))
        if self.set_setting_raises:
            raise RuntimeError("Resolve rejected the setting")
        return self.set_setting_result


class MediaPoolStub:
    def __init__(self, created):
        self._created = created
        self.create_calls = []

    def CreateEmptyTimeline(self, name):
        self.create_calls.append(name)
        return self._created


class ProjectStub:
    def __init__(self, timelines, media_pool):
        self._timelines = list(timelines)
        self._media_pool = media_pool

    def GetTimelineCount(self):
        return len(self._timelines)

    def GetTimelineByIndex(self, index):
        return self._timelines[index - 1]

    def GetMediaPool(self):
        return self._media_pool


def _executor(project):
    """Build the executor without running __init__'s live Resolve lookups."""
    executor = _SpecLiveExecutor.__new__(_SpecLiveExecutor)
    executor._r = object()
    executor._pm = object()
    executor._spec = None
    executor._proj = project
    return executor


class EnsureTimelineFpsReadbackTest(unittest.TestCase):
    def test_rejected_frame_rate_reports_failure(self):
        """The finding: SetSetting returns False, caller was still told True."""
        created = TimelineStub("Conform_01", set_setting_result=False)
        project = ProjectStub([], MediaPoolStub(created))

        ok = _executor(project).ensure_timeline("Conform_01", 23.976)

        self.assertFalse(ok, "a rejected timelineFrameRate must not report success")
        self.assertEqual([("timelineFrameRate", "23.976")], created.set_calls)

    def test_raising_frame_rate_set_reports_failure(self):
        created = TimelineStub("Conform_01", set_setting_raises=True)
        project = ProjectStub([], MediaPoolStub(created))

        self.assertFalse(_executor(project).ensure_timeline("Conform_01", 23.976))

    def test_accepted_frame_rate_reports_success(self):
        created = TimelineStub("Conform_01", set_setting_result=True)
        project = ProjectStub([], MediaPoolStub(created))

        ok = _executor(project).ensure_timeline("Conform_01", 23.976)

        self.assertTrue(ok)
        self.assertEqual([("timelineFrameRate", "23.976")], created.set_calls)

    def test_creation_without_fps_reports_success(self):
        created = TimelineStub("Conform_01")
        project = ProjectStub([], MediaPoolStub(created))

        self.assertTrue(_executor(project).ensure_timeline("Conform_01", None))
        self.assertEqual([], created.set_calls)

    def test_existing_timeline_is_a_noop_and_never_sets_fps(self):
        """Re-applying an unchanged spec must not turn into a false failure.

        fps is creation-time only, so an existing timeline is left alone; if it
        were attempted, Resolve would reject it and the run would report a
        failure on every idempotent re-apply.
        """
        existing = TimelineStub("Conform_01", set_setting_result=False)
        media_pool = MediaPoolStub(None)
        project = ProjectStub([existing], media_pool)

        ok = _executor(project).ensure_timeline("Conform_01", 23.976)

        self.assertTrue(ok)
        self.assertEqual([], existing.set_calls, "fps is creation-time only")
        self.assertEqual([], media_pool.create_calls, "must not re-create an existing timeline")

    def test_failed_creation_still_reports_failure(self):
        project = ProjectStub([], MediaPoolStub(None))
        self.assertFalse(_executor(project).ensure_timeline("Conform_01", 23.976))

    def test_no_project_reports_failure(self):
        self.assertFalse(_executor(None).ensure_timeline("Conform_01", 23.976))


if __name__ == "__main__":
    unittest.main()
