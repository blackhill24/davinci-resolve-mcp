"""Offline tests for src/domains/project_lifecycle/utils/project_properties.py (previously untested).

A FakeProject stands in for the Resolve Project object; GetSetting returns
strings for numeric settings, mirroring how Resolve's API actually behaves.
"""
import unittest

from tests._error_envelope_helpers import assert_error_mentions

from src.domains.project_lifecycle.utils.project_properties import (
    get_all_project_properties,
    get_color_settings,
    get_project_info,
    get_project_metadata,
    get_project_property,
    get_timeline_format_settings,
    set_color_science_mode,
    set_color_space,
    set_project_property,
    set_superscale_settings,
    set_timeline_format,
    get_superscale_settings,
)


class FakeProject:
    def __init__(self, settings=None, all_settings_works=True):
        self.settings = dict(settings or {})
        self.all_settings_works = all_settings_works
        self.set_calls = []

    def GetSetting(self, name):
        if name == "":
            return dict(self.settings) if self.all_settings_works else None
        return self.settings.get(name)

    def SetSetting(self, name, value):
        self.set_calls.append((name, value))
        self.settings[name] = value
        return True


class FakeTimeline:
    def __init__(self, name, start=86400, end=86448):
        self.name = name
        self.start = start
        self.end = end

    def GetName(self):
        return self.name

    def GetStartFrame(self):
        return self.start

    def GetEndFrame(self):
        return self.end


class FakeMetadataProject(FakeProject):
    """FakeProject plus the handful of Project methods the metadata path calls.

    Deliberately has NO GetPath: `dir()` on the real Resolve Project object does
    not list it (live-verified on Studio 21.0.2.4), and `_has_method` tests
    membership in `dir()`, so the probe branch must stay skipped here.
    """

    def __init__(self, settings=None, timelines=(), current=None):
        super().__init__(settings)
        self.timelines = list(timelines)
        self._current = current

    def GetName(self):
        return "Fake Project"

    def GetCurrentTimeline(self):
        return self._current

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, index):
        if 1 <= index <= len(self.timelines):
            return self.timelines[index - 1]
        return None


class FakeProjectWithPath(FakeMetadataProject):
    def GetPath(self):
        return "/library/Fake Project"


class GetPropertyTest(unittest.TestCase):
    def test_int_coercion_from_string(self):
        proj = FakeProject({"timelineResolutionWidth": "1920"})
        self.assertEqual(get_project_property(proj, "timelineResolutionWidth"), 1920)

    def test_float_coercion_from_string(self):
        proj = FakeProject({"timelineFrameRate": "23.976"})
        self.assertAlmostEqual(get_project_property(proj, "timelineFrameRate"), 23.976)

    def test_bool_coercion_from_string(self):
        proj = FakeProject({"superScaleEnabled": "true"})
        self.assertIs(get_project_property(proj, "superScaleEnabled"), True)
        proj = FakeProject({"superScaleEnabled": "0"})
        self.assertIs(get_project_property(proj, "superScaleEnabled"), False)

    def test_uncoercible_value_passes_through(self):
        proj = FakeProject({"timelineFrameRate": "not-a-number"})
        self.assertEqual(get_project_property(proj, "timelineFrameRate"), "not-a-number")

    def test_none_project_errors(self):
        out = get_project_property(None, "timelineFrameRate")
        assert_error_mentions(self, out, 'Invalid project object')


class SetPropertyTest(unittest.TestCase):
    def test_int_conversion(self):
        proj = FakeProject()
        self.assertTrue(set_project_property(proj, "timelineResolutionWidth", "3840"))
        self.assertEqual(proj.set_calls, [("timelineResolutionWidth", 3840)])

    def test_bool_string_conversion(self):
        proj = FakeProject()
        set_project_property(proj, "superScaleEnabled", "yes")
        self.assertEqual(proj.set_calls, [("superScaleEnabled", True)])

    def test_none_project_returns_false(self):
        self.assertFalse(set_project_property(None, "x", 1))

    def test_uncoercible_int_is_passed_through_raw(self):
        # The write still goes out with Resolve's own value rather than being
        # dropped; only a warning is logged.
        proj = FakeProject()
        set_project_property(proj, "timelineResolutionWidth", "wide")
        self.assertEqual(proj.set_calls, [("timelineResolutionWidth", "wide")])

    def test_uncoercible_float_is_passed_through_raw(self):
        proj = FakeProject()
        set_project_property(proj, "timelineFrameRate", "fast")
        self.assertEqual(proj.set_calls, [("timelineFrameRate", "fast")])

    def test_unknown_property_skips_coercion(self):
        proj = FakeProject()
        set_project_property(proj, "someFutureSetting", "24")
        self.assertEqual(proj.set_calls, [("someFutureSetting", "24")])

    def test_raising_setsetting_returns_false(self):
        class ExplodingProject(FakeProject):
            def SetSetting(self, name, value):
                raise RuntimeError("bridge died")

        self.assertFalse(set_project_property(ExplodingProject(), "timelineFrameRate", 24))


