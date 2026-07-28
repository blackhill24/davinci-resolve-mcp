"""Offline tests for the pure helpers in src/granular/common.py.

`common.py` is the shared spine of the granular surface: every granular module
pulls these helpers in via `import *`, so a regression here is a regression in
dozens of tools at once — and, because each module then holds its OWN binding,
it is exactly the kind of break the rest of the suite is blind to (#119).

Everything exercised here is pure validation/traversal. Nothing connects: the
two functions that reach for a Resolve handle (`_get_timeline_item`,
`ResolveProxy`) have `get_resolve` patched out, so a wedged or running Resolve
cannot change a result.
"""
import os
import unittest
from unittest import mock

from src.granular import common
from src.granular.common import (
    ResolveProxy,
    _build_audio_sync_settings,
    _build_create_clip_info_dict,
    _build_append_clip_info_dict,
    _build_subtitle_settings,
    _find_clip_by_id,
    _find_clips_by_ids,
    _frame_int,
    _get_timeline_item,
    _is_resolve_handle_live,
    _navigate_to_folder,
    _normalize_record_frame,
    _resolve_safe_dir,
    _timeline_start_frame,
    get_all_media_pool_folders,
    get_all_media_pool_clips,
    iter_all_media_pool_clips,
    timeline_item_kind,
)


class FakeClip:
    def __init__(self, clip_id):
        self.clip_id = clip_id

    def GetUniqueId(self):
        return self.clip_id


class FakeFolder:
    def __init__(self, name="Master", clips=(), subfolders=()):
        self.name = name
        self.clips = list(clips)
        self.subfolders = list(subfolders)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subfolders)


class FakeMediaPool:
    def __init__(self, root):
        self.root = root

    def GetRootFolder(self):
        return self.root


def _tree():
    """Master[a] -> Interviews[b, c] -> Day 1[d]; Master -> B-Roll[e]."""
    day1 = FakeFolder("Day 1", clips=[FakeClip("d")])
    interviews = FakeFolder("Interviews", clips=[FakeClip("b"), FakeClip("c")], subfolders=[day1])
    broll = FakeFolder("B-Roll", clips=[FakeClip("e")])
    return FakeFolder("Master", clips=[FakeClip("a")], subfolders=[interviews, broll])


class FrameIntTest(unittest.TestCase):
    def test_rounds_floats_and_numeric_strings(self):
        self.assertEqual(_frame_int(10.6), 11)
        self.assertEqual(_frame_int("24"), 24)
        self.assertEqual(_frame_int(-2.4), -2)

    def test_none_and_garbage_yield_none(self):
        self.assertIsNone(_frame_int(None))
        self.assertIsNone(_frame_int("start"))
        self.assertIsNone(_frame_int(object()))

    def test_timeline_start_frame_reads_the_timeline(self):
        self.assertEqual(_timeline_start_frame(mock.Mock(GetStartFrame=lambda: 86400.0)), 86400)

    def test_timeline_start_frame_survives_a_missing_or_raising_timeline(self):
        self.assertIsNone(_timeline_start_frame(None))
        raising = mock.Mock()
        raising.GetStartFrame.side_effect = RuntimeError("bridge died")
        self.assertIsNone(_timeline_start_frame(raising))


