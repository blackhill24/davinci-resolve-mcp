"""Restart must wait for the OLD Resolve process to actually exit (#104 finding 3).

Resolve is single-instance. The old code did `quit -> sleep(wait_seconds) ->
Popen`, so a shutdown slower than the fixed sleep meant the relaunch raced the
dying process and silently aborted, leaving the user with no Resolve at all.

No Resolve required.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from src.core import app_control


class ResolveProcessRunning(unittest.TestCase):
    def _run(self, returncode: int, stdout: str = ""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr="")

    def test_pgrep_match_means_running(self) -> None:
        with mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control.subprocess, "run", return_value=self._run(0)):
            self.assertIs(app_control.resolve_process_running(), True)

    def test_pgrep_no_match_means_gone(self) -> None:
        with mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control.subprocess, "run", return_value=self._run(1)):
            self.assertIs(app_control.resolve_process_running(), False)

    def test_pgrep_error_is_unknown_not_gone(self) -> None:
        """Exit 2 is a pgrep usage/syntax error — it must not read as "gone"."""
        with mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control.subprocess, "run", return_value=self._run(2)):
            self.assertIsNone(app_control.resolve_process_running())

    def test_missing_pgrep_is_unknown(self) -> None:
        with mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(app_control.resolve_process_running())

    def test_query_timeout_is_unknown(self) -> None:
        with mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(
                 app_control.subprocess, "run",
                 side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=10)):
            self.assertIsNone(app_control.resolve_process_running())

    def test_windows_reads_tasklist_output_not_returncode(self) -> None:
        """tasklist exits 0 either way, so only its stdout distinguishes the cases."""
        with mock.patch.object(app_control.platform, "system", return_value="Windows"):
            with mock.patch.object(
                app_control.subprocess, "run",
                return_value=self._run(0, "Resolve.exe   1234 Console  1  900,000 K"),
            ):
                self.assertIs(app_control.resolve_process_running(), True)
            with mock.patch.object(
                app_control.subprocess, "run",
                return_value=self._run(0, "INFO: No tasks are running which match the criteria."),
            ):
                self.assertIs(app_control.resolve_process_running(), False)

    def test_unsupported_platform_is_unknown(self) -> None:
        with mock.patch.object(app_control.platform, "system", return_value="Haiku"):
            self.assertIsNone(app_control.resolve_process_running())


class WaitForResolveExit(unittest.TestCase):
    def test_returns_as_soon_as_process_is_gone(self) -> None:
        """Fast shutdown must not pay the full timeout."""
        with mock.patch.object(app_control, "resolve_process_running", return_value=False), \
             mock.patch.object(app_control.time, "sleep") as sleep:
            self.assertTrue(app_control.wait_for_resolve_exit(30))
        self.assertFalse(sleep.called)

    def test_polls_until_the_process_disappears(self) -> None:
        states = [True, True, False]
        with mock.patch.object(app_control, "resolve_process_running", side_effect=states), \
             mock.patch.object(app_control.time, "sleep") as sleep:
            self.assertTrue(app_control.wait_for_resolve_exit(30))
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_when_process_never_exits(self) -> None:
        with mock.patch.object(app_control, "resolve_process_running", return_value=True), \
             mock.patch.object(app_control.time, "sleep"):
            self.assertFalse(app_control.wait_for_resolve_exit(2, poll=0.5))

    def test_unknown_state_falls_back_to_a_fixed_wait(self) -> None:
        with mock.patch.object(app_control, "resolve_process_running", return_value=None), \
             mock.patch.object(app_control.time, "sleep") as sleep:
            self.assertTrue(app_control.wait_for_resolve_exit(7))
        sleep.assert_called_once_with(7)


class RestartWaitsForExit(unittest.TestCase):
    def test_refuses_to_relaunch_while_the_old_process_lives(self) -> None:
        with mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control, "quit_resolve_app", return_value=True), \
             mock.patch.object(app_control, "resolve_process_running", return_value=True), \
             mock.patch.object(app_control.time, "sleep"), \
             mock.patch.object(app_control.subprocess, "Popen") as popen:
            self.assertFalse(
                app_control.restart_resolve_app(resolve_obj=mock.Mock(), wait_seconds=2)
            )
        self.assertFalse(popen.called, msg="must not spawn a second instance")

    def test_relaunches_once_the_old_process_is_gone(self) -> None:
        with mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control, "quit_resolve_app", return_value=True), \
             mock.patch.object(app_control, "resolve_process_running", side_effect=[True, False]), \
             mock.patch.object(app_control.time, "sleep"), \
             mock.patch.object(app_control.subprocess, "Popen") as popen:
            self.assertTrue(
                app_control.restart_resolve_app(resolve_obj=mock.Mock(), wait_seconds=30)
            )
        self.assertTrue(popen.called)


if __name__ == "__main__":
    unittest.main()
