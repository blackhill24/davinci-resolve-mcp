"""Spawned children must never inherit session preloads that crash them.

NoMachine exports LD_PRELOAD=/usr/NX/lib/libnxegl.so into the desktop session.
Two classes of children die from it: Resolve segfaults during NVIDIA GL
context creation as soon as it races a plugin dlopen (so an MCP-launched
Resolve crashed within seconds of page switches), and CUDA/cuDNN users
(whisper, GPU ffmpeg) abort with "Cannot load symbol cudnnGetVersion".
Every launch site must go through proc.sanitized_spawn_env().
"""
import inspect
import os
import tempfile
import unittest
from unittest import mock

import src.granular.common as granular_common
import src.server as server
import src.core.live_connection as live_connection
import src.core.resolve_launch as resolve_launch
import src.core.app_control as app_control
import src.core.launch_shim as launch_shim
import src.core.proc as proc
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


class AlsaDeviceRankingTest(unittest.TestCase):
    """Autodetect must rank free substreams by likely-usable, not take the
    first one PipeWire left open — which picked dead HDMI pins and changed the
    pair run to run (#99). Verified via the generated conf's slave hw lines."""

    def _fake_asound(self, root, devices, eld=None):
        """devices: (card, dev, name, direction, status).
        eld: {(card, index): monitor_present_int}."""
        lines = []
        for card, dev, name, direction, status in devices:
            lines.append(f"{card:02d}-{dev:02d}: {name} : {name} : {direction} 1\n")
            sub = os.path.join(root, f"card{card}", f"pcm{dev}{direction[0]}", "sub0")
            os.makedirs(sub, exist_ok=True)
            with open(os.path.join(sub, "status"), "w", encoding="utf-8") as fh:
                fh.write(status)
        with open(os.path.join(root, "pcm"), "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        for (card, index), present in (eld or {}).items():
            path = os.path.join(root, f"card{card}", f"eld#{card}.{index}")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"monitor_present\t\t{present}\neld_valid\t\t{present}\n")

    def _conf(self, root):
        env = resolve_spawn_env({}, proc_asound=root, conf_dir=root)
        path = env.get("ALSA_CONFIG_PATH")
        self.assertTrue(path and os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_prefers_analog_over_free_hdmi(self):
        # The take-first bug would pick the HDMI pin (card 0, listed first);
        # ranking must reach the analog output instead.
        with tempfile.TemporaryDirectory() as root:
            self._fake_asound(root, [
                (0, 3, "HDMI 0", "playback", "closed"),
                (1, 0, "ALC897 Analog", "playback", "closed"),
                (1, 0, "ALC897 Analog", "capture", "closed"),
            ], eld={(0, 0): 1})
            conf = self._conf(root)
            self.assertIn("playback.pcm { type plug; slave.pcm { type hw; card 1; device 0 }", conf)

    def test_prefers_connected_hdmi_over_dead_pin(self):
        # No analog available; among HDMI pins the one with a monitor wins even
        # though a dead pin has a lower device number.
        with tempfile.TemporaryDirectory() as root:
            self._fake_asound(root, [
                (0, 3, "HDMI 0", "playback", "closed"),   # eld index 0 -> dead
                (0, 7, "HDMI 1", "playback", "closed"),   # eld index 1 -> connected
                (1, 0, "ALC897 Analog", "capture", "closed"),
            ], eld={(0, 0): 0, (0, 1): 1})
            conf = self._conf(root)
            self.assertIn("card 0; device 7", conf)

    def test_deterministic_lowest_when_equal_rank(self):
        # Two equal-tier analog outputs -> always the lower (card, device).
        with tempfile.TemporaryDirectory() as root:
            self._fake_asound(root, [
                (1, 2, "ALC897 Analog", "playback", "closed"),
                (1, 0, "ALC897 Analog", "playback", "closed"),
                (1, 0, "ALC897 Analog", "capture", "closed"),
            ])
            conf = self._conf(root)
            self.assertIn("playback.pcm { type plug; slave.pcm { type hw; card 1; device 0 }", conf)

    def test_dead_hdmi_still_used_as_last_resort(self):
        # A dead HDMI pin is worse than nothing? No — it still opens, so when
        # it is the only free playback it must be selected, not refused.
        with tempfile.TemporaryDirectory() as root:
            self._fake_asound(root, [
                (0, 3, "HDMI 0", "playback", "closed"),
                (1, 0, "ALC897 Analog", "capture", "closed"),
            ], eld={(0, 0): 0})
            conf = self._conf(root)
            self.assertIn("card 0; device 3", conf)


class LaunchSitesUseSanitizedEnvTest(unittest.TestCase):
    """Every Linux Resolve spawn passes a sanitized env and detaches the session."""

    def _assert_popen_sanitized(self, popen):
        self.assertTrue(popen.called)
        kwargs = popen.call_args.kwargs
        self.assertIn("env", kwargs)
        self.assertNotIn("libnxegl", kwargs["env"].get("LD_PRELOAD", ""))
        self.assertTrue(kwargs.get("start_new_session"))

    def _launch_patches(self):
        """Patch the shared launch module both servers now delegate to (#104)."""
        return (
            mock.patch.dict("os.environ", {"LD_PRELOAD": NXEGL}, clear=False),
            mock.patch.object(resolve_launch.subprocess, "Popen"),
            mock.patch("os.path.exists", return_value=True),
            mock.patch.object(resolve_launch.platform, "system", return_value="Linux"),
            mock.patch.object(resolve_launch.time, "sleep"),
        )

    def test_granular_launch_resolve(self):
        env_p, popen_p, exists_p, plat_p, sleep_p = self._launch_patches()
        with env_p, popen_p as popen, exists_p, plat_p, sleep_p, \
             mock.patch.object(granular_common, "_try_connect", return_value=True):
            self.assertTrue(granular_common._launch_resolve())
        self._assert_popen_sanitized(popen)

    def test_server_launch_resolve(self):
        env_p, popen_p, exists_p, plat_p, sleep_p = self._launch_patches()
        with env_p, popen_p as popen, exists_p, plat_p, sleep_p, \
             mock.patch.object(live_connection, "_try_connect", return_value=True):
            self.assertTrue(live_connection._launch_resolve())
        self._assert_popen_sanitized(popen)

    def test_both_servers_share_one_launch_implementation(self):
        """The whole point of the dedupe: neither surface may reimplement the spawn.

        If either module grows its own Popen call again, a fix applied to the
        shared path (sanitized env, start_new_session, ALSA conf) silently stops
        covering it — exactly the drift #104 finding 4 reported.
        """
        for mod in (granular_common, live_connection):
            source = inspect.getsource(mod._launch_resolve)
            self.assertIn("launch_resolve(", source, msg=f"{mod.__name__} must delegate")
            self.assertNotIn("Popen", source, msg=f"{mod.__name__} reimplements the spawn")

    def test_shared_launch_returns_false_when_app_missing(self):
        env_p, popen_p, _exists, plat_p, sleep_p = self._launch_patches()
        with env_p, popen_p as popen, plat_p, sleep_p, \
             mock.patch("os.path.exists", return_value=False):
            self.assertFalse(resolve_launch.launch_resolve(lambda: True))
        self.assertFalse(popen.called)

    def test_shared_launch_returns_false_when_resolve_never_answers(self):
        env_p, popen_p, exists_p, plat_p, sleep_p = self._launch_patches()
        with env_p, popen_p, exists_p, plat_p, sleep_p:
            self.assertFalse(
                resolve_launch.launch_resolve(lambda: None, attempts=3, interval=0)
            )

    def test_restart_resolve_app(self):
        # #110 finding 12: restart must delegate to resolve_launch.spawn_resolve,
        # not reimplement the spawn — so the sanitized app/env guarding the
        # dedicated launch path also covers the relaunch.
        with mock.patch.dict("os.environ", {"LD_PRELOAD": NXEGL}, clear=False), \
             mock.patch.object(resolve_launch.subprocess, "Popen") as popen, \
             mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control.time, "sleep"), \
             mock.patch.object(app_control, "resolve_process_running", return_value=False), \
             mock.patch.object(app_control, "quit_resolve_app", return_value=True):
            self.assertTrue(
                app_control.restart_resolve_app(resolve_obj=mock.Mock(), wait_seconds=0)
            )
        self._assert_popen_sanitized(popen)

    def test_restart_delegates_to_shared_spawn(self):
        """The dedupe point: restart must not keep its own DEFAULT_APP_PATHS or
        Popen. If it grows them back, a launch-path fix silently stops covering
        the relaunch (#110 finding 12)."""
        source = inspect.getsource(app_control.restart_resolve_app)
        self.assertIn("spawn_resolve(", source, msg="restart must delegate spawn")
        self.assertNotIn("Popen", source, msg="restart reimplements the spawn")
        self.assertNotIn("DEFAULT_APP_PATHS", source, msg="restart keeps its own path copy")


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


