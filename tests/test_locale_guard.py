"""Regression guard: Resolve's `scriptapp()` must not leave the process in C locale.

`fusionscript.scriptapp("Resolve")` resets the C locale to POSIX/C. Python then
resolves the default encoding for `open()`, `pathlib.read_text()/write_text()`
and `subprocess(text=True)` from that locale, at call time — so after the first
connection every one of those calls that did not name an encoding decodes as
ASCII and raises on the first non-ASCII byte (an accented clip path, a subtitle
file, a non-English project name).

A *mid-process* reset is what makes this reachable. CPython decides UTF-8 mode
once, from the locale the interpreter started in: start in C/POSIX and UTF-8 mode
comes on, so `getpreferredencoding()` reports utf-8 no matter what the locale is
set to later. Start in a UTF-8 locale — as every real launch of this server does —
and UTF-8 mode stays off, so the encoding follows the live locale and `scriptapp()`
resetting it mid-flight really does switch the process to ASCII (#127).

That also means (1) below has an unreachable precondition when the interpreter
itself started in C, and skips there with that reason stated.

The offline suite never connects to Resolve, so it can never observe the reset
directly. Two things are checkable offline and both are checked here:

  1. `locale_guard.restore()` actually restores a deliberately-broken locale.
  2. Every `scriptapp()` call site in `src/` is followed by a restore call.

(2) is the one that matters for regressions: a new connection path that forgets
the restore reintroduces the bug in exactly the place the offline suite is blind.
"""
from __future__ import annotations

import ast
import locale
import pathlib
import sys
import unittest

from src.core import locale_guard

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

# Sites that name scriptapp inside a string are generating a *script for Resolve
# to run*, not connecting from this process.
_GENERATED_SCRIPT_FILES = {
    "src/domains/extension_authoring/utils/script_templates.py",
    "src/domains/extension_authoring/actions.py",
}


def _scriptapp_call_sites():
    """[(relpath, lineno, enclosing statement list)] for real scriptapp() calls."""
    sites = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        if rel in _GENERATED_SCRIPT_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "scriptapp":
                continue
            sites.append((rel, node.lineno, path))
    return sites


def _restore_calls(path):
    """Line numbers of `<something>.restore()` calls in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "restore"
    ]


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self._original = locale.setlocale(locale.LC_ALL)
        self.addCleanup(locale.setlocale, locale.LC_ALL, self._original)

    def test_restore_recovers_from_the_c_locale(self):
        # This is what scriptapp() does to the process, reproduced directly.
        healthy = locale.getpreferredencoding(False)
        locale.setlocale(locale.LC_ALL, "C")
        broken = locale.getpreferredencoding(False)
        if broken == healthy:
            # CPython decides UTF-8 mode once, from the *startup* locale: start in
            # C/POSIX and getpreferredencoding() stays utf-8 however the live locale
            # is reset afterwards. The precondition is then unreachable — there is no
            # broken encoding for restore() to recover — so assert why and skip (#127).
            self.assertTrue(
                sys.flags.utf8_mode,
                "the C locale stopped yielding ASCII, and UTF-8 mode is off — "
                "the precondition broke for some other reason",
            )
            raise unittest.SkipTest(
                "UTF-8 mode is on (the interpreter started in a C/POSIX locale), so a "
                "mid-process C-locale reset cannot change the encoding"
            )
        self.assertEqual(broken, "ANSI_X3.4-1968", "the C locale no longer yields ASCII here")
        self.assertEqual(locale_guard.restore(), healthy)

    def test_restore_is_idempotent(self):
        first = locale_guard.restore()
        self.assertEqual(first, locale_guard.restore())

    def test_restore_never_raises(self):
        # An environment with an unusable LANG must not take the connection down.
        import unittest.mock as mock

        with mock.patch.object(locale, "setlocale", side_effect=locale.Error("boom")):
            self.assertIsInstance(locale_guard.restore(), str)


class EveryConnectSiteRestoresTest(unittest.TestCase):
    def test_the_scan_finds_the_connect_sites(self):
        sites = _scriptapp_call_sites()
        self.assertGreaterEqual(
            len(sites), 4, f"scriptapp() scanner found only {len(sites)} sites — scan broken?"
        )

    def test_every_scriptapp_call_is_followed_by_a_locale_restore(self):
        missing = []
        for rel, lineno, path in _scriptapp_call_sites():
            restores = _restore_calls(path)
            # The restore must be the next few lines, not merely somewhere in the
            # file — a file with two connect paths needs two restores.
            if not any(lineno < r <= lineno + 3 for r in restores):
                missing.append(f"{rel}:{lineno}")
        self.assertEqual(
            [], missing,
            "scriptapp() resets the process's C locale, so every text-mode open() / "
            "read_text() / subprocess(text=True) that did not name an encoding starts "
            "decoding as ASCII. Call src.core.locale_guard.restore() right after "
            f"connecting: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
