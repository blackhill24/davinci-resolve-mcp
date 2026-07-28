"""#144: five Resolve API methods called with a signature the API doesn't have.

In four of the five the compound surface calls the same method correctly, so
these are granular-only copy/paste drift. The reference undercounts the real API
surface, so none of this is reported on absence — every one is a *documented*
method whose documented parameter list disagreed with the call. Finding 5 was
additionally settled live: `Timeline.ApplyGradeFromDRX` is fabricated, absent
from `dir(Timeline)` on 21.0.2.4, while `Graph.ApplyGradeFromDRX` is real.

`ResolveBridgeDouble` records the exact arguments each method received, which is
what these assertions are actually about — a MagicMock would accept any arity
and prove nothing.
"""

from __future__ import annotations

import unittest
from unittest import mock

from src import server  # noqa: F401 - import first (circular-import guard)
from src.granular import gallery as gallery_mod
from src.granular import media_storage as media_storage_mod
from src.granular import timeline as timeline_mod
from tests.bridge_double import ResolveBridgeDouble, calls_of


def _unwrapped(tool):
    return getattr(tool, "__wrapped__", tool)


def _album(name="Stills"):
    return ResolveBridgeDouble(methods={"GetName": name})


def _gallery(**methods):
    return ResolveBridgeDouble(methods=methods)


def _resolve_with_gallery(gallery):
    project = ResolveBridgeDouble(methods={"GetGallery": gallery})
    pm = ResolveBridgeDouble(methods={"GetCurrentProject": project})
    return ResolveBridgeDouble(methods={"GetProjectManager": pm})


class GalleryAlbumNameTest(unittest.TestCase):
    """Findings 1 (+ its read twin): every album method takes the album object."""

    def test_set_album_name_passes_the_album_object_first(self):
        album = _album()
        gallery = _gallery(GetCurrentStillAlbum=album, SetAlbumName=True)
        with mock.patch.object(gallery_mod, "get_resolve",
                               return_value=_resolve_with_gallery(gallery)):
            result = _unwrapped(gallery_mod.set_gallery_album_name)("Selects")
        self.assertTrue(result["success"])
        self.assertEqual(
            [("SetAlbumName", (album, "Selects"), {})],
            [c for c in calls_of(gallery) if c[0] == "SetAlbumName"],
            "SetAlbumName(galleryStillAlbum, albumName) - the album was never passed",
        )

    def test_set_album_name_can_target_an_album_by_index(self):
        first, second = _album("A"), _album("B")
        gallery = _gallery(GetGalleryStillAlbums=[first, second], SetAlbumName=True)
        with mock.patch.object(gallery_mod, "get_resolve",
                               return_value=_resolve_with_gallery(gallery)):
            result = _unwrapped(gallery_mod.set_gallery_album_name)("Selects", album_index=1)
        self.assertTrue(result["success"])
        self.assertEqual(
            ("SetAlbumName", (second, "Selects"), {}),
            [c for c in calls_of(gallery) if c[0] == "SetAlbumName"][0])

    def test_an_out_of_range_index_is_refused_before_the_api_call(self):
        gallery = _gallery(GetGalleryStillAlbums=[_album()], SetAlbumName=True)
        with mock.patch.object(gallery_mod, "get_resolve",
                               return_value=_resolve_with_gallery(gallery)):
            result = _unwrapped(gallery_mod.set_gallery_album_name)("X", album_index=7)
        self.assertIn("out of range", result["error"])
        self.assertEqual([], [c for c in calls_of(gallery) if c[0] == "SetAlbumName"])

    def test_get_album_name_passes_the_album_object_too(self):
        album = _album()
        gallery = _gallery(GetCurrentStillAlbum=album, GetAlbumName="Selects")
        with mock.patch.object(gallery_mod, "get_resolve",
                               return_value=_resolve_with_gallery(gallery)):
            result = _unwrapped(gallery_mod.get_gallery_album_name)()
        self.assertEqual("Selects", result["album_name"])
        self.assertEqual(
            ("GetAlbumName", (album,), {}),
            [c for c in calls_of(gallery) if c[0] == "GetAlbumName"][0])

    def test_no_current_album_is_an_error_not_a_none_argument(self):
        gallery = _gallery(GetCurrentStillAlbum=None, SetAlbumName=True)
        with mock.patch.object(gallery_mod, "get_resolve",
                               return_value=_resolve_with_gallery(gallery)):
            result = _unwrapped(gallery_mod.set_gallery_album_name)("X")
        self.assertIn("No current gallery still album", result["error"])