class RestoreAudioServerTest(unittest.TestCase):
    """The raw-hw ALSA conf takes the card exclusively; the desktop needs it back.

    While Resolve holds hw:X,Y, PipeWire's node fails to open the device and is
    parked in a terminal `error` state that it never retries out of — so the
    machine has no audio device *after* Resolve exits, until the session manager
    is restarted or the user reboots (#129).
    """

    def _on(self):
        """Clear the suite-wide kill switch conftest sets (see tests/conftest.py)."""
        env = {k: v for k, v in os.environ.items() if k != proc.AUDIO_RESTORE_OFF_ENV}
        return mock.patch.dict("os.environ", env, clear=True)

    def _linux(self):
        return mock.patch.object(proc.platform, "system", return_value="Linux")

    def test_restarts_the_first_active_session_manager(self):
        with self._on(), self._linux(), \
             mock.patch.object(proc.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(proc.restore_audio_server(units=("wireplumber", "other")))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands,
            [
                ["systemctl", "--user", "is-active", "--quiet", "wireplumber"],
                ["systemctl", "--user", "restart", "wireplumber"],
            ],
            msg="must stop at the first active manager, not bounce every unit",
        )

    def test_inactive_unit_is_skipped_for_the_next_one(self):
        def fake_run(argv, **kwargs):
            if argv[2] == "is-active" and argv[-1] == "wireplumber":
                return mock.Mock(returncode=3)
            return mock.Mock(returncode=0)

        with self._on(), self._linux(), \
             mock.patch.object(proc.subprocess, "run", side_effect=fake_run) as run:
            self.assertTrue(proc.restore_audio_server(units=("wireplumber", "fallback")))
        self.assertEqual(run.call_args_list[-1].args[0][-1], "fallback")

    def test_no_active_unit_returns_false(self):
        with self._on(), self._linux(), \
             mock.patch.object(proc.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=3)
            self.assertFalse(proc.restore_audio_server(units=("wireplumber",)))

    def test_subprocess_failure_never_raises(self):
        """Audio restore is a courtesy — it must not break a launch or a quit."""
        with self._on(), self._linux(), \
             mock.patch.object(proc.subprocess, "run", side_effect=OSError("no systemctl")):
            self.assertFalse(proc.restore_audio_server(units=("wireplumber",)))

    def test_kill_switch_blocks_everything(self):
        with self._linux(), \
             mock.patch.dict("os.environ", {proc.AUDIO_RESTORE_OFF_ENV: "1"}, clear=False), \
             mock.patch.object(proc.subprocess, "run") as run:
            self.assertFalse(proc.restore_audio_server(units=("wireplumber",)))
        self.assertFalse(run.called, msg="kill switch must short-circuit before any command")

    def test_non_linux_is_a_no_op(self):
        with self._on(), \
             mock.patch.object(proc.platform, "system", return_value="Darwin"), \
             mock.patch.object(proc.subprocess, "run") as run:
            self.assertFalse(proc.restore_audio_server())
        self.assertFalse(run.called)


