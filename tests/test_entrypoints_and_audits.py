"""Smoke coverage for entry-point shims and the repo's own audit scripts.

- src/resolve_mcp_server.py and src/control_panel.py must import cleanly
  (their __main__ guards keep servers from starting under import).
- scripts/audit_api_parity.py and scripts/audit_readwrite_symmetry.py must
  exit 0 — they are the standing guards against API fabrication and
  read/write asymmetry, so a red audit should fail CI, not just a human run.
"""
import importlib
import pathlib
import re
import subprocess
import sys
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


class EntrypointImportTest(unittest.TestCase):
    def test_granular_entry_imports(self):
        mod = importlib.import_module("src.resolve_mcp_server")
        self.assertTrue(hasattr(mod, "mcp"))
        self.assertTrue(hasattr(mod, "VERSION"))

    def test_control_panel_imports(self):
        mod = importlib.import_module("src.control_panel")
        self.assertTrue(callable(mod.main))


class AuditScriptsTest(unittest.TestCase):
    def _run(self, script):
        return subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=PROJECT_ROOT, timeout=300)

    def test_api_parity_audit_passes(self):
        proc = self._run("audit_api_parity.py")
        self.assertEqual(proc.returncode, 0,
                         f"audit_api_parity failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-500:]}")

    def test_readwrite_symmetry_audit_passes(self):
        proc = self._run("audit_readwrite_symmetry.py")
        self.assertEqual(proc.returncode, 0,
                         f"audit_readwrite_symmetry failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-500:]}")
        # #110 finding 13: the audit used to scan only src/server.py, whose
        # _unknown lists lost the compound surface in the #52 restructure —
        # "4 actions scanned" while thinking it covered everything. Assert it
        # actually reaches the domain surface so the re-scoping can't silently
        # regress.
        m = re.search(r"write-style actions scanned:\s*\*\*(\d+)\*\*", proc.stdout)
        self.assertIsNotNone(m, f"could not parse scan count:\n{proc.stdout[-2000:]}")
        self.assertGreaterEqual(int(m.group(1)), 50,
                                f"symmetry audit scanned too few actions — surface not reached:\n{proc.stdout[-2000:]}")

    def test_readwrite_symmetry_audit_fails_on_new_gap(self):
        # The audit must return nonzero when a new set_-without-get_ gap appears,
        # otherwise "returncode == 0" proves nothing (#110 finding 13).
        import scripts.audit_readwrite_symmetry as audit_mod
        original = audit_mod.BASELINE_HIGH_SIGNAL_GAPS
        try:
            audit_mod.BASELINE_HIGH_SIGNAL_GAPS = frozenset()
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = audit_mod.main()
            self.assertEqual(rc, 1, "audit did not fail with the baseline emptied")
            self.assertIn("AUDIT FAILED", buf.getvalue())
        finally:
            audit_mod.BASELINE_HIGH_SIGNAL_GAPS = original


class ApiParityAuditIsNotVacuousTest(unittest.TestCase):
    """`audit_api_parity.py` exits 0 — prove that means something (#121 task 2).

    `test_api_parity_audit_passes` only asserts the exit code. An audit whose
    doc parser stopped matching, or whose source scan started returning nothing,
    would report "PASS — all checks clean" with zero methods compared. The
    readwrite audit already has this treatment (see
    `test_readwrite_symmetry_audit_fails_on_new_gap`); this is its missing half.
    """

    def setUp(self):
        import scripts.audit_api_parity as audit_mod

        self.audit = audit_mod
        self.docs = audit_mod.parse_documented_methods(audit_mod.DOCS_PATH)

    def test_the_documented_surface_is_actually_parsed(self):
        total = sum(len(v) for v in self.docs.values())
        self.assertGreater(len(self.docs), 5, "API doc parser found almost no classes")
        self.assertGreater(total, 200, f"API doc parser found only {total} methods")

    def test_the_missing_method_detector_fires_on_an_empty_source_scan(self):
        missing = self.audit.find_methods_missing_from_source(self.docs, "")
        self.assertTrue(
            missing,
            "every documented method is 'present' in an EMPTY source text — the "
            "detector matches nothing, so its clean result is meaningless",
        )

    def test_the_audit_reports_failure_when_the_source_scan_comes_back_empty(self):
        # End-to-end: main() must return 1, not 0, when nothing implements the API.
        import contextlib
        import io
        import unittest.mock as mock

        buf = io.StringIO()
        with mock.patch.object(self.audit, "collect_source_text", return_value=""):
            with contextlib.redirect_stdout(buf):
                rc = self.audit.main()
        self.assertEqual(rc, 1, f"audit passed with an empty source scan:\n{buf.getvalue()[-2000:]}")
        self.assertIn("MISSING", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
