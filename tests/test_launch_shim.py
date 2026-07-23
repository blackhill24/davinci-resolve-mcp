"""Contract tests for the Resolve launch shim (issue #93).

The shim exists so the Fairlight raw-hw ALSA config applies however Resolve is
started, not only when the connector spawns it. These tests run fully offline:
HOME is redirected to a temp dir so nothing touches the real ~/.local.
"""
import os
import platform
import tempfile
import unittest
from unittest import mock

import src.core.launch_shim as launch_shim


SYSTEM_ENTRY = """\
[Desktop Entry]
Version=1.0
Type=Application
Name=DaVinci Resolve
Path=/opt/resolve/
Exec=/opt/resolve/bin/resolve %u
Terminal=false
Icon=davinci-resolve
"""


class LaunchShimTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

        # Pretend Linux with Resolve installed unless a test says otherwise.
        p = mock.patch.object(launch_shim, "_is_linux", return_value=True)
        p.start()
        self.addCleanup(p.stop)

        exists = mock.patch("os.path.exists", side_effect=self._exists)
        self._real_exists = os.path.exists
        exists.start()
        self.addCleanup(exists.stop)

    def _exists(self, path):
        if path == launch_shim.RESOLVE_BINARY:
            return True
        return self._real_exists(path)


class InstallTest(LaunchShimTestBase):
    def test_install_writes_executable_shim_and_desktop_override(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            result = launch_shim.install()

        self.assertTrue(result["success"], result)
        shim = launch_shim.shim_path()
        desktop = launch_shim.desktop_entry_path()

        self.assertTrue(self._real_exists(shim))
        self.assertTrue(os.access(shim, os.X_OK), "shim must be executable")
        self.assertTrue(self._real_exists(desktop))

        with open(shim, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("resolve_spawn_env", body)
        self.assertIn(launch_shim.RESOLVE_BINARY, body)
        self.assertIn(launch_shim.SHIM_MARKER, body)

    def test_desktop_override_points_at_shim_and_keeps_field_codes(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            launch_shim.install()

        with open(launch_shim.desktop_entry_path(), encoding="utf-8") as handle:
            entry = handle.read()

        self.assertIn(f"Exec={launch_shim.shim_path()} %u", entry)
        self.assertNotIn("Exec=/opt/resolve/bin/resolve", entry)
        # Non-Exec lines survive untouched.
        self.assertIn("Icon=davinci-resolve", entry)
        self.assertIn("Name=DaVinci Resolve", entry)
        self.assertIn("Path=/opt/resolve/", entry)

    def test_desktop_override_opens_with_the_group_header(self):
        """A comment above [Desktop Entry] trips stricter .desktop parsers."""
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            launch_shim.install()

        with open(launch_shim.desktop_entry_path(), encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        self.assertEqual(lines[0], "[Desktop Entry]")
        self.assertIn(launch_shim.SHIM_MARKER, lines[1])

    def test_install_is_idempotent(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            first = launch_shim.install()
            second = launch_shim.install()

        self.assertTrue(first["success"])
        self.assertTrue(second["success"], "re-installing over our own files must succeed")

    def test_falls_back_when_system_entry_unreadable(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=None):
            result = launch_shim.install()

        self.assertTrue(result["success"], result)
        with open(launch_shim.desktop_entry_path(), encoding="utf-8") as handle:
            entry = handle.read()
        self.assertIn(f"Exec={launch_shim.shim_path()} %u", entry)
        self.assertIn("Name=DaVinci Resolve", entry)

    def test_refuses_to_overwrite_a_file_we_did_not_write(self):
        shim = launch_shim.shim_path()
        os.makedirs(os.path.dirname(shim), exist_ok=True)
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n# hand-written by the user\n")

        result = launch_shim.install()

        self.assertFalse(result["success"])
        self.assertIn("refusing to overwrite", result["error"])
        with open(shim, encoding="utf-8") as handle:
            self.assertIn("hand-written by the user", handle.read())

    def test_refuses_when_resolve_binary_absent(self):
        with mock.patch("os.path.exists", return_value=False):
            result = launch_shim.install()
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])


class UninstallTest(LaunchShimTestBase):
    def test_uninstall_removes_only_our_files(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            launch_shim.install()

        result = launch_shim.uninstall()

        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["removed"]), 2)
        self.assertFalse(self._real_exists(launch_shim.shim_path()))
        self.assertFalse(self._real_exists(launch_shim.desktop_entry_path()))

    def test_uninstall_skips_foreign_files(self):
        desktop = launch_shim.desktop_entry_path()
        os.makedirs(os.path.dirname(desktop), exist_ok=True)
        with open(desktop, "w", encoding="utf-8") as handle:
            handle.write(SYSTEM_ENTRY)

        result = launch_shim.uninstall()

        self.assertTrue(result["success"])
        self.assertEqual(result["removed"], [])
        self.assertIn(desktop, result["skipped_not_ours"])
        self.assertTrue(self._real_exists(desktop), "a foreign file must survive uninstall")

    def test_uninstall_is_idempotent_when_nothing_installed(self):
        result = launch_shim.uninstall()
        self.assertTrue(result["success"])
        self.assertEqual(result["removed"], [])


class StatusTest(LaunchShimTestBase):
    def test_status_reports_not_installed_before_install(self):
        status = launch_shim.status()
        self.assertTrue(status["supported"])
        self.assertFalse(status["installed"])

    def test_status_reports_installed_after_install(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            launch_shim.install()

        with mock.patch("shutil.which", return_value=launch_shim.shim_path()):
            status = launch_shim.status()

        self.assertTrue(status["installed"])
        self.assertTrue(status["shim"]["installed"])
        self.assertTrue(status["desktop_entry"]["installed"])

    def test_status_warns_when_path_resolves_elsewhere(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            launch_shim.install()

        with mock.patch("shutil.which", return_value="/opt/resolve/bin/resolve"):
            status = launch_shim.status()

        self.assertTrue(any("not the shim" in w for w in status["warnings"]), status["warnings"])

    def test_status_warns_when_not_on_path_at_all(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            launch_shim.install()

        with mock.patch("shutil.which", return_value=None):
            status = launch_shim.status()

        self.assertTrue(any("not on PATH" in w for w in status["warnings"]), status["warnings"])


class NonLinuxTest(unittest.TestCase):
    """Every entry point must no-op cleanly off Linux rather than writing files."""

    def setUp(self):
        p = mock.patch.object(launch_shim, "_is_linux", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_install_refuses(self):
        result = launch_shim.install()
        self.assertFalse(result["success"])
        self.assertFalse(result["supported"])

    def test_uninstall_refuses(self):
        result = launch_shim.uninstall()
        self.assertFalse(result["success"])
        self.assertFalse(result["supported"])

    def test_status_reports_unsupported(self):
        status = launch_shim.status()
        self.assertFalse(status["supported"])
        self.assertFalse(status["installed"])


class LaunchAdvisoryTest(LaunchShimTestBase):
    """The advisory fires only for the silent-bypass case (issue #95)."""

    def _install(self):
        with mock.patch.object(launch_shim, "_desktop_entry_source", return_value=SYSTEM_ENTRY):
            launch_shim.install()

    def test_silent_when_shim_not_installed(self):
        """Declining the shim is a legitimate choice, not something to nag about."""
        with mock.patch("shutil.which", return_value="/opt/resolve/bin/resolve"):
            self.assertIsNone(launch_shim.launch_advisory())

    def test_silent_when_shim_is_effective(self):
        self._install()
        with mock.patch("shutil.which", return_value=launch_shim.shim_path()):
            self.assertIsNone(launch_shim.launch_advisory())

    def test_fires_when_shadowed_on_path(self):
        self._install()
        with mock.patch("shutil.which", return_value="/opt/resolve/bin/resolve"):
            advisory = launch_shim.launch_advisory()

        self.assertIsNotNone(advisory)
        self.assertTrue(advisory["installed"])
        self.assertFalse(advisory["effective"])
        self.assertEqual(advisory["resolve_on_path"], "/opt/resolve/bin/resolve")
        self.assertTrue(advisory["warnings"])
        self.assertIn("wedge", advisory["impact"])

    def test_fires_when_absent_from_path(self):
        self._install()
        with mock.patch("shutil.which", return_value=None):
            advisory = launch_shim.launch_advisory()

        self.assertIsNotNone(advisory)
        self.assertFalse(advisory["effective"])

    def test_never_raises(self):
        """An advisory must not be able to fail a launch."""
        self._install()
        with mock.patch.object(launch_shim, "status", side_effect=RuntimeError("boom")):
            self.assertIsNone(launch_shim.launch_advisory())

    def test_silent_off_linux(self):
        with mock.patch.object(launch_shim, "_is_linux", return_value=False):
            self.assertIsNone(launch_shim.launch_advisory())


class LaunchAdvisoryWiringTest(unittest.TestCase):
    """Both pre-render entry points carry the advisory."""

    def test_resolve_control_launch_includes_advisory_when_bypassed(self):
        import src.server as compound

        advisory = {"installed": True, "effective": False, "warnings": ["w"], "impact": "x"}
        with mock.patch.object(compound, "get_resolve", return_value=object()), \
                mock.patch.object(compound._launch_shim, "launch_advisory", return_value=advisory):
            out = compound.resolve_control("launch")

        self.assertTrue(out["success"])
        self.assertEqual(out["launch_shim"], advisory)

    def test_resolve_control_launch_omits_advisory_when_healthy(self):
        import src.server as compound

        with mock.patch.object(compound, "get_resolve", return_value=object()), \
                mock.patch.object(compound._launch_shim, "launch_advisory", return_value=None):
            out = compound.resolve_control("launch")

        self.assertTrue(out["success"])
        self.assertNotIn("launch_shim", out)

    def test_preflight_advisory_swallows_import_and_call_failures(self):
        import tests.preflight as preflight

        with mock.patch.object(launch_shim, "launch_advisory", side_effect=RuntimeError("boom")):
            self.assertIsNone(preflight._launch_shim_advisory())

    def test_preflight_advisory_passes_through(self):
        import tests.preflight as preflight

        advisory = {"installed": True, "effective": False, "warnings": [], "impact": "x"}
        with mock.patch.object(launch_shim, "launch_advisory", return_value=advisory):
            self.assertEqual(preflight._launch_shim_advisory(), advisory)


class DispatchTest(unittest.TestCase):
    """The three actions must be reachable through resolve_control without a
    live Resolve — the whole point is fixing how Resolve gets started."""

    def test_actions_dispatch_without_a_resolve_connection(self):
        import src.server as compound

        for action, fn in (
            ("launch_shim_status", "status"),
            ("install_launch_shim", "install"),
            ("uninstall_launch_shim", "uninstall"),
        ):
            with self.subTest(action=action):
                sentinel = {"success": True, "probe": action}
                with mock.patch.object(compound._launch_shim, fn, return_value=sentinel) as stub:
                    out = compound.resolve_control(action)
                stub.assert_called_once()
                self.assertEqual(out, sentinel)

    def test_actions_are_listed_for_unknown_action_errors(self):
        import src.server as compound

        out = compound.resolve_control("no_such_action_at_all")
        available = str(out)
        for action in ("install_launch_shim", "uninstall_launch_shim", "launch_shim_status"):
            self.assertIn(action, available)


if __name__ == "__main__":
    unittest.main()