class AudioReleaseWatcherTest(unittest.TestCase):
    """spawn_resolve arms the restore only when it actually took the card."""

    def _spawn(self, env):
        with mock.patch.object(resolve_launch.subprocess, "Popen") as popen, \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(resolve_launch.platform, "system", return_value="Linux"), \
             mock.patch.object(resolve_launch, "resolve_spawn_env", return_value=env), \
             mock.patch.object(resolve_launch, "_watch_for_audio_release") as watch:
            self.assertTrue(resolve_launch.spawn_resolve())
        return popen, watch

    def test_watcher_armed_when_a_raw_hw_conf_was_handed_out(self):
        popen, watch = self._spawn({"ALSA_CONFIG_PATH": "/tmp/x.conf"})
        watch.assert_called_once()
        self.assertIs(watch.call_args.args[0], popen.return_value)

    def test_no_watcher_without_a_raw_hw_conf(self):
        """No conf means Resolve shares the card via PipeWire — nothing to restore."""
        _popen, watch = self._spawn({"HOME": "/home/x"})
        self.assertFalse(watch.called)

    def test_watcher_restores_after_the_process_exits(self):
        proc_mock = mock.Mock()
        with mock.patch.object(resolve_launch, "restore_audio_server") as restore:
            thread = resolve_launch._watch_for_audio_release(proc_mock)
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        proc_mock.wait.assert_called_once()
        restore.assert_called_once()

    def test_watcher_thread_never_blocks_shutdown(self):
        proc_mock = mock.Mock()
        with mock.patch.object(resolve_launch, "restore_audio_server"):
            thread = resolve_launch._watch_for_audio_release(proc_mock)
            thread.join(timeout=5)
        self.assertTrue(thread.daemon)


class LaunchShimAudioReleaseTest(unittest.TestCase):
    """The desktop/terminal launch path needs the same release (#93/#94 + #129)."""

    def test_shim_restores_audio_instead_of_bare_exec(self):
        shim = launch_shim._SHIM_TEMPLATE
        self.assertIn("restore_audio_server", shim)
        self.assertIn("proc.wait()", shim)

    def test_shim_still_execs_when_no_conf_was_produced(self):
        """Without a raw-hw conf there is nothing to clean up, so don't linger
        as a parent process for the whole of Resolve's lifetime."""
        self.assertIn('if not env.get("ALSA_CONFIG_PATH")', launch_shim._SHIM_TEMPLATE)
        self.assertIn("os.execve", launch_shim._SHIM_TEMPLATE)

    def test_shim_template_is_valid_python(self):
        source = launch_shim._SHIM_TEMPLATE.format(
            marker=launch_shim.SHIM_MARKER, repo_root="/repo", binary="/opt/resolve/bin/resolve"
        )
        compile(source, "<shim>", "exec")


if __name__ == "__main__":
    unittest.main()