class NormalizeRecordFrameTest(unittest.TestCase):
    """record_frame is timeline-RELATIVE by default; Resolve wants absolute."""

    def test_relative_is_offset_from_the_timeline_start(self):
        self.assertEqual(_normalize_record_frame({"record_frame": 10}, 0, 86400), (86410, None))

    def test_absolute_is_passed_through_untouched(self):
        frame, err = _normalize_record_frame(
            {"record_frame": 86410, "record_frame_mode": "absolute"}, 0, 86400
        )
        self.assertEqual((frame, err), (86410, None))

    def test_auto_promotes_only_values_below_the_timeline_start(self):
        below = {"record_frame": 10, "record_frame_mode": "auto"}
        self.assertEqual(_normalize_record_frame(below, 0, 86400), (86410, None))
        above = {"record_frame": 86500, "record_frame_mode": "auto"}
        self.assertEqual(_normalize_record_frame(above, 0, 86400), (86500, None))

    def test_zero_or_unknown_timeline_start_leaves_the_frame_alone(self):
        self.assertEqual(_normalize_record_frame({"record_frame": 10}, 0, 0), (10, None))
        self.assertEqual(_normalize_record_frame({"record_frame": 10}, 0, None), (10, None))

    def test_mode_aliases(self):
        for alias in ("relative", "timeline_relative", "offset"):
            with self.subTest(alias=alias):
                frame, _ = _normalize_record_frame(
                    {"record_frame": 10, "record_frame_mode": alias}, 0, 86400
                )
                self.assertEqual(frame, 86410)
        for alias in ("absolute", "timeline_absolute"):
            with self.subTest(alias=alias):
                frame, _ = _normalize_record_frame(
                    {"record_frame": 10, "record_frame_mode": alias}, 0, 86400
                )
                self.assertEqual(frame, 10)

    def test_camelcase_keys_accepted(self):
        self.assertEqual(
            _normalize_record_frame({"recordFrame": 10, "recordFrameMode": "absolute"}, 0, 86400),
            (10, None),
        )

    def test_non_numeric_record_frame_is_rejected(self):
        frame, err = _normalize_record_frame({"record_frame": "soon"}, 3, 86400)
        self.assertIsNone(frame)
        self.assertIn("clip_infos[3]", err["error"])

    def test_unknown_mode_is_rejected(self):
        frame, err = _normalize_record_frame(
            {"record_frame": 10, "record_frame_mode": "sideways"}, 1, 86400
        )
        self.assertIsNone(frame)
        self.assertIn("record_frame_mode", err["error"])


class ClipInfoBuilderTest(unittest.TestCase):
    def setUp(self):
        self.root = _tree()

    def _ci(self, **overrides):
        base = {"clip_id": "b", "start_frame": 0, "end_frame": 100,
                "record_frame": 10, "track_index": 1}
        base.update(overrides)
        return base

    def test_append_builds_the_five_required_keys(self):
        out, err = _build_append_clip_info_dict(self.root, self._ci(), 0, 86400)
        self.assertIsNone(err)
        self.assertEqual(
            set(out), {"mediaPoolItem", "startFrame", "endFrame", "recordFrame", "trackIndex"}
        )
        self.assertEqual(out["mediaPoolItem"].GetUniqueId(), "b")
        self.assertEqual(out["recordFrame"], 86410)

    def test_append_includes_media_type_only_when_given(self):
        out, _ = _build_append_clip_info_dict(self.root, self._ci(media_type=1), 0)
        self.assertEqual(out["mediaType"], 1)
        out, _ = _build_append_clip_info_dict(self.root, self._ci(), 0)
        self.assertNotIn("mediaType", out)

    def test_create_builds_the_four_required_keys(self):
        out, err = _build_create_clip_info_dict(self.root, self._ci(), 0, 86400)
        self.assertIsNone(err)
        self.assertEqual(set(out), {"mediaPoolItem", "startFrame", "endFrame", "recordFrame"})
        self.assertEqual(out["recordFrame"], 86410)

    def test_create_ignores_track_index(self):
        out, err = _build_create_clip_info_dict(
            self.root, {"clip_id": "b", "start_frame": 0, "end_frame": 100, "record_frame": 0}, 0
        )
        self.assertIsNone(err)
        self.assertNotIn("trackIndex", out)

    def test_clip_is_found_in_a_nested_folder(self):
        out, err = _build_create_clip_info_dict(self.root, self._ci(clip_id="d"), 0)
        self.assertIsNone(err)
        self.assertEqual(out["mediaPoolItem"].GetUniqueId(), "d")

    def test_missing_clip_is_reported_with_its_id(self):
        for builder in (_build_append_clip_info_dict, _build_create_clip_info_dict):
            with self.subTest(builder=builder.__name__):
                out, err = builder(self.root, self._ci(clip_id="nope"), 2)
                self.assertIsNone(out)
                self.assertIn("nope", err["error"])
                self.assertIn("clip_infos[2]", err["error"])

    def test_non_dict_entry_is_rejected(self):
        for builder in (_build_append_clip_info_dict, _build_create_clip_info_dict):
            with self.subTest(builder=builder.__name__):
                out, err = builder(self.root, "not a dict", 0)
                self.assertIsNone(out)
                self.assertIn("must be an object", err["error"])

    def test_missing_required_fields_are_each_named(self):
        cases = [
            ({"start_frame": 0, "end_frame": 1, "record_frame": 0, "track_index": 1}, "clip_id"),
            (self._ci(start_frame=None), "start_frame"),
            (self._ci(end_frame=None), "end_frame"),
            (self._ci(record_frame=None), "record_frame"),
        ]
        for payload, expected in cases:
            with self.subTest(missing=expected):
                out, err = _build_create_clip_info_dict(self.root, payload, 0)
                self.assertIsNone(out)
                self.assertIn(expected, err["error"])

    def test_append_requires_a_track_index(self):
        out, err = _build_append_clip_info_dict(self.root, self._ci(track_index=None), 0)
        self.assertIsNone(out)
        self.assertIn("track_index", err["error"])

    def test_media_pool_item_id_is_an_accepted_alias(self):
        payload = {"media_pool_item_id": "b", "start_frame": 0, "end_frame": 1, "record_frame": 0}
        out, err = _build_create_clip_info_dict(self.root, payload, 0)
        self.assertIsNone(err)
        self.assertEqual(out["mediaPoolItem"].GetUniqueId(), "b")

    def test_record_frame_mode_errors_propagate_from_the_builders(self):
        out, err = _build_create_clip_info_dict(
            self.root, self._ci(record_frame_mode="sideways"), 0, 86400
        )
        self.assertIsNone(out)
        self.assertIn("record_frame_mode", err["error"])


