"""Refusal and error branches of the `timeline` tool (#121 task 1).

`src/domains/timeline_edit/actions.py` is the biggest domain module in the repo
(2878 statements) and its live harnesses cover the happy paths. The missing
statements are the branches that only run when Resolve says no — which is
exactly what a faithful double makes testable, and exactly where the bugs in
#110 and #111 lived.

Every double here is `ResolveBridgeDouble`, never a `MagicMock`: on a MagicMock
`dir()` lists only attributes the test happened to touch, so `_has_method()`
reports every unconfigured method as missing, the capability gate closes, and
the test asserts on a fallback envelope instead of the branch it was written for
(#119). See tests/bridge_double.py.

The assertions pin the *specific* refusal, not merely that an error came back —
"error is in the result" would hold for any of them (#121 §3).
"""
from __future__ import annotations

import unittest
from unittest import mock

# Import src.server first: timeline_edit.actions imports back into it, so
# importing the domain module cold hits a partially-initialised src.server.
import src.server  # noqa: F401
import src.domains.timeline_edit.actions as timeline_edit
from tests._error_envelope_helpers import assert_error_mentions, err_code
from tests.bridge_double import ResolveBridgeDouble


def _project(**methods):
    return ResolveBridgeDouble(methods=methods, name="project")


def _timeline(**methods):
    return ResolveBridgeDouble(methods=methods, name="timeline")


class _ConnectedTo:
    """Patch `timeline_edit._check` to hand back a given project double."""

    def __init__(self, project):
        self._project = project
        self._patch = None

    def __enter__(self):
        pm = ResolveBridgeDouble(methods={}, name="projectManager")
        self._patch = mock.patch.object(
            timeline_edit, "_check", return_value=(pm, self._project, None)
        )
        self._patch.start()
        return self._project

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class NotConnectedTest(unittest.TestCase):
    def test_the_connection_error_is_returned_verbatim(self):
        refusal = {"error": {"message": "Not connected to DaVinci Resolve. Is Resolve running?",
                             "code": "NOT_CONNECTED", "category": "not_connected"}}
        with mock.patch.object(timeline_edit, "_check", return_value=(None, None, refusal)):
            result = timeline_edit.timeline("get_current")
        self.assertEqual(refusal, result)

    def test_a_busy_resolve_refuses_before_touching_the_project(self):
        refusal = {"error": {"message": "Resolve is busy with a long operation: render",
                             "code": "RESOLVE_BUSY", "category": "busy"}}
        with mock.patch.object(timeline_edit, "_check", return_value=(None, None, refusal)):
            result = timeline_edit.timeline("set_name", {"name": "x"})
        self.assertEqual("RESOLVE_BUSY", err_code(result))


class NoCurrentTimelineTest(unittest.TestCase):
    """Everything past the "needs a current timeline" line must refuse cleanly.

    `GetCurrentTimeline` returning None is the ordinary state right after a
    project opens, so this branch runs in the field constantly.
    """

    ACTIONS_NEEDING_A_TIMELINE = [
        "get_current", "get_name", "set_name", "get_start_frame", "get_end_frame",
        "get_start_timecode", "clip_where",
    ]

    def test_every_timeline_scoped_action_refuses_without_one(self):
        project = _project(GetCurrentTimeline=lambda: None)
        with _ConnectedTo(project):
            for action in self.ACTIONS_NEEDING_A_TIMELINE:
                with self.subTest(action=action):
                    assert_error_mentions(
                        self, timeline_edit.timeline(action, {"name": "x"}), "current timeline"
                    )

    def test_an_action_that_does_not_need_one_still_works(self):
        # The converse: if `list` also refused, the assertions above would be
        # measuring a dead tool rather than a guarded branch.
        project = _project(
            GetCurrentTimeline=lambda: None,
            GetTimelineCount=lambda: 1,
            GetTimelineByIndex=lambda i: _timeline(
                GetName=lambda: "cut_v3", GetUniqueId=lambda: "tl-1"
            ),
        )
        with _ConnectedTo(project):
            result = timeline_edit.timeline("list")
        self.assertEqual([{"name": "cut_v3", "id": "tl-1", "index": 1}], result["timelines"])


