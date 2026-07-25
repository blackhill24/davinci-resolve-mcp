import unittest

from src.server import _timeline_create_variant_from_ranges, _build_append_clip_info_dict
from tests._error_envelope_helpers import err_message, is_err


class MediaPoolItemStub:
    def __init__(self, unique_id):
        self._id = unique_id

    def GetUniqueId(self):
        return self._id


class RootFolderStub:
    def __init__(self, clips):
        self._clips = clips

    def GetClipList(self):
        return self._clips

    def GetSubFolderList(self):
        return []


class MediaPoolStub:
    def __init__(self, root):
        self._root = root
        self.created = None

    def GetRootFolder(self):
        return self._root

    def CreateEmptyTimeline(self, name):
        self.created = name
        raise AssertionError("dry_run must not create a timeline")


class ProjectStub:
    def __init__(self, mp):
        self._mp = mp

    def GetMediaPool(self):
        return self._mp


class SourceTimelineStub:
    def GetStartFrame(self):
        return 0


def _proj_with_clip(clip_id):
    return ProjectStub(MediaPoolStub(RootFolderStub([MediaPoolItemStub(clip_id)])))


class CreateVariantDryRunTest(unittest.TestCase):
    def test_dry_run_reports_would_create_without_creating(self):
        proj = _proj_with_clip("mp-1")
        res = _timeline_create_variant_from_ranges(proj, SourceTimelineStub(), {
            "name": "variant",
            "dry_run": True,
            "ranges": [{"clip_id": "mp-1", "start_frame": 0, "end_frame": 100}],
        })
        self.assertTrue(res.get("dry_run"))
        self.assertTrue(res.get("would_create_timeline"))
        self.assertIsNone(proj.GetMediaPool().created)

    def test_dry_run_fails_on_unresolvable_clip_id(self):
        proj = _proj_with_clip("mp-1")
        res = _timeline_create_variant_from_ranges(proj, SourceTimelineStub(), {
            "name": "variant",
            "dry_run": True,
            "ranges": [{"clip_id": "does-not-exist", "start_frame": 0, "end_frame": 100}],
        })
        self.assertTrue(is_err(res))
        self.assertIn("media pool clip not found", err_message(res))

    def test_dry_run_fails_on_invalid_frame_range(self):
        proj = _proj_with_clip("mp-1")
        res = _timeline_create_variant_from_ranges(proj, SourceTimelineStub(), {
            "name": "variant",
            "dry_run": True,
            "ranges": [{"clip_id": "mp-1", "start_frame": 100, "end_frame": 100}],
        })
        self.assertTrue(is_err(res))
        self.assertIn("requires valid start_frame/end_frame", err_message(res))

    def test_pack_dry_run_needs_no_record_frame(self):
        proj = _proj_with_clip("mp-1")
        res = _timeline_create_variant_from_ranges(proj, SourceTimelineStub(), {
            "name": "variant",
            "dry_run": True,
            "pack": True,
            "ranges": [{"clip_id": "mp-1", "start_frame": 0, "end_frame": 100}],
        })
        self.assertTrue(res.get("would_create_timeline"))
        self.assertIsNone(res["ranges"][0]["record_frame"])


class BuildAppendClipInfoPackTest(unittest.TestCase):
    def setUp(self):
        self.root = RootFolderStub([MediaPoolItemStub("mp-1")])

    def test_pack_omits_record_frame(self):
        info, err = _build_append_clip_info_dict(
            self.root, {"clip_id": "mp-1", "start_frame": 0, "end_frame": 100, "track_index": 1},
            0, pack=True)
        self.assertIsNone(err)
        self.assertNotIn("recordFrame", info)

    def test_non_pack_still_requires_record_frame(self):
        info, err = _build_append_clip_info_dict(
            self.root, {"clip_id": "mp-1", "start_frame": 0, "end_frame": 100, "track_index": 1},
            0, pack=False)
        self.assertIsNone(info)
        self.assertIn("record_frame", err_message(err))

    def test_non_pack_reports_record_frame_before_track_index(self):
        # Missing both: record_frame is validated first (preserved error order).
        info, err = _build_append_clip_info_dict(
            self.root, {"clip_id": "mp-1", "start_frame": 0, "end_frame": 100}, 0, pack=False)
        self.assertIsNone(info)
        self.assertIn("record_frame", err_message(err))


