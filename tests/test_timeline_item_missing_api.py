"""#142 findings 1 & 2: two whole method groups do not exist on TimelineItem.

Live-verified on Resolve Studio 21.0.2.4 (`dir(TimelineItem)`, 88 entries):

- the seven-method keyframe API is absent **entirely** — there is no rename that
  fixes it, so the tools must refuse rather than crash;
- `GetType` / `GetMediaType` are absent, but `GetTrackTypeAndIndex` is present
  and carries the same information.

Both must be exercised through `ResolveBridgeDouble`, not `MagicMock`: the real
bridge *fabricates* any missing attribute as a non-callable `None` and answers
`hasattr` True for everything, so a MagicMock would make the absent methods look
present (or absent for the wrong reason) and the tests would prove nothing. See
tests/GUARDS.md and #119.
"""

from __future__ import annotations

import unittest

from src import server  # noqa: F401 - import first; domain modules import back from it
from src.domains.timeline_edit import actions as timeline_edit_actions
from src.granular.common import timeline_item_kind
from tests.bridge_double import ResolveBridgeDouble

# Every method a real 21.0.2.4 TimelineItem does expose that these paths touch.
_REAL_TIMELINE_ITEM_METHODS = {
    "GetName": "broll_b.mov",
    "GetStart": 0,
    "GetEnd": 120,
    "GetDuration": 120,
    "GetUniqueId": "item-1",
    "GetTrackTypeAndIndex": ["video", 1],
    "GetProperty": lambda *_a, **_k: 0.0,
    "SetProperty": lambda *_a, **_k: True,
}


def _timeline_item(track_type="video", **extra):
    methods = dict(_REAL_TIMELINE_ITEM_METHODS)
    methods["GetTrackTypeAndIndex"] = [track_type, 1]
    methods.update(extra)
    return ResolveBridgeDouble(methods=methods)


class KeyframeApiAbsenceTest(unittest.TestCase):
    """Finding 1 — no keyframe surface at all."""

    def test_the_probe_reports_the_api_missing_on_a_real_shaped_item(self):
        item = _timeline_item()
        self.assertFalse(timeline_edit_actions._keyframe_api_available(item))
        # And it is a probe, not a hard-coded False: a build that shipped the
        # API would be detected.
        future = _timeline_item(**{
            name: (lambda *_a, **_k: 0)
            for name in timeline_edit_actions._KEYFRAME_METHODS
        })
        self.assertTrue(timeline_edit_actions._keyframe_api_available(future))

    def test_calling_a_fabricated_keyframe_method_really_does_raise(self):
        # The premise of the whole finding: the bridge hands back a
        # non-callable None rather than raising AttributeError.
        item = _timeline_item()
        self.assertIsNone(getattr(item, "GetKeyframeCount"))
        with self.assertRaises(TypeError):
            item.GetKeyframeCount("Pan")

    def test_copy_keyframes_reports_failure_instead_of_success_having_copied_nothing(self):
        source = _timeline_item()
        duplicate = _timeline_item()
        result = timeline_edit_actions._copy_keyframes(source, duplicate, ["Pan", "ZoomX"])
        self.assertFalse(
            result["success"],
            "success:true having copied nothing is the silent-drop bug this fixes",
        )
        self.assertEqual(0, result["copied"])
        self.assertEqual("KEYFRAMES_UNSUPPORTED", result["code"])
        self.assertEqual(
            ["Pan", "ZoomX"], [row["property"] for row in result["unavailable"]])

    def test_copy_keyframes_still_works_where_the_methods_exist(self):
        # Guard the guard: the refusal must be capability-driven, not blanket.
        added = []
        source = _timeline_item(
            GetKeyframeCount=lambda _prop: 1,
            GetKeyframeAtIndex=lambda _prop, _i: {"frame": 7},
            GetPropertyAtKeyframeIndex=lambda _prop, _i: 0.5,
        )
        duplicate = _timeline_item(
            AddKeyframe=lambda prop, frame, value: added.append((prop, frame, value)) or True,
        )
        result = timeline_edit_actions._copy_keyframes(source, duplicate, ["Pan"])
        self.assertTrue(result["success"])
        self.assertEqual(1, result["copied"])
        self.assertEqual([("Pan", 7, 0.5)], added)

    def test_each_keyframe_action_returns_a_named_refusal(self):
        for action in ("get_keyframes", "add_keyframe", "modify_keyframe",
                       "delete_keyframe", "set_keyframe_interpolation"):
            with self.subTest(action=action):
                methods = timeline_edit_actions._KEYFRAME_ACTION_METHODS[action]
                self.assertFalse(
                    timeline_edit_actions._keyframe_api_available(
                        _timeline_item(), methods))
                refusal = timeline_edit_actions._keyframes_unsupported(action)["error"]
                self.assertEqual("KEYFRAMES_UNSUPPORTED", refusal["code"])
                self.assertEqual("unsupported", refusal["category"])
                self.assertFalse(refusal["retryable"],
                                 "a missing API is never worth retrying")
                self.assertEqual(list(methods), refusal["state"]["missing_methods"])
                self.assertIn("fusion_comp", refusal["remediation"])


class TimelineItemKindTest(unittest.TestCase):
    """Finding 2 — GetType/GetMediaType absent, GetTrackTypeAndIndex present."""

    def test_video_and_audio_are_resolved_without_the_absent_methods(self):
        video = _timeline_item("video")
        audio = _timeline_item("audio")
        self.assertEqual("Video", timeline_item_kind(video))
        self.assertEqual("Audio", timeline_item_kind(audio))
        # Neither absent method was consulted — they would have raised.
        self.assertIsNone(getattr(video, "GetType"))
        self.assertIsNone(getattr(video, "GetMediaType"))

    def test_subtitle_tracks_are_named_not_mistaken_for_video(self):
        self.assertEqual("Subtitle", timeline_item_kind(_timeline_item("subtitle")))

    def test_an_unusable_item_is_none_rather_than_a_wrong_guess(self):
        self.assertIsNone(timeline_item_kind(None))
        self.assertIsNone(timeline_item_kind(ResolveBridgeDouble(methods={})))
        self.assertIsNone(timeline_item_kind(
            ResolveBridgeDouble(methods={"GetTrackTypeAndIndex": []})))
        self.assertIsNone(timeline_item_kind(
            ResolveBridgeDouble(methods={"GetTrackTypeAndIndex": ["hologram", 1]})))

    def test_no_call_site_still_reaches_the_absent_methods(self):
        # The finding listed 8 entry points across 12 call sites; pin that none
        # came back.
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "granular", "timeline_item.py",
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn(".GetType()", source)
        self.assertNotIn(".GetMediaType()", source)


if __name__ == "__main__":
    unittest.main()
