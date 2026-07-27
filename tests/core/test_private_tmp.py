"""#143 finding 3: state files must not live at a predictable bare-/tmp path.

The page lock is truncate()d and the transport state file holds the networked
bearer token (plus a pid the dashboard SIGTERMs), so a name another local user
can pre-create is a file-clobbering and secret-disclosure primitive. These tests
pin the three properties that close it: a per-uid 0700 directory, mode 0600 on
the files, and a refusal to follow a symlink at the final open.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

from src.core import private_tmp


@unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only permission model")
class PrivateDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(tempfile, "gettempdir", return_value=self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def test_directory_is_per_uid_and_mode_0700(self) -> None:
        path = private_tmp.private_dir()
        self.assertIsNotNone(path)
        self.assertIn(str(os.getuid()), os.path.basename(path))
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)
        # Idempotent: a second call reuses it rather than failing on exist.
        self.assertEqual(private_tmp.private_dir(), path)

    def test_refuses_a_path_that_is_not_a_directory(self) -> None:
        squat = os.path.join(self.tmp.name, f"drm-resolve-{os.getuid()}")
        with open(squat, "w", encoding="utf-8") as handle:
            handle.write("planted")
        self.assertIsNone(private_tmp.private_dir())

    def test_refuses_a_directory_owned_by_someone_else(self) -> None:
        real_lstat = os.lstat

        def fake_lstat(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if os.path.basename(str(path)).startswith("drm-resolve"):
                # Same struct, foreign owner.
                return os.stat_result(tuple(st)[:4] + (st.st_uid + 1,) + tuple(st)[5:])
            return st

        with mock.patch.object(os, "lstat", side_effect=fake_lstat):
            self.assertIsNone(private_tmp.private_dir())

    def test_private_path_is_inside_the_private_dir(self) -> None:
        path = private_tmp.private_path("thing.json")
        self.assertEqual(os.path.dirname(path), private_tmp.private_dir())


@unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only permission model")
class WritePrivateTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_writes_mode_0600(self) -> None:
        path = os.path.join(self.tmp.name, "secret.json")
        self.assertTrue(private_tmp.write_private_text(path, '{"token": "s3cret"}'))
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"token": "s3cret"}')

    def test_refuses_to_follow_a_planted_symlink(self) -> None:
        victim = os.path.join(self.tmp.name, "victim.conf")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("important")
        planted = os.path.join(self.tmp.name, "state.json")
        os.symlink(victim, planted)

        self.assertFalse(private_tmp.write_private_text(planted, "clobbered"))
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "important", "victim must be untouched")

    def test_truncates_an_existing_file_of_our_own(self) -> None:
        path = os.path.join(self.tmp.name, "state.json")
        self.assertTrue(private_tmp.write_private_text(path, "a-long-previous-value"))
        self.assertTrue(private_tmp.write_private_text(path, "new"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "new")


@unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only permission model")
class OpenPrivateHandleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_does_not_truncate_on_open(self) -> None:
        # The page lock stamps a holder PID that must survive another process
        # opening the same lock file.
        path = os.path.join(self.tmp.name, "page.lock")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("12345")
        fh = private_tmp.open_private_handle(path)
        self.addCleanup(fh.close)
        fh.seek(0)
        self.assertEqual(fh.read(), "12345")

    def test_refuses_a_planted_symlink(self) -> None:
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("important")
        planted = os.path.join(self.tmp.name, "page.lock")
        os.symlink(victim, planted)
        self.assertIsNone(private_tmp.open_private_handle(planted))
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "important")

    def test_rejects_an_unsupported_mode(self) -> None:
        with self.assertRaises(ValueError):
            private_tmp.open_private_handle(
                os.path.join(self.tmp.name, "x"), mode="w"
            )


if __name__ == "__main__":
    unittest.main()
