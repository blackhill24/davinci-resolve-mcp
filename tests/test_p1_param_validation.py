"""Tests for deep-QC P1 1b: required-param validation returns structured errors
instead of crashing with KeyError on omitted params."""
import unittest

from tests._error_envelope_helpers import assert_error_mentions
from unittest import mock

import src.server as s
import src.domains.timeline_edit.actions as _dom_timeline_edit
import src.domains.project_lifecycle.actions as _dom_project_lifecycle


class TimelineParamValidationTest(unittest.TestCase):
    def test_set_current_missing_index_errors(self):
        with mock.patch.object(s, "_check", return_value=(mock.Mock(), mock.Mock(), None)):
            out = s.timeline("set_current", {})
        self.assertIn("error", out)
        self.assertNotIn("success", out)  # did not crash / did not proceed

    def test_set_current_valid_index_proceeds(self):
        fake_proj = mock.Mock()
        fake_tl = mock.Mock()
        fake_proj.GetTimelineByIndex.return_value = fake_tl
        fake_proj.SetCurrentTimeline.return_value = True
        with mock.patch.object(_dom_timeline_edit, "_check", return_value=(mock.Mock(), fake_proj, None)):
            out = s.timeline("set_current", {"index": 2})
        self.assertTrue(out.get("success"))
        fake_proj.GetTimelineByIndex.assert_called_once_with(2)

    def test_add_track_missing_track_type_errors(self):
        fake_proj = mock.Mock()
        fake_proj.GetCurrentTimeline.return_value = mock.Mock()
        # Patch the module that actually owns the dispatch. Patching
        # src.server._check does nothing here since the #52 restructure, so this
        # test was reaching the developer's RUNNING Resolve and passing on
        # whatever error came back (#121 task 3, shape 2).
        with mock.patch.object(_dom_timeline_edit, "_check", return_value=(mock.Mock(), fake_proj, None)):
            out = s.timeline("add_track", {})
        assert_error_mentions(self, out, 'track_type', 'required')


class ProjectManagerParamValidationTest(unittest.TestCase):
    def _fake_resolve(self):
        r = mock.Mock()
        r.GetProjectManager.return_value = mock.Mock()
        return r

    def _patched_resolve(self):
        # Patch the module that actually owns the dispatch, for the same reason
        # spelled out in TimelineParamValidationTest above: since the #52
        # restructure `project_manager` lives in the project_lifecycle domain
        # and holds its own `get_resolve` binding, so patching src.server did
        # nothing. These three reached the developer's RUNNING Resolve and
        # passed on whatever came back; on a Resolve-less runner the connection
        # error masked the validation error they exist to assert (#224).
        return mock.patch.object(
            _dom_project_lifecycle, "get_resolve", return_value=self._fake_resolve())

    def test_create_missing_name_errors(self):
        with self._patched_resolve():
            out = s.project_manager("create", {})
        assert_error_mentions(self, out, 'create requires name')

    def test_export_project_missing_path_errors(self):
        with self._patched_resolve():
            out = s.project_manager("export_project", {"name": "X"})
        assert_error_mentions(self, out, 'path', 'required')

    def test_archive_missing_both_errors(self):
        with self._patched_resolve():
            out = s.project_manager("archive", {})
        assert_error_mentions(self, out, 'name', 'required')


if __name__ == "__main__":
    unittest.main()
