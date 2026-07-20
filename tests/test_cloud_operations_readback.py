"""Tests for the cloud-project readback guard (issue #25, stage 5 task 5.3).

ImportCloudProject/RestoreCloudProject return an advisory bool only (same
unreliable-return pattern as AutoSyncAudio) — success must be confirmed by
reading the project list back, not by trusting the return value.
"""
import unittest

from src.utils.cloud_operations import (
    CLOUD_SYNC_STATUS_LABELS,
    cloud_sync_status_label,
    import_cloud_project,
    restore_cloud_project,
)


class FakeProjectManager:
    def __init__(self, *, import_returns=True, restore_returns=True, appears=True):
        self._import_returns = import_returns
        self._restore_returns = restore_returns
        self._appears = appears
        self._projects = ["Existing Project"]

    def ImportCloudProject(self, file_path, settings):
        if self._appears:
            self._projects.append("Imported Project")
        return self._import_returns

    def RestoreCloudProject(self, folder_path, settings):
        if self._appears:
            self._projects.append("Restored Project")
        return self._restore_returns

    def GetProjectListInCurrentFolder(self):
        return list(self._projects)


class FakeResolve:
    CLOUD_SETTING_PROJECT_NAME = "__NAME__"
    CLOUD_SETTING_PROJECT_MEDIA_PATH = "__MEDIA__"
    CLOUD_SETTING_IS_COLLAB = "__COLLAB__"
    CLOUD_SETTING_SYNC_MODE = "__SYNCMODE__"
    CLOUD_SETTING_IS_CAMERA_ACCESS = "__CAM__"
    CLOUD_SYNC_NONE = "__SYNC_NONE__"
    CLOUD_SYNC_PROXY_ONLY = "__SYNC_PROXY__"
    CLOUD_SYNC_PROXY_AND_ORIG = "__SYNC_BOTH__"

    def __init__(self, pm):
        self._pm = pm

    def GetProjectManager(self):
        return self._pm


class ImportRestoreReadbackTest(unittest.TestCase):
    def test_import_verified_when_project_appears(self):
        pm = FakeProjectManager(import_returns=True, appears=True)
        out = import_cloud_project(FakeResolve(pm), file_path="/tmp/x.zip", project_name="Imported Project")
        self.assertTrue(out["success"])
        self.assertTrue(out["verified"])

    def test_import_contradiction_when_api_lies(self):
        # API reports success but nothing actually appeared in the project list.
        pm = FakeProjectManager(import_returns=True, appears=False)
        out = import_cloud_project(FakeResolve(pm), file_path="/tmp/x.zip", project_name="Imported Project")
        self.assertTrue(out["success"])
        self.assertFalse(out["verified"])

    def test_import_falls_back_to_count_delta_without_project_name(self):
        pm = FakeProjectManager(import_returns=True, appears=True)
        out = import_cloud_project(FakeResolve(pm), file_path="/tmp/x.zip")
        self.assertTrue(out["success"])
        self.assertTrue(out["verified"])
        self.assertEqual(len(out["projects_after"]) - len(out["projects_before"]), 1)

    def test_restore_verified_when_project_appears(self):
        pm = FakeProjectManager(restore_returns=True, appears=True)
        out = restore_cloud_project(FakeResolve(pm), folder_path="/tmp/folder", project_name="Restored Project")
        self.assertTrue(out["success"])
        self.assertTrue(out["verified"])

    def test_restore_contradiction_when_api_lies(self):
        pm = FakeProjectManager(restore_returns=True, appears=False)
        out = restore_cloud_project(FakeResolve(pm), folder_path="/tmp/folder", project_name="Restored Project")
        self.assertTrue(out["success"])
        self.assertFalse(out["verified"])

    def test_method_missing_reports_error_not_crash(self):
        class NoImportPM(FakeProjectManager):
            def __getattribute__(self, name):
                if name == "ImportCloudProject":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        out = import_cloud_project(FakeResolve(NoImportPM()), file_path="/tmp/x.zip")
        self.assertFalse(out["success"])
        self.assertIn("error", out)


class CloudSyncStatusLabelTest(unittest.TestCase):
    def test_known_values_map_to_labels(self):
        self.assertEqual(cloud_sync_status_label(-1), "default")
        self.assertEqual(cloud_sync_status_label(10), "success")
        self.assertEqual(cloud_sync_status_label(7), "upload_success")

    def test_all_documented_enum_values_covered(self):
        # docs/reference/resolve_scripting_api.txt lines 667-681: -1 through 10.
        self.assertEqual(set(CLOUD_SYNC_STATUS_LABELS.keys()), set(range(-1, 11)))

    def test_unrecognized_value_returns_none(self):
        self.assertIsNone(cloud_sync_status_label(99))
        self.assertIsNone(cloud_sync_status_label(None))
        self.assertIsNone(cloud_sync_status_label("not-a-number"))


if __name__ == "__main__":
    unittest.main()
