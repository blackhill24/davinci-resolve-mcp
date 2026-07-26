"""Voice-isolation capability gates, driven both ways (#119 tasks 4, 5).

Part of the #119 tasks 4/5 migration: every Resolve object here is a faithful
`tests.bridge_double.ResolveBridgeDouble`, not a `MagicMock`. `_has_method` tests
`dir()`, and a MagicMock's `dir()` lists only the children a test has touched — so
every method the test did not explicitly configure reads as absent, the gate closes,
and the test asserts on the degraded result while the supported path never runs. Each gate below is driven in BOTH
directions so a gate stuck open or stuck shut fails here.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import src.server  # noqa: E402,F401  domain modules import back from it
import src.domains.audio_fairlight.actions as audio_fairlight  # noqa: E402
from tests.bridge_double import ResolveBridgeDouble  # noqa: E402


def _double(methods, name="obj"):
    return ResolveBridgeDouble(methods=methods, name=name)


def _flatten(mapping, prefix=""):
    """Every leaf bool in a nested capability map, keyed by dotted path."""
    out = {}
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, prefix=f"{path}."))
        elif isinstance(value, bool):
            out[path] = value
    return out


class AudioCapabilitiesTest(unittest.TestCase):
    """src/domains/audio_fairlight — voice isolation and transcription gates."""

    def _timeline_with(self, timeline_methods, item):
        """A timeline double that can resolve one audio item, plus the gated methods."""
        methods = {"GetTrackCount": 1, "GetItemListInTrack": [item]}
        methods.update(timeline_methods)
        return _double(methods, name="timeline")

    def test_voice_isolation_reports_the_timeline_and_the_item_independently(self):
        item = _double({"GetVoiceIsolationState": {"isEnabled": True, "amount": 50}},
                       name="timelineItem")
        tl = self._timeline_with(
            {"GetVoiceIsolationState": {"isEnabled": False, "amount": 0}}, item)

        report = audio_fairlight._voice_isolation_capabilities(tl, {"track_index": 1})

        self.assertTrue(report["timeline_track"]["get_available"])
        self.assertFalse(report["timeline_track"]["set_available"])
        self.assertEqual({"isEnabled": False, "amount": 0},
                         report["timeline_track"]["state"])
        self.assertTrue(report["item"]["get_available"])
        self.assertFalse(report["item"]["set_available"])
        self.assertEqual({"isEnabled": True, "amount": 50}, report["item"]["state"])

    def test_a_bare_timeline_and_item_report_nothing_available(self):
        item = _double({"GetName": "A001"}, name="timelineItem")
        tl = self._timeline_with({}, item)

        report = audio_fairlight._voice_isolation_capabilities(tl, {"track_index": 1})

        self.assertFalse(report["timeline_track"]["get_available"])
        self.assertFalse(report["timeline_track"]["set_available"])
        self.assertNotIn("state", report["timeline_track"])
        self.assertFalse(report["item"]["get_available"])
        self.assertFalse(report["item"]["set_available"])

    def test_the_setter_gate_is_independent_of_the_getter_gate(self):
        item = _double({"SetVoiceIsolationState": True}, name="timelineItem")
        tl = self._timeline_with({"SetVoiceIsolationState": True}, item)

        report = audio_fairlight._voice_isolation_capabilities(tl, {"track_index": 1})

        self.assertFalse(report["timeline_track"]["get_available"])
        self.assertTrue(report["timeline_track"]["set_available"])
        self.assertFalse(report["item"]["get_available"])
        self.assertTrue(report["item"]["set_available"])


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