class GalleryAlbumCreationTest(unittest.TestCase):
    """Finding 2: the Create*Album methods take no arguments."""

    def _create(self, tool_name, create_method):
        album = _album()
        gallery = _gallery(**{create_method: album, "SetAlbumName": True})
        with mock.patch.object(gallery_mod, "get_resolve",
                               return_value=_resolve_with_gallery(gallery)):
            named = _unwrapped(getattr(gallery_mod, tool_name))(album_name="Selects")
            unnamed = _unwrapped(getattr(gallery_mod, tool_name))()
        return gallery, album, named, unnamed

    def test_still_album_is_created_with_zero_args_then_renamed(self):
        gallery, album, named, unnamed = self._create(
            "create_gallery_still_album", "CreateGalleryStillAlbum")
        creates = [c for c in calls_of(gallery) if c[0] == "CreateGalleryStillAlbum"]
        self.assertEqual(2, len(creates))
        for call in creates:
            self.assertEqual((), call[1], "CreateGalleryStillAlbum() takes no arguments")
        self.assertTrue(named["success"])
        self.assertTrue(named["named"])
        self.assertEqual(
            ("SetAlbumName", (album, "Selects"), {}),
            [c for c in calls_of(gallery) if c[0] == "SetAlbumName"][0],
            "naming is a second call, SetAlbumName(album, name)")
        # The no-name path must not rename at all.
        self.assertTrue(unnamed["success"])
        self.assertNotIn("named", unnamed)

    def test_power_grade_album_is_created_with_zero_args_then_renamed(self):
        gallery, album, named, _unnamed = self._create(
            "create_gallery_power_grade_album", "CreateGalleryPowerGradeAlbum")
        creates = [c for c in calls_of(gallery) if c[0] == "CreateGalleryPowerGradeAlbum"]
        self.assertTrue(creates)
        for call in creates:
            self.assertEqual((), call[1])
        self.assertTrue(named["named"])

    def test_a_failed_rename_is_reported_not_swallowed(self):
        album = _album()
        gallery = _gallery(CreateGalleryStillAlbum=album, SetAlbumName=False)
        with mock.patch.object(gallery_mod, "get_resolve",
                               return_value=_resolve_with_gallery(gallery)):
            result = _unwrapped(gallery_mod.create_gallery_still_album)(album_name="Selects")
        self.assertTrue(result["success"])
        self.assertFalse(result["named"])
        self.assertIn("could not be renamed", result["warning"])


class TimelineMattesTest(unittest.TestCase):
    """Finding 3: the timeline-matte method takes ONLY the paths list."""

    def test_only_the_paths_list_is_passed(self):
        added = [ResolveBridgeDouble(methods={"GetName": "matte_a.exr"})]
        storage = ResolveBridgeDouble(methods={"AddTimelineMattesToMediaPool": added})
        resolve = ResolveBridgeDouble(methods={"GetMediaStorage": storage})
        with mock.patch.object(media_storage_mod, "get_resolve", return_value=resolve):
            result = _unwrapped(media_storage_mod.add_timeline_mattes_to_media_pool)(
                ["/tmp/matte_a.exr"])
        self.assertEqual(
            [("AddTimelineMattesToMediaPool", (["/tmp/matte_a.exr"],), {})],
            calls_of(storage),
            "a TimelineItem used to be passed where the paths list belongs",
        )
        # It returns [MediaPoolItems], not a bool.
        self.assertTrue(result["success"])
        self.assertEqual(1, result["added_count"])
        self.assertEqual(["matte_a.exr"], result["added"])

    def test_the_timeline_item_parameters_are_gone(self):
        import inspect

        params = inspect.signature(
            _unwrapped(media_storage_mod.add_timeline_mattes_to_media_pool)).parameters
        self.assertEqual(["matte_paths"], list(params))

    def test_an_empty_path_list_is_refused(self):
        storage = ResolveBridgeDouble(methods={"AddTimelineMattesToMediaPool": []})
        resolve = ResolveBridgeDouble(methods={"GetMediaStorage": storage})
        with mock.patch.object(media_storage_mod, "get_resolve", return_value=resolve):
            result = _unwrapped(media_storage_mod.add_timeline_mattes_to_media_pool)([])
        self.assertIn("non-empty", result["error"])
        self.assertEqual([], calls_of(storage))


class InsertGeneratorTest(unittest.TestCase):
    """Finding 4: InsertGeneratorIntoTimeline takes exactly one argument."""

    def test_exactly_one_argument_is_passed(self):
        item = ResolveBridgeDouble(methods={"GetName": "gen"})
        timeline = ResolveBridgeDouble(methods={"InsertGeneratorIntoTimeline": item})
        with mock.patch.object(timeline_mod, "_get_timeline",
                               return_value=(None, timeline, None)):
            result = _unwrapped(timeline_mod.timeline_insert_generator)("Solid Color")
        self.assertTrue(result["success"])
        self.assertEqual(
            [("InsertGeneratorIntoTimeline", ("Solid Color",), {})], calls_of(timeline))

    def test_the_phantom_duration_parameter_is_gone(self):
        import inspect

        params = inspect.signature(
            _unwrapped(timeline_mod.timeline_insert_generator)).parameters
        self.assertEqual(["generator_name"], list(params))


class AutoEditApplyDrxTest(unittest.TestCase):
    """Finding 5: ApplyGradeFromDRX is a Graph method, with two arguments."""

    def test_the_documented_two_arg_form_is_used_on_each_item_graph(self):
        import re

        from src.domains.auto_edit import actions as auto_edit_actions

        source = open(auto_edit_actions.__file__, encoding="utf-8").read()
        self.assertNotIn(
            "tl.ApplyGradeFromDRX", source,
            "Timeline.ApplyGradeFromDRX is fabricated - absent from dir(Timeline)",
        )
        # The graph-scoped, guarded call, matching granular/graph.py and
        # color_grade/actions.py.
        self.assertTrue(
            re.search(r"g\.ApplyGradeFromDRX\(drx_path, grade_mode\)", source),
            "expected the documented Graph.ApplyGradeFromDRX(path, gradeMode)",
        )
        self.assertIn('_has_method(g, "ApplyGradeFromDRX")', source)

    def test_a_timeline_really_does_fabricate_the_method(self):
        # The premise: this is why the old call was silently swallowed rather
        # than failing loudly.
        timeline = ResolveBridgeDouble(methods={"GetName": "Auto Edit"})
        self.assertIsNone(getattr(timeline, "ApplyGradeFromDRX"))
        with self.assertRaises(TypeError):
            timeline.ApplyGradeFromDRX("/tmp/x.drx", 0, [])


if __name__ == "__main__":
    unittest.main()