class MediaPoolTraversalTest(unittest.TestCase):
    def setUp(self):
        self.root = _tree()
        self.pool = FakeMediaPool(self.root)

    def test_clips_are_yielded_in_preorder(self):
        ids = [c.GetUniqueId() for c in get_all_media_pool_clips(self.pool)]
        self.assertEqual(ids, ["a", "b", "c", "d", "e"])

    def test_iteration_is_lazy_enough_to_exit_early(self):
        # An early break must not have walked the whole tree — this is the whole
        # reason the lazy iterator exists alongside the eager list builder.
        walked = []

        class CountingFolder(FakeFolder):
            def GetClipList(self):
                walked.append(self.name)
                return super().GetClipList()

        root = CountingFolder("Master", clips=[FakeClip("a")],
                              subfolders=[CountingFolder("Deep", clips=[FakeClip("z")])])
        for clip in iter_all_media_pool_clips(FakeMediaPool(root)):
            if clip.GetUniqueId() == "a":
                break
        self.assertEqual(walked, ["Master"])

    def test_missing_root_folder_yields_nothing(self):
        self.assertEqual(get_all_media_pool_clips(FakeMediaPool(None)), [])

    def test_all_folders_are_collected_recursively(self):
        names = [f.GetName() for f in get_all_media_pool_folders(self.pool)]
        self.assertEqual(names, ["Master", "Interviews", "Day 1", "B-Roll"])

    def test_folders_handles_missing_root_and_missing_pool(self):
        self.assertEqual(get_all_media_pool_folders(FakeMediaPool(None)), [])
        self.assertEqual(get_all_media_pool_folders(None), [])

    def test_find_clip_by_id_searches_subfolders(self):
        self.assertEqual(_find_clip_by_id(self.root, "d").GetUniqueId(), "d")
        self.assertIsNone(_find_clip_by_id(self.root, "missing"))

    def test_find_clips_by_ids_returns_every_match(self):
        found = _find_clips_by_ids(self.root, {"a", "d", "missing"})
        self.assertEqual(sorted(c.GetUniqueId() for c in found), ["a", "d"])

    def test_find_clips_by_ids_on_no_match(self):
        self.assertEqual(_find_clips_by_ids(self.root, {"missing"}), [])


class NavigateToFolderTest(unittest.TestCase):
    def setUp(self):
        self.root = _tree()
        self.pool = FakeMediaPool(self.root)

    def test_root_aliases_all_return_the_root(self):
        for path in (None, "", "/", "Master"):
            with self.subTest(path=path):
                self.assertIs(_navigate_to_folder(self.pool, path), self.root)

    def test_nested_path_with_and_without_the_master_prefix(self):
        self.assertEqual(_navigate_to_folder(self.pool, "Interviews/Day 1").GetName(), "Day 1")
        self.assertEqual(_navigate_to_folder(self.pool, "Master/Interviews/Day 1").GetName(), "Day 1")

    def test_surrounding_slashes_are_tolerated(self):
        self.assertEqual(_navigate_to_folder(self.pool, "/Interviews/").GetName(), "Interviews")

    def test_unknown_segment_returns_none(self):
        self.assertIsNone(_navigate_to_folder(self.pool, "Interviews/Day 9"))
        self.assertIsNone(_navigate_to_folder(self.pool, "Nope"))