class AllPropertiesTest(unittest.TestCase):
    def test_bulk_getsetting_path(self):
        proj = FakeProject({"timelineFrameRate": "24"})
        self.assertEqual(get_all_project_properties(proj), {"timelineFrameRate": "24"})

    def test_fallback_when_bulk_unavailable(self):
        proj = FakeProject({"timelineFrameRate": "24"}, all_settings_works=False)
        out = get_all_project_properties(proj)
        self.assertEqual(out.get("timelineFrameRate"), "24")
        self.assertNotIn("error", out)

    def test_fallback_skips_properties_that_raise(self):
        class PartlyBrokenProject(FakeProject):
            def GetSetting(self, name):
                if name == "":
                    return None
                if name == "colorScienceMode":
                    raise RuntimeError("bridge died")
                return self.settings.get(name)

        out = get_all_project_properties(PartlyBrokenProject({"timelineFrameRate": "24"}))
        self.assertEqual(out.get("timelineFrameRate"), "24")
        self.assertNotIn("colorScienceMode", out)
        self.assertNotIn("error", out)

    def test_raising_bulk_call_returns_error_envelope(self):
        class ExplodingProject(FakeProject):
            def GetSetting(self, name):
                raise RuntimeError("bridge died")

        assert_error_mentions(self, get_all_project_properties(ExplodingProject()), "bridge died")

    def test_none_project_errors(self):
        assert_error_mentions(self, get_all_project_properties(None), "Invalid project object")


class TimelineFormatTest(unittest.TestCase):
    def _proj(self, fps, w, h):
        return FakeProject({
            "timelineFrameRate": fps,
            "timelineResolutionWidth": w,
            "timelineResolutionHeight": h,
            "timelineOutputResolutionWidth": w,
            "timelineOutputResolutionHeight": h,
            "timelineInterlaceProcessing": "0",
        })

    def test_drop_frame_detection(self):
        out = get_timeline_format_settings(self._proj("29.97", "1920", "1080"))
        self.assertTrue(out["isDropFrame"])
        out = get_timeline_format_settings(self._proj("25", "1920", "1080"))
        self.assertFalse(out["isDropFrame"])

    def test_resolution_names(self):
        out = get_timeline_format_settings(self._proj("24", "3840", "2160"))
        self.assertEqual(out["resolutionName"], "UHD 4K")
        out = get_timeline_format_settings(self._proj("24", "1920", "1080"))
        self.assertEqual(out["resolutionName"], "FHD 1080p")

    def test_cinema_and_hd_resolution_names(self):
        cases = [
            ("1280", "720", "HD 720p"),
            ("4096", "2160", "DCI 4K"),
            ("4096", "2304", "DCI 4K"),
            ("2048", "1080", "DCI 2K"),
            ("2048", "1152", "DCI 2K"),
        ]
        for width, height, expected in cases:
            with self.subTest(resolution=f"{width}x{height}"):
                out = get_timeline_format_settings(self._proj("24", width, height))
                self.assertEqual(out["resolutionName"], expected)

    def test_nonstandard_resolution_gets_no_name(self):
        out = get_timeline_format_settings(self._proj("24", "1440", "1080"))
        self.assertNotIn("resolutionName", out)

    def test_5994_is_drop_frame(self):
        self.assertTrue(get_timeline_format_settings(self._proj("59.94", "1920", "1080"))["isDropFrame"])

    def test_uncoercible_frame_rate_is_not_drop_frame(self):
        # fps stays a string, so the numeric drop-frame test must be skipped
        # rather than raising.
        out = get_timeline_format_settings(self._proj("unknown", "1920", "1080"))
        self.assertIs(out["isDropFrame"], False)

    def test_none_project_errors(self):
        assert_error_mentions(self, get_timeline_format_settings(None), "Invalid project object")

    def test_set_timeline_format_reports_false_when_a_write_fails(self):
        class RefusingProject(FakeProject):
            def SetSetting(self, name, value):
                self.set_calls.append((name, value))
                return name != "timelineFrameRate"

        proj = RefusingProject()
        self.assertFalse(set_timeline_format(proj, 1920, 1080, 25.0))
        # A failed write must not short-circuit the remaining ones.
        self.assertIn("timelineInterlaceProcessing", dict(proj.set_calls))

    def test_set_timeline_format_none_project_returns_false(self):
        self.assertFalse(set_timeline_format(None, 1920, 1080, 25.0))

    def test_set_timeline_format_writes_all_four(self):
        proj = FakeProject()
        self.assertTrue(set_timeline_format(proj, 1920, 1080, 25.0, interlaced=True))
        written = dict(proj.set_calls)
        self.assertEqual(written["timelineResolutionWidth"], 1920)
        self.assertEqual(written["timelineResolutionHeight"], 1080)
        self.assertEqual(written["timelineFrameRate"], 25.0)
        self.assertEqual(written["timelineInterlaceProcessing"], 1)


