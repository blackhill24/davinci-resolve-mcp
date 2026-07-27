"""Regression guard: text IO under `src/` must never take its encoding from the locale.

`open()`, `pathlib.read_text()/write_text()` and `subprocess(text=True)` all resolve
their default encoding from the process's C locale, at call time. That locale is not
ours to trust: `fusionscript.scriptapp("Resolve")` used to reset it to POSIX (#121,
fixed at the trigger by `src/core/locale_guard.restore()`), and a systemd unit with no
`LANG`, a cron job, or a minimal container starts in it anyway. Under that locale every
unencoded text-mode call decodes as ASCII and raises `UnicodeDecodeError` on the first
byte over 0x7F — an accented clip path, a non-7-bit subtitle, a project name typed in a
language other than English.

`test_locale_guard.py` guards the trigger we know about. This one removes the
dependency: it fails when a *new* encoding-less text-mode site appears under `src/`
(#124). The two are complementary — neither replaces the other.

Adding a site that genuinely wants the locale's encoding? Put it in `_ALLOWLIST` with a
reason. An empty allowlist is the intended steady state.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

# rel:lineno -> reason. Deliberately empty: every site under src/ names its encoding.
_ALLOWLIST: dict[str, str] = {}

# Names whose text-mode call resolves the encoding from the locale. `subprocess.run`,
# `Popen`, `check_output` and every local wrapper (`safe_run`) are covered by keying on
# the `text=True` / `universal_newlines=True` keyword rather than on the callee's name —
# a wrapper renamed tomorrow is still caught.
_PATHLIB_TEXT_METHODS = {"read_text", "write_text"}


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _scan_source(source: str, rel: str):
    """[(rel, lineno, kind)] for encoding-less text-mode IO in `source`.

    `kind` is one of "subprocess", "open", "pathlib". Exposed separately from the
    filesystem walk so the drift test below can feed the scanner a known-bad file.
    """
    found = []
    tree = ast.parse(source, filename=rel)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        has_encoding = "encoding" in kwargs
        func = node.func

        # subprocess(..., text=True) / universal_newlines=True, via any callee.
        if _is_true(kwargs.get("text")) or _is_true(kwargs.get("universal_newlines")):
            if not has_encoding:
                found.append((rel, node.lineno, "subprocess"))

        # Bare `open(...)`. `os.open`/`Image.open` are attribute calls and not text IO.
        if isinstance(func, ast.Name) and func.id == "open":
            mode = node.args[1] if len(node.args) > 1 else kwargs.get("mode")
            literal_binary = (
                isinstance(mode, ast.Constant)
                and isinstance(mode.value, str)
                and "b" in mode.value
            )
            # A non-literal mode can't be proven binary — flag it rather than assume.
            if not literal_binary and not has_encoding:
                found.append((rel, node.lineno, "open"))

        if isinstance(func, ast.Attribute) and func.attr in _PATHLIB_TEXT_METHODS:
            if not has_encoding:
                found.append((rel, node.lineno, "pathlib"))

    return found


def _scan_src():
    """(offending sites, total .py files read) across `src/`."""
    sites, files = [], 0
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        files += 1
        sites.extend(_scan_source(path.read_text(encoding="utf-8"), rel))
    return sites, files


class ScannerIsNotBlindTest(unittest.TestCase):
    """A guard whose scan silently matches nothing reads as safety while providing none."""

    def test_the_scan_actually_reads_src(self):
        _, files = _scan_src()
        self.assertGreater(files, 50, f"scanner read only {files} files under src/ — scan broken?")

    def test_the_scanner_flags_an_encodingless_subprocess(self):
        bad = "import subprocess\nsubprocess.run(['ls'], capture_output=True, text=True)\n"
        self.assertEqual(
            [("drift.py", 2, "subprocess")], _scan_source(bad, "drift.py")
        )

    def test_the_scanner_flags_universal_newlines(self):
        bad = "import subprocess\nsubprocess.run(['ls'], universal_newlines=True)\n"
        self.assertEqual([("drift.py", 2, "subprocess")], _scan_source(bad, "drift.py"))

    def test_the_scanner_flags_an_encodingless_open(self):
        self.assertEqual([("drift.py", 1, "open")], _scan_source("open('x')\n", "drift.py"))
        self.assertEqual([("drift.py", 1, "open")], _scan_source("open('x', 'w')\n", "drift.py"))

    def test_the_scanner_flags_encodingless_pathlib_text_io(self):
        self.assertEqual(
            [("drift.py", 1, "pathlib")], _scan_source("p.read_text()\n", "drift.py")
        )
        self.assertEqual(
            [("drift.py", 1, "pathlib")], _scan_source("p.write_text(s)\n", "drift.py")
        )

    def test_the_scanner_does_not_flag_what_is_already_safe(self):
        safe = (
            "subprocess.run(['ls'], text=True, encoding='utf-8')\n"
            "open('x', 'rb')\n"
            "open('x', encoding='utf-8')\n"
            "p.read_text(encoding='utf-8')\n"
            # `text=` is a common ordinary kwarg — flagging it would make the guard
            # unusable and its allowlist meaningless.
            "embeddings.find_similar(root, text=query)\n"
            "os.open(path, os.O_RDONLY)\n"
        )
        self.assertEqual([], _scan_source(safe, "safe.py"))


class NoLocaleDependentTextIOTest(unittest.TestCase):
    def test_every_text_mode_site_under_src_names_its_encoding(self):
        sites, _ = _scan_src()
        offenders = [
            f"{rel}:{lineno} ({kind})"
            for rel, lineno, kind in sites
            if f"{rel}:{lineno}" not in _ALLOWLIST
        ]
        self.assertEqual(
            [], offenders,
            "These sites take their text encoding from the process locale, which is "
            "ASCII under a C locale (a service/cron start, or a native library that "
            "resets it). Pass encoding=\"utf-8\" — plus errors=\"replace\" when the "
            "output is only logged or pattern-matched, never parsed. If a site really "
            f"wants the locale's encoding, add it to _ALLOWLIST with a reason: {offenders}",
        )

    def test_the_allowlist_has_no_stale_entries(self):
        sites, _ = _scan_src()
        live = {f"{rel}:{lineno}" for rel, lineno, _ in sites}
        stale = sorted(set(_ALLOWLIST) - live)
        self.assertEqual(
            [], stale,
            "Allowlisted sites that no longer scan as offenders — line numbers drift, so "
            f"a stale entry silently exempts whatever moved onto that line: {stale}",
        )


if __name__ == "__main__":
    unittest.main()
