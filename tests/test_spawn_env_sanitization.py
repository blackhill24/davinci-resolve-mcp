"""Spawned children must never inherit session preloads that crash them.

NoMachine exports LD_PRELOAD=/usr/NX/lib/libnxegl.so into the desktop session.
Two classes of children die from it: Resolve segfaults during NVIDIA GL
context creation as soon as it races a plugin dlopen (so an MCP-launched
Resolve crashed within seconds of page switches), and CUDA/cuDNN users
(whisper, GPU ffmpeg) abort with "Cannot load symbol cudnnGetVersion".
Every launch site must go through proc.sanitized_spawn_env().
"""
import unittest
from unittest import mock

import src.granular.common as granular_common
import src.server as server
import src.utils.app_control as app_control
import src.utils.media_analysis as media_analysis
from src.utils.proc import sanitized_spawn_env

NXEGL = "/usr/NX/lib/libnxegl.so"


class SanitizedSpawnEnvTest(unittest.TestCase):
    def test_nxegl_only_preload_is_dropped(self):
        env = sanitized_spawn_env({"LD_PRELOAD": NXEGL, "HOME": "/home/x"})
        self.assertNotIn("LD_PRELOAD", env)
        self.assertEqual(env["HOME"], "/home/x")

    def test_other_preloads_survive(self):
        env = sanitized_spawn_env({"LD_PRELOAD": f"/usr/lib/libjemalloc.so:{NXEGL}"})
        self.assertEqual(env["LD_PRELOAD"], "/usr/lib/libjemalloc.so")

    def test_space_separated_preload_list(self):
        env = sanitized_spawn_env({"LD_PRELOAD": f"{NXEGL} /usr/lib/libfoo.so"})
        self.assertEqual(env["LD_PRELOAD"], "/usr/lib/libfoo.so")

    def test_env_without_preload_passes_through(self):
        env = sanitized_spawn_env({"PATH": "/usr/bin"})
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_defaults_to_os_environ(self):
        with mock.patch.dict("os.environ", {"LD_PRELOAD": NXEGL}, clear=False):
            self.assertNotIn(
                "libnxegl", sanitized_spawn_env().get("LD_PRELOAD", "")
            )


class LaunchSitesUseSanitizedEnvTest(unittest.TestCase):
    """Every Linux Resolve spawn passes a sanitized env and detaches the session."""

    def _assert_popen_sanitized(self, popen):
        self.assertTrue(popen.called)
        kwargs = popen.call_args.kwargs
        self.assertIn("env", kwargs)
        self.assertNotIn("libnxegl", kwargs["env"].get("LD_PRELOAD", ""))
        self.assertTrue(kwargs.get("start_new_session"))

    def test_granular_launch_resolve(self):
        with mock.patch.dict("os.environ", {"LD_PRELOAD": NXEGL}, clear=False), \
             mock.patch.object(granular_common.subprocess, "Popen") as popen, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(granular_common.platform, "system", return_value="Linux"), \
             mock.patch.object(granular_common.time, "sleep"), \
             mock.patch.object(granular_common, "_try_connect", return_value=True):
            self.assertTrue(granular_common._launch_resolve())
        self._assert_popen_sanitized(popen)

    def test_server_launch_resolve(self):
        with mock.patch.dict("os.environ", {"LD_PRELOAD": NXEGL}, clear=False), \
             mock.patch.object(server.subprocess, "Popen") as popen, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(server.platform, "system", return_value="Linux"), \
             mock.patch.object(server.time, "sleep"), \
             mock.patch.object(server, "_try_connect", return_value=True):
            self.assertTrue(server._launch_resolve())
        self._assert_popen_sanitized(popen)

    def test_restart_resolve_app(self):
        with mock.patch.dict("os.environ", {"LD_PRELOAD": NXEGL}, clear=False), \
             mock.patch.object(app_control.subprocess, "Popen") as popen, \
             mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control.time, "sleep"), \
             mock.patch.object(app_control, "quit_resolve_app", return_value=True):
            self.assertTrue(
                app_control.restart_resolve_app(resolve_obj=mock.Mock(), wait_seconds=0)
            )
        self._assert_popen_sanitized(popen)


class MediaAnalysisSubprocessEnvTest(unittest.TestCase):
    """_run_command (whisper / ffmpeg runner) spawns with a sanitized env."""

    def test_run_command_passes_sanitized_env(self):
        completed = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.dict("os.environ", {"LD_PRELOAD": NXEGL}, clear=False), \
             mock.patch.object(
                 media_analysis.subprocess, "run", return_value=completed
             ) as run:
            code, _, _ = media_analysis._run_command(["true"])
        self.assertEqual(code, 0)
        kwargs = run.call_args.kwargs
        self.assertIn("env", kwargs)
        self.assertNotIn("libnxegl", kwargs["env"].get("LD_PRELOAD", ""))


if __name__ == "__main__":
    unittest.main()