class TimelineItemKindTest(unittest.TestCase):
    """GetType()/GetMediaType() do not exist on Resolve 21.x (#142 finding 2)."""

    def _item(self, track_info):
        return mock.Mock(GetTrackTypeAndIndex=lambda: track_info)

    def test_known_track_types(self):
        self.assertEqual(timeline_item_kind(self._item(("video", 1))), "Video")
        self.assertEqual(timeline_item_kind(self._item(("audio", 2))), "Audio")
        self.assertEqual(timeline_item_kind(self._item(("subtitle", 1))), "Subtitle")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(timeline_item_kind(self._item((" VIDEO ", 1))), "Video")

    def test_none_item_and_empty_track_info(self):
        self.assertIsNone(timeline_item_kind(None))
        self.assertIsNone(timeline_item_kind(self._item(None)))
        self.assertIsNone(timeline_item_kind(self._item([])))

    def test_unrecognised_track_type(self):
        self.assertIsNone(timeline_item_kind(self._item(("caption", 1))))

    def test_raising_bridge_call_returns_none(self):
        item = mock.Mock()
        item.GetTrackTypeAndIndex.side_effect = TypeError("'NoneType' object is not callable")
        self.assertIsNone(timeline_item_kind(item))


class GetTimelineItemTest(unittest.TestCase):
    """The negative-index guard from #141 finding 2.

    Without it, `item_index=-1` reverse-indexes into the track list and every
    granular tool silently reads or MUTATES the last clip instead of erroring.
    """

    def _resolve_with_items(self, items):
        timeline = mock.Mock()
        timeline.GetItemListInTrack.return_value = items
        project = mock.Mock()
        project.GetCurrentTimeline.return_value = timeline
        pm = mock.Mock()
        pm.GetCurrentProject.return_value = project
        resolve = mock.Mock()
        resolve.GetProjectManager.return_value = pm
        return resolve

    def test_valid_index_returns_the_item(self):
        items = ["first", "second"]
        with mock.patch.object(common, "get_resolve", return_value=self._resolve_with_items(items)):
            item, err = _get_timeline_item("video", 1, 1)
        self.assertEqual((item, err), ("second", None))

    def test_negative_index_is_rejected_not_reverse_indexed(self):
        items = ["first", "last"]
        with mock.patch.object(common, "get_resolve", return_value=self._resolve_with_items(items)):
            item, err = _get_timeline_item("video", 1, -1)
        self.assertIsNone(item)
        self.assertIn("No item at index -1", err["error"])

    def test_boolean_index_is_rejected(self):
        # bool is an int subclass; True would otherwise read item 1.
        items = ["first", "second"]
        with mock.patch.object(common, "get_resolve", return_value=self._resolve_with_items(items)):
            item, err = _get_timeline_item("video", 1, True)
        self.assertIsNone(item)
        self.assertIsNotNone(err)

    def test_out_of_range_and_empty_track(self):
        with mock.patch.object(common, "get_resolve", return_value=self._resolve_with_items(["only"])):
            self.assertIsNone(_get_timeline_item("video", 1, 5)[0])
        with mock.patch.object(common, "get_resolve", return_value=self._resolve_with_items([])):
            self.assertIsNone(_get_timeline_item("video", 1, 0)[0])

    def test_disconnected_resolve_reports_the_connection_not_the_index(self):
        with mock.patch.object(common, "get_resolve", return_value=None):
            item, err = _get_timeline_item("video", 1, 0)
        self.assertIsNone(item)
        self.assertIn("Not connected", err["error"])

    def test_no_project_and_no_timeline_are_distinguished(self):
        resolve = self._resolve_with_items([])
        resolve.GetProjectManager.return_value.GetCurrentProject.return_value = None
        with mock.patch.object(common, "get_resolve", return_value=resolve):
            self.assertIn("No project", _get_timeline_item("video", 1, 0)[1]["error"])

        resolve = self._resolve_with_items([])
        resolve.GetProjectManager.return_value.GetCurrentProject.return_value.GetCurrentTimeline.return_value = None
        with mock.patch.object(common, "get_resolve", return_value=resolve):
            self.assertIn("No current timeline", _get_timeline_item("video", 1, 0)[1]["error"])


