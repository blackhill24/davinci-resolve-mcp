"""The env-leak guard in ``tests/conftest.py`` is itself checked (#121 task 4).

An autouse fixture that silently stopped detecting leaks would read as safety
while providing none — the same failure mode #110 found when two drift guards
had `ImportError`'d out of every CI run and nobody noticed. So this file feeds
the guard a test that leaks on purpose, in a throwaway pytest run, and asserts
the run fails.

The child run is a subprocess rather than an in-process ``pytest.main`` because
the leak has to happen under a *fresh* conftest instance; running it in-process
would leak into this process's own guard as well.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_LEAKING_TEST = """
import os


def test_leaks_on_purpose():
    os.environ["DRM_ENV_LEAK_GUARD_PROBE"] = "1"
"""

_CLEAN_TEST = """
import os
from unittest import mock


def test_does_not_leak():
    with mock.patch.dict(os.environ, {"DRM_ENV_LEAK_GUARD_PROBE": "1"}):
        assert os.environ["DRM_ENV_LEAK_GUARD_PROBE"] == "1"
"""


class EnvLeakGuardIsNotVacuous(unittest.TestCase):
    def _run(self, body: str) -> subprocess.CompletedProcess:
        # Written into tests/ so the real tests/conftest.py applies to it.
        with tempfile.NamedTemporaryFile(
            "w",
            suffix=".py",
            prefix="test_generated_env_probe_",
            dir=str(_REPO_ROOT / "tests"),
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(textwrap.dedent(body))
            path = handle.name
        try:
            return subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", path],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=300,
            )
        finally:
            os.unlink(path)

    def test_guard_fails_a_test_that_leaks(self):
        result = self._run(_LEAKING_TEST)
        self.assertNotEqual(
            result.returncode,
            0,
            "tests/conftest.py::_no_env_leak did not fail a test that wrote "
            f"os.environ directly.\nstdout:\n{result.stdout}",
        )
        self.assertIn("leaked os.environ changes", result.stdout + result.stderr)

    def test_guard_passes_a_test_that_uses_patch_dict(self):
        # The other half: a guard that fails everything is equally useless.
        result = self._run(_CLEAN_TEST)
        self.assertEqual(
            result.returncode,
            0,
            "tests/conftest.py::_no_env_leak flagged a mock.patch.dict user as a "
            f"leak.\nstdout:\n{result.stdout}",
        )


if __name__ == "__main__":
    unittest.main()
