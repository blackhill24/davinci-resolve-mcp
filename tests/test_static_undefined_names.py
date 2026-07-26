"""Static guard: no undefined names anywhere under src/.

An undefined name inside a guard or try/except doesn't crash — it silently
falls back to a default. Three shipped bugs were this class: the
confirm-token gate calling a misspelled preference reader (v2.37.0), the
update-channel resource reporting "stable" unconditionally, and the
auto-run idle-timeout preference being ignored (both fixed after a
pyflakes audit). This test keeps the class extinct.

Skips when pyflakes is not installed (it is a dev dependency, not a
runtime one): `pip install pyflakes`.
"""
from __future__ import annotations

import io
import pathlib
import tempfile
import textwrap
import unittest

try:
    from pyflakes.api import checkPath
    from pyflakes.reporter import Reporter
    HAVE_PYFLAKES = True
except ImportError:
    HAVE_PYFLAKES = False

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def _undefined_names_in(paths):
    out = io.StringIO()
    reporter = Reporter(out, out)
    for path in paths:
        checkPath(str(path), reporter)
    return [
        line
        for line in out.getvalue().splitlines()
        if "undefined name" in line and "unable to detect undefined names" not in line
    ]


@unittest.skipUnless(HAVE_PYFLAKES, "pyflakes not installed")
class UndefinedNamesTest(unittest.TestCase):
    def test_the_scan_actually_reaches_src(self):
        # Without this, a moved/renamed src/ turns the check below into "no files
        # scanned, therefore no undefined names" — a green vacuum (#121 task 2).
        scanned = sorted(SRC.rglob("*.py"))
        self.assertGreater(len(scanned), 100, f"only {len(scanned)} files under {SRC}")

    def test_no_undefined_names_in_src(self):
        out = io.StringIO()
        reporter = Reporter(out, out)
        for path in sorted(SRC.rglob("*.py")):
            checkPath(str(path), reporter)
        undefined = [
            line
            for line in out.getvalue().splitlines()
            if "undefined name" in line and "unable to detect undefined names" not in line
        ]
        self.assertEqual(undefined, [], "undefined names found:\n" + "\n".join(undefined))


@unittest.skipUnless(HAVE_PYFLAKES, "pyflakes not installed")
class UndefinedNameDetectionIsNotVacuousTest(unittest.TestCase):
    """Feed the detector the exact shape it exists to catch (#121 task 2).

    The three shipped bugs in this file's docstring all had the same form: a
    misspelled name inside a `try`/`except` that fell back to a default instead
    of crashing. If pyflakes ever stopped reporting that — a version bump, a
    changed message string, a reporter wired to the wrong stream — the guard
    would go green and stay green.
    """

    def test_a_misspelled_name_inside_a_guard_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "synthetic_undefined.py"
            path.write_text(
                textwrap.dedent(
                    """
                    def read_preference():
                        try:
                            return _reed_confirm_token_preference()
                        except Exception:
                            return None
                    """
                ),
                encoding="utf-8",
            )
            found = _undefined_names_in([path])
        self.assertTrue(found, "pyflakes no longer reports undefined names")
        self.assertIn("_reed_confirm_token_preference", "\n".join(found))

    def test_a_clean_file_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "synthetic_clean.py"
            path.write_text("def f():\n    return 1\n", encoding="utf-8")
            self.assertEqual(_undefined_names_in([path]), [])


if __name__ == "__main__":
    unittest.main()
