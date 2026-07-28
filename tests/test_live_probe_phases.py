"""Guards on the two-phase GUI probes' sweep behaviour (issue #154).

The leak these probes had was invisible to the offline suite and invisible to
the sweep's own verdict: they returned PASS while leaving a project behind every
run, because `main()` defaulted to the interactive `setup` phase and the sweep
invokes every harness with no arguments. Nothing failed; the evidence was a
growing project list nobody read.

So the regression guard cannot be "the cleanup function works" — it has to be
"the no-argument invocation is not `setup`". That is the property that broke.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.probe_phases import delete_probe_project, run_sweep  # noqa: E402


class RunSweepTest(unittest.TestCase):
    def test_cleanup_runs_after_a_successful_setup(self):
        calls = []
        code = run_sweep(lambda: calls.append("setup") or 0,
                         lambda: calls.append("cleanup"))
        self.assertEqual(calls, ["setup", "cleanup"])
        self.assertEqual(code, 0)

    def test_cleanup_runs_after_a_failing_setup(self):
        """The half that made #154 worse: a setup that failed after creating the
        project used to leak where a passing one would not."""
        calls = []
        code = run_sweep(lambda: calls.append("setup") or 1,
                         lambda: calls.append("cleanup"))
        self.assertEqual(calls, ["setup", "cleanup"])
        self.assertEqual(code, 1)

    def test_cleanup_runs_when_setup_raises(self):
        calls = []
        with self.assertRaises(RuntimeError):
            run_sweep(self._raiser, lambda: calls.append("cleanup"))
        self.assertEqual(calls, ["cleanup"])

    @staticmethod
    def _raiser():
        raise RuntimeError("setup blew up")

    def test_a_failing_cleanup_does_not_mask_setups_verdict(self):
        code = run_sweep(lambda: 0, self._raiser)
        self.assertEqual(code, 0)


class _FakeProjectManager:
    def __init__(self, projects):
        self.projects = list(projects)
        self.deleted = []

    def GetProjectListInCurrentFolder(self):
        return list(self.projects)

    def GetCurrentProject(self):
        return None

    def DeleteProject(self, name):
        if name not in self.projects:
            return False
        self.projects.remove(name)
        self.deleted.append(name)
        return True


class _FakeResolve:
    def __init__(self, projects):
        self.pm = _FakeProjectManager(projects)
        self.pages = []

    def GetProjectManager(self):
        return self.pm

    def GetCurrentPage(self):
        return "fusion"

    def OpenPage(self, page):
        self.pages.append(page)
        return True


class DeleteProbeProjectTest(unittest.TestCase):
    def test_it_deletes_and_parks_off_the_fusion_page(self):
        """`resolve=` is what makes the helper park off Fusion, where a delete
        terminates Resolve outright (#153/#157). A probe that forgot to pass it
        would still pass a delete-happened assertion, so assert the park."""
        resolve = _FakeResolve(["pan_probe_120000", "ZZ_live_suite_scratch"])
        self.assertTrue(delete_probe_project(resolve, "pan_probe_120000"))
        self.assertEqual(resolve.pm.deleted, ["pan_probe_120000"])
        self.assertTrue(resolve.pages, "the UI was never parked off the Fusion page")

    def test_an_absent_project_is_not_an_error(self):
        resolve = _FakeResolve(["ZZ_live_suite_scratch"])
        self.assertFalse(delete_probe_project(resolve, "pan_probe_120000"))
        self.assertEqual(resolve.pm.deleted, [])

    def test_no_name_and_no_resolve_are_both_no_ops(self):
        self.assertFalse(delete_probe_project(_FakeResolve([]), None))
        self.assertFalse(delete_probe_project(None, "pan_probe_120000"))


def _default_phase(source: str):
    """The phase a harness runs when invoked with no arguments, or None.

    Reads the `sys.argv[1] if len(sys.argv) > 1 else "<phase>"` idiom every
    multi-phase probe uses — the orelse branch *is* the sweep's behaviour.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.IfExp):
            continue
        if isinstance(node.orelse, ast.Constant) and isinstance(node.orelse.value, str):
            body = node.body
            if isinstance(body, ast.Subscript) and "argv" in ast.dump(body):
                return node.orelse.value
    return None


class NoHarnessDefaultsToSetupTest(unittest.TestCase):
    """The #154 regression guard, checked against the real harness tree."""

    def test_no_live_harness_defaults_to_an_interactive_setup_phase(self):
        offenders = []
        for path in sorted(REPO_ROOT.joinpath("tests").rglob("live_*.py")):
            phase = _default_phase(path.read_text(encoding="utf-8"))
            if phase == "setup":
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [], "these leak a project on every sweep: "
                                        "no-argument invocation runs the interactive "
                                        "setup phase and never reaches cleanup (#154)")

    def test_the_five_from_the_issue_default_to_sweep(self):
        for rel in ("tests/domains/audio_fairlight/live_audio_fx_probe.py",
                    "tests/domains/audio_fairlight/live_channel_format_probe.py",
                    "tests/domains/audio_fairlight/live_pan_probe.py",
                    "tests/domains/audio_fairlight/live_subtitle_probe.py",
                    "tests/domains/timeline_conform_interchange/live_multicam_drt_probe.py"):
            with self.subTest(harness=rel):
                source = REPO_ROOT.joinpath(rel).read_text(encoding="utf-8")
                self.assertEqual(_default_phase(source), "sweep")

    def test_the_guard_can_actually_see_the_old_shape(self):
        """Without this, a broken `_default_phase` would report an empty
        offender list and the guard above would pass on any tree at all."""
        self.assertEqual(
            _default_phase('phase = sys.argv[1] if len(sys.argv) > 1 else "setup"\n'),
            "setup")


if __name__ == "__main__":
    unittest.main()
