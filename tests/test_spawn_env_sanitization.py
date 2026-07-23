"""Spawned children must never inherit session preloads that crash them.

NoMachine exports LD_PRELOAD=/usr/NX/lib/libnxegl.so into the desktop session.
Two classes of children die from it: Resolve segfaults during NVIDIA GL
context creation as soon as it races a plugin dlopen (so an MCP-launched
Resolve crashed within seconds of page switches), and CUDA/cuDNN users
(whisper, GPU ffmpeg) abort with "Cannot load symbol cudnnGetVersion".
Every launch site must go through proc.sanitized_spawn_env().
"""
import os
import tempfile
import unittest
from unittest import mock

import src.granular.common as granular_common
import src.server as server
import src.core.live_connection as live_connection
import src.core.app_control as app_control
import src.domains.media_analysis.utils.technical_probe as media_analysis
from src.core.proc import preload_audit, resolve_spawn_env, sanitized_spawn_env

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


class ResolveSpawnEnvTest(unittest.TestCase):
    """Resolve launches get an ALSA raw-hw override so Fairlight's duplex audio
    engine initializes; against the PipeWire/Pulse ALSA plugins it retry-loops
    forever and every render stalls at 0% (LoadFairlightAudioSamples never
    returns). See memory/resolve-headless-render-hang for the live diagnosis."""

    def _fake_asound(self, root, devices):
        """devices: list of (card, dev, direction, status_text)."""
        lines = []
        for card, dev, direction, status in devices:
            lines.append(f"{card:02d}-{dev:02d}: Dev {card}.{dev} : Dev : {direction} 1\n")
            sub = os.path.join(root, f"card{card}", f"pcm{dev}{direction[0]}", "sub0")
            os.makedirs(sub, exist_ok=True)
            with open(os.path.join(sub, "status"), "w", encoding="utf-8") as fh:
                fh.write(status)
        with open(os.path.join(root, "pcm"), "w", encoding="utf-8") as fh:
            fh.writelines(lines)

    def test_picks_first_free_playback_and_capture(self):
        with tempfile.TemporaryDirectory() as root:
            self._fake_asound(root, [
                (1, 0, "playback", "state: RUNNING"),   # held (e.g. PipeWire mmap)
                (0, 3, "playback", "closed"),
                (1, 2, "capture", "closed"),
            ])
            env = resolve_spawn_env({}, proc_asound=root, conf_dir=root)
            conf_path = env.get("ALSA_CONFIG_PATH")
            self.assertTrue(conf_path and os.path.exists(conf_path))
            with open(conf_path, encoding="utf-8") as fh:
                conf = fh.read()
            self.assertIn("type hw; card 0; device 3", conf)
            self.assertIn("type hw; card 1; device 2", conf)
            # Must not INCLUDE the system alsa.conf: its conf.d hooks re-apply
            # the pipewire default after any override in this file. Checked on
            # directive lines only — the header comment names the file to
            # explain why it is excluded, which is not an include.
            directives = [
                line for line in conf.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            self.assertFalse(
                [line for line in directives if "alsa.conf" in line or line.lstrip().startswith("<")],
                "generated conf must not pull in the system alsa.conf",
            )

    def test_defines_hw_names_for_by_name_opens(self):
        """Resolve opens `hw:0`/`hw:1` by name for the mixer.

        A self-contained conf has none of alsa.conf's `hw` name definitions, so
        without these blocks every such open fails with "Invalid CTL hw:0" —
        observed repeating in ResolveDebug.txt (issue #93).
        """
        with tempfile.TemporaryDirectory() as root:
            self._fake_asound(root, [
                (0, 3, "playback", "closed"),
                (1, 2, "capture", "closed"),
            ])
            env = resolve_spawn_env({}, proc_asound=root, conf_dir=root)
            with open(env["ALSA_CONFIG_PATH"], encoding="utf-8") as fh:
                conf = fh.read()
            self.assertIn("ctl.hw {", conf)
            self.assertIn("pcm.hw {", conf)
            self.assertIn("@args.CARD", conf)

    def test_no_free_duplex_pair_leaves_env_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            self._fake_asound(root, [
                (0, 0, "playback", "state: RUNNING"),
                (0, 0, "capture", "closed"),
            ])
            env = resolve_spawn_env({"PATH": "/usr/bin"}, proc_asound=root, conf_dir=root)
            self.assertNotIn("ALSA_CONFIG_PATH", env)

    def test_missing_proc_asound_is_harmless(self):
        env = resolve_spawn_env({"PATH": "/usr/bin"}, proc_asound="/nonexistent-asound")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("ALSA_CONFIG_PATH", env)

    def test_existing_alsa_config_path_is_respected(self):
        with tempfile.TemporaryDirectory() as root:
            self._fake_asound(root, [
                (0, 3, "playback", "closed"),
                (1, 2, "capture", "closed"),
            ])
            env = resolve_spawn_env(
                {"ALSA_CONFIG_PATH": "/etc/mine.conf"}, proc_asound=root, conf_dir=root
            )
            self.assertEqual(env["ALSA_CONFIG_PATH"], "/etc/mine.conf")

    def test_still_sanitizes_preload(self):
        env = resolve_spawn_env({"LD_PRELOAD": NXEGL}, proc_asound="/nonexistent-asound")
        self.assertNotIn("LD_PRELOAD", env)


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
             mock.patch.object(live_connection.subprocess, "Popen") as popen, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(live_connection.platform, "system", return_value="Linux"), \
             mock.patch.object(live_connection.time, "sleep"), \
             mock.patch.object(live_connection, "_try_connect", return_value=True):
            self.assertTrue(live_connection._launch_resolve())
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


class PreloadAuditTest(unittest.TestCase):
    """The server must be able to see its OWN poisoned env: spawn sanitization
    can't protect in-process CUDA/GL, so boot and the status tool audit it."""

    def test_poisoned_env_is_flagged(self):
        audit = preload_audit({"LD_PRELOAD": NXEGL})
        self.assertTrue(audit["poisoned"])
        self.assertEqual(audit["crashy_entries"], [NXEGL])
        self.assertIn("libnxegl", audit["message"])

    def test_poisoned_among_benign_preloads(self):
        audit = preload_audit({"LD_PRELOAD": f"/usr/lib/libjemalloc.so:{NXEGL}"})
        self.assertTrue(audit["poisoned"])
        self.assertEqual(audit["crashy_entries"], [NXEGL])

    def test_clean_env_is_not_flagged(self):
        audit = preload_audit({"LD_PRELOAD": "/usr/lib/libjemalloc.so"})
        self.assertFalse(audit["poisoned"])
        self.assertEqual(audit["crashy_entries"], [])
        self.assertIsNone(audit["message"])

    def test_no_preload_is_not_flagged(self):
        audit = preload_audit({"PATH": "/usr/bin"})
        self.assertFalse(audit["poisoned"])
        self.assertEqual(audit["preload"], "")

    def test_env_audit_action_reports_poisoning(self):
        """resolve_control(env_audit) surfaces the process env, no connection."""
        with mock.patch.dict("os.environ", {"LD_PRELOAD": NXEGL}, clear=False):
            result = server.resolve_control("env_audit")
        self.assertTrue(result["poisoned"])
        self.assertIn(NXEGL, result["crashy_entries"])


if __name__ == "__main__":
    unittest.main()
