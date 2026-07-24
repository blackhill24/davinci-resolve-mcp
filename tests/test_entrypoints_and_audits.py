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


if __name__ == "__main__":
    unittest.main()
