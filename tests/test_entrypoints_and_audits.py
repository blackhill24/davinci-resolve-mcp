"""Smoke coverage for entry-point shims and the repo's own audit scripts.

- src/resolve_mcp_server.py and src/control_panel.py must import cleanly
  (their __main__ guards keep servers from starting under import).
- scripts/audit_api_parity.py and scripts/audit_readwrite_symmetry.py must
  exit 0 — they are the standing guards against API fabrication and
  read/write asymmetry, so a red audit should fail CI, not just a human run.
"""
import importlib
import pathlib
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


if __name__ == "__main__":
    unittest.main()