class ColorScienceTest(unittest.TestCase):
    def test_string_mode_mapping(self):
        proj = FakeProject()
        self.assertTrue(set_color_science_mode(proj, "ACEScct"))
        self.assertEqual(proj.set_calls[-1], ("colorScienceMode", 2))

    def test_int_mode_passthrough(self):
        proj = FakeProject()
        self.assertTrue(set_color_science_mode(proj, 1))
        self.assertEqual(proj.set_calls[-1], ("colorScienceMode", 1))

    def test_invalid_mode_rejected(self):
        proj = FakeProject()
        self.assertFalse(set_color_science_mode(proj, "NotAMode"))
        self.assertEqual(proj.set_calls, [])


class SuperScaleTest(unittest.TestCase):
    def test_quality_name_mapping(self):
        proj = FakeProject({"superScaleEnabled": "1", "superScaleQuality": "1"})
        out = get_superscale_settings(proj)
        self.assertTrue(out["enabled"])
        self.assertEqual(out["quality"], 1)
        self.assertEqual(out["qualityName"], "Better Quality")

    def test_unknown_quality_gets_no_name(self):
        proj = FakeProject({"superScaleEnabled": "0", "superScaleQuality": "7"})
        out = get_superscale_settings(proj)
        self.assertIs(out["enabled"], False)
        self.assertNotIn("qualityName", out)

    def test_override_dimensions_included_only_when_present(self):
        with_override = get_superscale_settings(
            FakeProject({"superScaleEnabled": "1", "superScaleOverrideWidth": "3840"})
        )
        self.assertEqual(with_override["superScaleOverrideWidth"], "3840")
        self.assertNotIn("superScaleOverrideHeight", with_override)

    def test_set_writes_enabled_and_quality(self):
        proj = FakeProject()
        self.assertTrue(set_superscale_settings(proj, True, quality=2))
        self.assertEqual(dict(proj.set_calls), {"superScaleEnabled": True, "superScaleQuality": 2})

    def test_set_clamps_out_of_range_quality_to_auto(self):
        proj = FakeProject()
        self.assertTrue(set_superscale_settings(proj, True, quality=9))
        self.assertEqual(dict(proj.set_calls)["superScaleQuality"], 0)

    def test_set_none_project_returns_false(self):
        self.assertFalse(set_superscale_settings(None, True))

    def test_get_none_project_errors(self):
        assert_error_mentions(self, get_superscale_settings(None), "Invalid project object")


class ColorSettingsTest(unittest.TestCase):
    def test_science_mode_gets_descriptive_name(self):
        proj = FakeProject({"colorScienceMode": "1", "timelineColorSpace": "Rec.709"})
        out = get_color_settings(proj)
        self.assertEqual(out["colorScienceName"], "DaVinci YRGB Color Managed")
        self.assertEqual(out["timelineColorSpace"], "Rec.709")

    def test_unset_properties_are_omitted(self):
        out = get_color_settings(FakeProject({"colorScienceMode": "0"}))
        self.assertEqual(out["colorScienceName"], "DaVinci YRGB")
        self.assertNotIn("inputDRT", out)
        self.assertNotIn("timelineGamma", out)

    def test_unknown_science_mode_gets_no_name(self):
        out = get_color_settings(FakeProject({"colorScienceMode": "9"}))
        self.assertEqual(out["colorScienceMode"], 9)
        self.assertNotIn("colorScienceName", out)

    def test_none_project_errors(self):
        assert_error_mentions(self, get_color_settings(None), "Invalid project object")