class AudioSyncSettingsTest(unittest.TestCase):
    class FakeResolve:
        AUDIO_SYNC_MODE = "mode-key"
        AUDIO_SYNC_CHANNEL_NUMBER = "channel-key"
        AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO = "retain-audio-key"
        AUDIO_SYNC_RETAIN_VIDEO_METADATA = "retain-video-key"
        AUDIO_SYNC_WAVEFORM = "WAVEFORM_CONST"
        AUDIO_SYNC_TIMECODE = "TIMECODE_CONST"
        AUDIO_SYNC_CHANNEL_AUTOMATIC = "AUTO_CONST"
        AUDIO_SYNC_CHANNEL_MIX = "MIX_CONST"

    def setUp(self):
        self.resolve = self.FakeResolve()

    def test_empty_when_nothing_is_requested(self):
        self.assertEqual(_build_audio_sync_settings(self.resolve), ({}, None))

    def test_sync_modes_map_to_constants(self):
        settings, err = _build_audio_sync_settings(self.resolve, sync_mode="Timecode")
        self.assertIsNone(err)
        self.assertEqual(settings, {"mode-key": "TIMECODE_CONST"})

    def test_unknown_sync_mode_is_rejected(self):
        settings, err = _build_audio_sync_settings(self.resolve, sync_mode="vibes")
        self.assertIsNone(settings)
        self.assertIn("vibes", err["error"])

    def test_special_channel_names_map_to_constants(self):
        for name, const in (("automatic", "AUTO_CONST"), ("auto", "AUTO_CONST"), ("mix", "MIX_CONST")):
            with self.subTest(channel=name):
                settings, err = _build_audio_sync_settings(self.resolve, channel_number=name)
                self.assertIsNone(err)
                self.assertEqual(settings["channel-key"], const)

    def test_numeric_channel_is_passed_through(self):
        settings, err = _build_audio_sync_settings(self.resolve, channel_number=3)
        self.assertIsNone(err)
        self.assertEqual(settings["channel-key"], 3)

    def test_unknown_channel_string_and_wrong_type_are_rejected(self):
        self.assertIsNone(_build_audio_sync_settings(self.resolve, channel_number="left")[0])
        settings, err = _build_audio_sync_settings(self.resolve, channel_number=1.5)
        self.assertIsNone(settings)
        self.assertIn("float", err["error"])

    def test_retain_flags_are_coerced_to_bool(self):
        settings, err = _build_audio_sync_settings(
            self.resolve, retain_embedded_audio=1, retain_video_metadata=0
        )
        self.assertIsNone(err)
        self.assertIs(settings["retain-audio-key"], True)
        self.assertIs(settings["retain-video-key"], False)


