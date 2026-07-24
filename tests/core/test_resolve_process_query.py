"""The Linux "is Resolve running?" query must not lie in either direction.

#111 finding 7 flagged `install.py`'s `pgrep -f resolve` as a false positive
(it matches /usr/lib/systemd/systemd-resolved, which runs on essentially every
modern Linux desktop) and pointed at src/core/app_control.py's `pgrep -x resolve`
as the already-fixed form to copy.

Copying it would have shipped a worse bug. `-x` matches the process NAME, and
Resolve renames its main thread: `ps -eo pid,comm` reports "GUI Thread" for
/opt/resolve/bin/resolve, so `pgrep -x resolve` matches nothing even with Resolve
running. Verified on 21.0.2.4 — resolve_process_running() returned False while
Resolve was up. That direction is the dangerous one: wait_for_resolve_exit()
would report the old process already gone, and the restart would race the still
dying instance — exactly the silent-abort failure #104 finding 3 added the query
to prevent.

Both sites now anchor a command-line match to a path boundary. These tests pin
the pattern's behaviour against the real command lines involved, so neither
regression can come back.
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.core import app_control  # noqa: E402

# POSIX ERE as handed to pgrep; Python's re has no [[:space:]], so translate
# just that class to test the same shape.
_PATTERN = app_control._LINUX_RESOLVE_CMDLINE_RE.replace("[[:space:]]", r"\s")

MATCH = [
    "/opt/resolve/bin/resolve",
    "/opt/resolve/bin/resolve --nogui",
    "resolve",
    "/usr/local/DaVinciResolve/bin/resolve",
]

NO_MATCH = [
    # The false positive the finding is about — present on nearly every desktop.
    "/usr/lib/systemd/systemd-resolved",
    "/lib/systemd/systemd-resolved --no-fork",
    "systemd-resolve",
    # Neighbouring binaries that merely contain the substring.
    "/usr/bin/resolvectl",
    "/usr/bin/resolveconf",
    "/opt/other/resolver",
]


class LinuxResolveCmdlinePattern(unittest.TestCase):
    def test_matches_real_resolve_command_lines(self):
        for cmdline in MATCH:
            with self.subTest(cmdline=cmdline):
                self.assertRegex(cmdline, _PATTERN)

    def test_does_not_match_systemd_resolved_or_neighbours(self):
        for cmdline in NO_MATCH:
            with self.subTest(cmdline=cmdline):
                self.assertIsNone(
                    re.search(_PATTERN, cmdline),
                    f"{cmdline!r} must not read as a running Resolve",
                )

    def test_linux_query_matches_the_command_line_not_the_process_name(self):
        """`-x` would match `comm`, which is "GUI Thread" for Resolve on Linux."""
        cmd = app_control._PROCESS_QUERIES["linux"]
        self.assertEqual("pgrep", cmd[0])
        self.assertIn("-f", cmd, "must match the command line; -x matches comm and never fires")
        self.assertNotIn("-x", cmd)

    def test_query_is_still_wired_into_resolve_process_running(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(app_control.platform, "system", return_value="Linux"), \
             mock.patch.object(app_control.subprocess, "run", side_effect=fake_run):
            self.assertIs(app_control.resolve_process_running(), True)

        self.assertEqual(app_control._PROCESS_QUERIES["linux"], captured["cmd"])


class InstallerUsesTheSameQuery(unittest.TestCase):
    """install.py carries its own copy of the check and must not drift back."""

    def test_installer_linux_branch_matches_app_control(self):
        source = (
            pathlib.Path(__file__).resolve().parents[2] / "install.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            app_control._LINUX_RESOLVE_CMDLINE_RE, source,
            "install.py's check_resolve_running must use the same anchored pattern as "
            "src/core/app_control.py — a bare `pgrep -f resolve` matches "
            "systemd-resolved, and `pgrep -x resolve` never matches Resolve at all",
        )

    def test_installer_process_queries_are_time_bounded(self):
        """#111 finding 8: a hung pgrep/tasklist hung the installer with no diagnostic."""
        source = (
            pathlib.Path(__file__).resolve().parents[2] / "install.py"
        ).read_text(encoding="utf-8")
        start = source.index("def check_resolve_running")
        body = source[start:source.index("\ndef ", start + 1)]
        self.assertEqual(
            body.count("subprocess.run("), body.count("timeout="),
            "every subprocess.run in check_resolve_running needs a timeout",
        )


if __name__ == "__main__":
    unittest.main()