class ColorSpaceTest(unittest.TestCase):
    def test_gamma_written_only_when_supplied(self):
        proj = FakeProject()
        self.assertTrue(set_color_space(proj, "Rec.2020"))
        self.assertEqual(proj.set_calls, [("timelineColorSpace", "Rec.2020")])

        proj = FakeProject()
        self.assertTrue(set_color_space(proj, "Rec.709", gamma="Gamma 2.4"))
        self.assertEqual(
            proj.set_calls,
            [("timelineColorSpace", "Rec.709"), ("timelineGamma", "Gamma 2.4")],
        )

    def test_failed_write_reports_false(self):
        class RefusingProject(FakeProject):
            def SetSetting(self, name, value):
                self.set_calls.append((name, value))
                return False

        self.assertFalse(set_color_space(RefusingProject(), "Rec.709"))

    def test_none_project_returns_false(self):
        self.assertFalse(set_color_space(None, "Rec.709"))


class ProjectMetadataTest(unittest.TestCase):
    def _project(self, current=None, timelines=()):
        return FakeMetadataProject(
            {
                "timelineFrameRate": "24",
                "timelineResolutionWidth": "1920",
                "timelineResolutionHeight": "1080",
                "colorScienceMode": "0",
                "superScaleEnabled": "1",
            },
            timelines=timelines,
            current=current,
        )

    def test_merges_format_color_and_superscale(self):
        timeline = FakeTimeline("Edit v1")
        out = get_project_metadata(self._project(current=timeline, timelines=[timeline]))
        self.assertEqual(out["name"], "Fake Project")
        self.assertEqual(out["currentTimeline"], "Edit v1")
        self.assertEqual(out["timelineCount"], 1)
        self.assertEqual(out["resolutionName"], "FHD 1080p")
        self.assertEqual(out["colorSettings"]["colorScienceName"], "DaVinci YRGB")
        self.assertTrue(out["superScale"]["enabled"])

    def test_no_current_timeline_omits_timeline_keys(self):
        out = get_project_metadata(self._project(current=None))
        self.assertNotIn("currentTimeline", out)
        self.assertNotIn("timelineCount", out)
        # The rest of the payload must still be present — the whole function
        # used to collapse to an error when one probe failed (#141 finding 1).
        self.assertEqual(out["name"], "Fake Project")
        self.assertIn("colorSettings", out)

    def test_path_probe_skipped_when_getpath_absent(self):
        self.assertNotIn("path", get_project_metadata(self._project()))

    def test_path_reported_when_a_build_grows_getpath(self):
        proj = FakeProjectWithPath({}, timelines=[], current=None)
        self.assertEqual(get_project_metadata(proj)["path"], "/library/Fake Project")

    def test_none_project_errors(self):
        assert_error_mentions(self, get_project_metadata(None), "Invalid project object")

    def test_raising_project_returns_error_envelope(self):
        class ExplodingProject(FakeMetadataProject):
            def GetName(self):
                raise RuntimeError("bridge died")

        assert_error_mentions(self, get_project_metadata(ExplodingProject()), "bridge died")


class ProjectInfoTest(unittest.TestCase):
    def test_lists_timelines_and_flags_the_current_one(self):
        first = FakeTimeline("Edit v1", start=86400, end=86448)
        second = FakeTimeline("Edit v2", start=0, end=100)
        proj = FakeMetadataProject(
            {"timelineFrameRate": "24"}, timelines=[first, second], current=second
        )
        out = get_project_info(proj)

        self.assertEqual(out["name"], "Fake Project")
        self.assertEqual([t["name"] for t in out["timelines"]], ["Edit v1", "Edit v2"])
        self.assertEqual([t["isCurrent"] for t in out["timelines"]], [False, True])
        # GetEndFrame is EXCLUSIVE, so a 48-frame timeline is end - start.
        self.assertEqual(out["timelines"][0]["duration"], 48)
        self.assertIn("metadata", out)
        self.assertIn("settings", out)

    def test_no_timelines_yields_empty_list(self):
        out = get_project_info(FakeMetadataProject({}, timelines=[], current=None))
        self.assertEqual(out["timelines"], [])

    def test_none_project_errors(self):
        assert_error_mentions(self, get_project_info(None), "Invalid project object")


if __name__ == "__main__":
    unittest.main()