class SubtitleSettingsTest(unittest.TestCase):
    class FakeResolve:
        """Only the constants these tests actually exercise, declared explicitly.

        No `__getattr__`: fabricating unknown attributes is the bridge's job and
        a hand-rolled copy of it is banned by
        tests/test_hand_rolled_double_audit.py (#119 task 5).
        """

        SUBTITLE_LANGUAGE = "language-key"
        SUBTITLE_CAPTION_PRESET = "preset-key"
        SUBTITLE_CHARS_PER_LINE = "chars-key"
        SUBTITLE_LINE_BREAK = "break-key"
        SUBTITLE_GAP = "gap-key"
        AUTO_CAPTION_ENGLISH = "ENGLISH_CONST"
        AUTO_CAPTION_MANDARIN_SIMPLIFIED = "MANDARIN_SIMPLIFIED_CONST"
        AUTO_CAPTION_LINE_SINGLE = "LINE_SINGLE_CONST"
        AUTO_CAPTION_LINE_DOUBLE = "LINE_DOUBLE_CONST"

    def setUp(self):
        self.resolve = self.FakeResolve()

    def test_empty_when_nothing_is_requested(self):
        self.assertEqual(_build_subtitle_settings(self.resolve), ({}, None))

    def test_language_maps_to_a_constant(self):
        settings, err = _build_subtitle_settings(self.resolve, language="English")
        self.assertIsNone(err)
        self.assertEqual(settings, {"language-key": "ENGLISH_CONST"})

    def test_hyphen_and_underscore_language_variants_both_work(self):
        for name in ("mandarin_simplified", "mandarin-simplified"):
            with self.subTest(language=name):
                settings, err = _build_subtitle_settings(self.resolve, language=name)
                self.assertIsNone(err)
                self.assertEqual(settings["language-key"], "MANDARIN_SIMPLIFIED_CONST")

    def test_unknown_language_lists_the_valid_ones(self):
        settings, err = _build_subtitle_settings(self.resolve, language="Klingon")
        self.assertIsNone(settings)
        self.assertIn("Klingon", err["error"])
        self.assertIn("english", err["error"])

    def test_unknown_preset_and_line_break_are_rejected(self):
        self.assertIsNone(_build_subtitle_settings(self.resolve, preset="fancy")[0])
        settings, err = _build_subtitle_settings(self.resolve, line_break="triple")
        self.assertIsNone(settings)
        self.assertIn("triple", err["error"])

    def test_line_break_maps_to_a_constant(self):
        settings, err = _build_subtitle_settings(self.resolve, line_break="double")
        self.assertIsNone(err)
        self.assertEqual(settings["break-key"], "LINE_DOUBLE_CONST")

    def test_chars_per_line_bounds(self):
        for good in (1, 30, 60):
            with self.subTest(chars=good):
                settings, err = _build_subtitle_settings(self.resolve, chars_per_line=good)
                self.assertIsNone(err)
                self.assertEqual(settings["chars-key"], good)
        # `True`/`False` must be rejected: bool is an int subclass, so without an
        # explicit guard `1 <= True <= 60` passes and a bool reaches Resolve as a
        # chars-per-line value.
        for bad in (0, 61, "30", 1.5, True, False):
            with self.subTest(chars=bad):
                settings, err = _build_subtitle_settings(self.resolve, chars_per_line=bad)
                self.assertIsNone(settings)
                self.assertIn("chars_per_line", err["error"])

    def test_gap_bounds(self):
        for good in (0, 5, 10):
            with self.subTest(gap=good):
                settings, err = _build_subtitle_settings(self.resolve, gap=good)
                self.assertIsNone(err)
                self.assertEqual(settings["gap-key"], good)
        # Both bools slip through without the guard here, since `0 <= False <= 10`
        # holds too.
        for bad in (-1, 11, "5", 1.5, True, False):
            with self.subTest(gap=bad):
                settings, err = _build_subtitle_settings(self.resolve, gap=bad)
                self.assertIsNone(settings)
                self.assertIn("gap", err["error"])


class MediaPoolAccessorTest(unittest.TestCase):
    def _resolve(self, project=None, media_pool=None):
        pm = mock.Mock()
        pm.GetCurrentProject.return_value = project
        if project is not None:
            project.GetMediaPool.return_value = media_pool
        resolve = mock.Mock()
        resolve.GetProjectManager.return_value = pm
        return resolve

    def test_get_project_manager_returns_none_when_disconnected(self):
        with mock.patch.object(common, "get_resolve", return_value=None):
            self.assertIsNone(common.get_project_manager())

    def test_get_current_project_returns_a_pair(self):
        project = mock.Mock()
        with mock.patch.object(common, "get_resolve", return_value=self._resolve(project)):
            pm, proj = common.get_current_project()
        self.assertIsNotNone(pm)
        self.assertIs(proj, project)

    def test_get_current_project_when_disconnected(self):
        with mock.patch.object(common, "get_resolve", return_value=None):
            self.assertEqual(common.get_current_project(), (None, None))

    def test_get_mp_happy_path(self):
        project, media_pool = mock.Mock(), mock.Mock()
        with mock.patch.object(common, "get_resolve", return_value=self._resolve(project, media_pool)):
            proj, mp, err = common._get_mp()
        self.assertIsNone(err)
        self.assertIs(proj, project)
        self.assertIs(mp, media_pool)

    def test_get_mp_distinguishes_its_three_failure_modes(self):
        with mock.patch.object(common, "get_resolve", return_value=None):
            self.assertIn("Not connected", common._get_mp()[2]["error"])

        with mock.patch.object(common, "get_resolve", return_value=self._resolve(None)):
            self.assertIn("No project", common._get_mp()[2]["error"])

        project = mock.Mock()
        with mock.patch.object(common, "get_resolve", return_value=self._resolve(project, None)):
            proj, mp, err = common._get_mp()
        self.assertIs(proj, project)
        self.assertIsNone(mp)
        self.assertIn("MediaPool", err["error"])