if __name__ == "__main__":
    unittest.main()


class CommitTimelineStub:
    """A created timeline whose start timecode may or may not stick."""

    def __init__(self, *, tc_applies=True):
        self.tc_applies = tc_applies
        self.start_tc = "00:00:00:00"
        self.tracks = {"video": 1, "audio": 1}

    def GetUniqueId(self):
        return "new-tl"

    def GetName(self):
        return "variant"

    def GetStartFrame(self):
        return 0

    def GetEndFrame(self):
        return 100

    def GetItemListInTrack(self, track_type, index):
        return []

    def GetMarkers(self):
        return {}

    def SetStartTimecode(self, tc):
        if self.tc_applies:
            self.start_tc = tc
        return True          # Resolve reports success either way — that's the finding

    def GetStartTimecode(self):
        return self.start_tc

    def GetTrackCount(self, track_type):
        return self.tracks.get(track_type, 1)

    def AddTrack(self, track_type):
        self.tracks[track_type] = self.tracks.get(track_type, 1) + 1
        return True


class CommitMediaPoolStub(MediaPoolStub):
    def __init__(self, root, timeline):
        super().__init__(root)
        self._timeline = timeline
        self.appended = None

    def CreateEmptyTimeline(self, name):
        self.created = name
        return self._timeline

    def AppendToTimeline(self, infos):
        self.appended = infos
        return [object() for _ in infos]


class CommitProjectStub(ProjectStub):
    def __init__(self, mp, timeline):
        super().__init__(mp)
        self._current = None
        self._timeline = timeline

    def SetCurrentTimeline(self, tl):
        self._current = tl
        return True

    def GetCurrentTimeline(self):
        return self._current


def _commit_proj(timeline):
    mp = CommitMediaPoolStub(RootFolderStub([MediaPoolItemStub("mp-1")]), timeline)
    return CommitProjectStub(mp, timeline), mp


class CreateVariantStartTimecodeTest(unittest.TestCase):
    """#113 Tier 2: a start timecode that silently fails must not be appended over.

    The record_frame values are ABSOLUTE and are computed from the requested
    start, so if the timecode does not take, every clip lands at the wrong
    offset. Resolve reports success on the set either way, so the readback in
    `_set_start_timecode` is the only thing that catches it.
    """

    def _params(self, **extra):
        params = {
            "name": "variant",
            "ranges": [{"clip_id": "mp-1", "start_frame": 0, "end_frame": 100,
                        "record_frame": 0}],
        }
        params.update(extra)
        return params

    def test_refuses_to_append_when_the_start_timecode_does_not_take(self):
        tl = CommitTimelineStub(tc_applies=False)
        proj, mp = _commit_proj(tl)

        res = _timeline_create_variant_from_ranges(
            proj, SourceTimelineStub(), self._params(start_timecode="01:00:00:00"))

        self.assertTrue(is_err(res))
        self.assertIn("start timecode", err_message(res))
        self.assertIsNone(mp.appended, "must not append against a wrong start")

    def test_appends_when_the_start_timecode_takes(self):
        tl = CommitTimelineStub(tc_applies=True)
        proj, mp = _commit_proj(tl)

        res = _timeline_create_variant_from_ranges(
            proj, SourceTimelineStub(), self._params(start_timecode="01:00:00:00"))

        self.assertFalse(is_err(res), res)
        self.assertIsNotNone(mp.appended)
        self.assertEqual("01:00:00:00", tl.GetStartTimecode())

    def test_no_start_timecode_requested_still_appends(self):
        tl = CommitTimelineStub(tc_applies=False)
        proj, mp = _commit_proj(tl)

        res = _timeline_create_variant_from_ranges(proj, SourceTimelineStub(), self._params())

        self.assertFalse(is_err(res), res)
        self.assertIsNotNone(mp.appended)