class SetCurrentRefusalTest(unittest.TestCase):
    def test_an_unmatched_name_is_refused_by_name(self):
        project = _project(GetTimelineCount=lambda: 0, GetCurrentTimeline=lambda: None)
        with _ConnectedTo(project):
            result = timeline_edit.timeline("set_current", {"name": "no-such-timeline"})
        assert_error_mentions(self, result, "no-such-timeline")

    def test_an_unmatched_id_is_refused_by_id(self):
        project = _project(GetTimelineCount=lambda: 0, GetCurrentTimeline=lambda: None)
        with _ConnectedTo(project):
            result = timeline_edit.timeline("set_current", {"id": "tl-does-not-exist"})
        assert_error_mentions(self, result, "tl-does-not-exist")

    def test_a_missing_index_is_a_parameter_refusal(self):
        project = _project(GetCurrentTimeline=lambda: None)
        with _ConnectedTo(project):
            result = timeline_edit.timeline("set_current", {})
        assert_error_mentions(self, result, "index")

    def test_a_zero_index_is_refused_by_the_range_check(self):
        # 1-based API: index 0 is not "the first timeline", it is invalid.
        project = _project(GetCurrentTimeline=lambda: None)
        with _ConnectedTo(project):
            result = timeline_edit.timeline("set_current", {"index": 0})
        assert_error_mentions(self, result, "index")

    def test_an_out_of_range_index_names_the_index(self):
        project = _project(
            GetCurrentTimeline=lambda: None,
            GetTimelineByIndex=lambda i: None,
        )
        with _ConnectedTo(project):
            result = timeline_edit.timeline("set_current", {"index": 99})
        assert_error_mentions(self, result, "99")

    def test_a_matched_index_reports_what_resolve_returned(self):
        # SetCurrentTimeline returning False must NOT be reported as success —
        # the Resolve API reports failure by return value (#111 findings 5/6).
        target = _timeline(GetName=lambda: "cut_v3", GetUniqueId=lambda: "tl-1")
        project = _project(
            GetCurrentTimeline=lambda: None,
            GetTimelineByIndex=lambda i: target,
            SetCurrentTimeline=lambda tl: False,
        )
        with _ConnectedTo(project):
            result = timeline_edit.timeline("set_current", {"index": 1})
        self.assertFalse(result["success"])


class ParameterRefusalTest(unittest.TestCase):
    """Validation branches, which run before any Resolve call at all."""

    def setUp(self):
        self.timeline = _timeline(
            GetName=lambda: "cut_v3",
            GetUniqueId=lambda: "tl-1",
            GetStartFrame=lambda: 0,
            GetEndFrame=lambda: 100,
            GetStartTimecode=lambda: "01:00:00:00",
            SetName=lambda name: True,
        )
        self.project = _project(GetCurrentTimeline=lambda: self.timeline)

    def test_set_name_requires_a_non_empty_name(self):
        with _ConnectedTo(self.project):
            for params in ({}, {"name": ""}, {"name": "   "}):
                with self.subTest(params=params):
                    assert_error_mentions(self, timeline_edit.timeline("set_name", params), "name")

    def test_set_start_timecode_requires_a_timecode(self):
        with _ConnectedTo(self.project):
            assert_error_mentions(self, timeline_edit.timeline("set_start_timecode", {}), "timecode")

    def test_add_track_requires_a_track_type(self):
        with _ConnectedTo(self.project):
            assert_error_mentions(self, timeline_edit.timeline("add_track", {}), "track_type")

    def test_get_items_rejects_an_unknown_track_type(self):
        with _ConnectedTo(self.project):
            result = timeline_edit.timeline("get_items", {"track_type": "hologram"})
        assert_error_mentions(self, result, "track_type")

    def test_a_valid_call_still_reaches_the_api(self):
        # Guards the guards: if validation refused everything, the refusals above
        # would prove nothing about the parameter being validated.
        with _ConnectedTo(self.project):
            result = timeline_edit.timeline("set_name", {"name": "cut_v4"})
        self.assertTrue(result["success"])


class UnknownActionTest(unittest.TestCase):
    def test_an_unknown_action_lists_the_valid_ones(self):
        # Needs a current timeline: the unknown-action fallback sits at the END
        # of the dispatch, so with no timeline open a typo reports "No current
        # timeline" instead of the valid-action list an agent recovers from.
        project = _project(GetCurrentTimeline=lambda: _timeline(GetName=lambda: "cut_v3"))
        with _ConnectedTo(project):
            result = timeline_edit.timeline("teleport_clips")
        message = assert_error_mentions(self, result, "teleport_clips")
        # The list is what an agent reads to recover from a typo — it must be there.
        self.assertIn("set_current", message)
        self.assertIn("get_items", message)

    def test_action_help_needs_no_connection_at_all(self):
        # It is answered before _check(), so a disconnected Resolve must not
        # stop an agent from discovering the surface.
        with mock.patch.object(
            timeline_edit, "_check",
            side_effect=AssertionError("action_help must not reach _check()"),
        ):
            result = timeline_edit.timeline("action_help", {"action": "set_current"})
        self.assertNotIn("error", result)


if __name__ == "__main__":
    unittest.main()