class ResolveSafeDirTest(unittest.TestCase):
    """Resolve cannot write into sandbox temp dirs, so they are redirected."""

    def _redirected(self):
        return os.path.join(os.path.expanduser("~"), "Documents", "resolve-stills")

    def test_linux_temp_paths_are_redirected(self):
        with mock.patch("platform.system", return_value="Linux"):
            self.assertEqual(_resolve_safe_dir("/tmp/stills"), self._redirected())
            self.assertEqual(_resolve_safe_dir("/var/tmp/stills"), self._redirected())
            self.assertEqual(_resolve_safe_dir("/home/user/stills"), "/home/user/stills")

    def test_macos_sandbox_paths_are_redirected(self):
        with mock.patch("platform.system", return_value="Darwin"):
            self.assertEqual(_resolve_safe_dir("/var/folders/xy/stills"), self._redirected())
            self.assertEqual(_resolve_safe_dir("/private/var/tmp/stills"), self._redirected())
            self.assertEqual(_resolve_safe_dir("/Users/me/stills"), "/Users/me/stills")

    def test_unknown_platform_leaves_the_path_alone(self):
        with mock.patch("platform.system", return_value="Plan9"):
            self.assertEqual(_resolve_safe_dir("/tmp/stills"), "/tmp/stills")


class ResolveHandleLivenessTest(unittest.TestCase):
    def test_handle_answering_getversion_is_live(self):
        self.assertTrue(_is_resolve_handle_live(mock.Mock(GetVersion=lambda: [21, 0, 2, 4])))

    def test_handle_returning_falsey_version_is_stale(self):
        self.assertFalse(_is_resolve_handle_live(mock.Mock(GetVersion=lambda: None)))

    def test_handle_without_a_callable_getversion_is_stale(self):
        self.assertFalse(_is_resolve_handle_live(object()))
        self.assertFalse(_is_resolve_handle_live(None))

    def test_raising_handle_is_stale_rather_than_propagating(self):
        handle = mock.Mock()
        handle.GetVersion.side_effect = RuntimeError("connection reset")
        self.assertFalse(_is_resolve_handle_live(handle))


class ResolveProxyTest(unittest.TestCase):
    """The proxy must stay LATE-bound: it re-reads get_resolve() every access."""

    def test_falsey_when_disconnected_and_truthy_when_connected(self):
        proxy = ResolveProxy()
        with mock.patch.object(common, "get_resolve", return_value=None):
            self.assertFalse(proxy)
        with mock.patch.object(common, "get_resolve", return_value=mock.Mock()):
            self.assertTrue(proxy)

    def test_attribute_access_forwards_to_the_live_handle(self):
        handle = mock.Mock()
        handle.GetProductName.return_value = "DaVinci Resolve Studio"
        with mock.patch.object(common, "get_resolve", return_value=handle):
            self.assertEqual(ResolveProxy().GetProductName(), "DaVinci Resolve Studio")

    def test_attribute_access_while_disconnected_raises_a_clear_error(self):
        with mock.patch.object(common, "get_resolve", return_value=None):
            with self.assertRaises(AttributeError) as ctx:
                ResolveProxy().GetProductName
        self.assertIn("not connected", str(ctx.exception).lower())

    def test_a_reconnect_is_picked_up_without_rebinding_the_proxy(self):
        proxy = ResolveProxy()
        with mock.patch.object(common, "get_resolve", return_value=None):
            self.assertFalse(proxy)
        handle = mock.Mock()
        handle.GetProductName.return_value = "DaVinci Resolve"
        with mock.patch.object(common, "get_resolve", return_value=handle):
            self.assertEqual(proxy.GetProductName(), "DaVinci Resolve")


if __name__ == "__main__":
    unittest.main()
