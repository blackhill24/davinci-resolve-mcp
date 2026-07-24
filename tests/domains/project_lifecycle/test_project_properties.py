"""Offline tests for src/domains/project_lifecycle/utils/project_properties.py (previously untested).

A FakeProject stands in for the Resolve Project object; GetSetting returns
strings for numeric settings, mirroring how Resolve's API actually behaves.
"""
import unittest

from src.domains.project_lifecycle.utils.project_properties import (
    get_all_project_properties,
    get_project_property,
    get_timeline_format_settings,
    set_color_science_mode,
    set_project_property,
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
        self.assertIn("error", out)


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


class AllPropertiesTest(unittest.TestCase):
    def test_bulk_getsetting_path(self):
        proj = FakeProject({"timelineFrameRate": "24"})
        self.assertEqual(get_all_project_properties(proj), {"timelineFrameRate": "24"})

    def test_fallback_when_bulk_unavailable(self):
        proj = FakeProject({"timelineFrameRate": "24"}, all_settings_works=False)
        out = get_all_project_properties(proj)
        self.assertEqual(out.get("timelineFrameRate"), "24")
        self.assertNotIn("error", out)


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


if __name__ == "__main__":
    unittest.main()
