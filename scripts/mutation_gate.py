#!/usr/bin/env python3
"""Mutation gate for the Resolve bridge helpers (#119 task 12).

The premise of #119 is that a green offline suite proved nothing about the code that
kept regressing: breaking `_api_constant` or `_has_method` outright left
`2153 passed, 0 failed`. Coverage percentages could not see that. Mutation testing
can — so it is measured here on every publish instead of being rediscovered by the
live suite (or by a user) months later.

Each mutation below re-introduces a defect that has ACTUALLY SHIPPED, or the exact
inversion the current implementation exists to prevent. Applying it must make the
offline suite fail, by at least `min_failures` tests. A mutation that survives means
the suite has gone blind to that defect again.

Usage
-----

    .venv/bin/python scripts/mutation_gate.py            # all mutations
    .venv/bin/python scripts/mutation_gate.py --list
    .venv/bin/python scripts/mutation_gate.py --only api_constant_dir_based

Exit codes: 0 all mutations killed, 1 at least one survived (or was too weakly
killed), 2 the harness itself could not run.

Thresholds are floors, not targets. Raise one when new coverage lands; never lower
one to make this pass — a falling kill count is the signal this script exists to
emit. The baselines in the comments are the numbers #119 measured before its fix.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Each entry: the body of a pytest plugin's pytest_configure, applied before
# collection. `min_failures` is the floor measured after #119's fix.
MUTATIONS = {
    "api_constant_dead": {
        "why": "_api_constant always returns None — every export falls back to "
               "passing the literal constant NAME to the API (the cc007ef symptom).",
        "baseline": "0 failed before #119 when applied to granular/common only",
        "min_failures": 5,
        "patch": """
    import src.core.envelope as env
    env._api_constant = lambda obj, name: None
""",
    },
    "api_constant_dir_based": {
        "why": "The cc007ef bug itself: resolve constants via `name in dir(obj)`. "
               "dir() lists methods only, so every EXPORT_* lookup fails and the "
               "literal name is passed to Timeline.Export()/ExportLUT().",
        "baseline": "6 failed, all in tests/core/test_api_constant_resolution.py",
        "min_failures": 10,
        "patch": """
    import src.core.envelope as env

    def _dir_based(obj, name):
        if obj is None or not name:
            return None
        return getattr(obj, name) if name in dir(obj) else None

    env._api_constant = _dir_based
""",
    },
    "api_constant_granular_copy": {
        "why": "Break ONLY the src/granular/common binding. That copy is what the "
               "export path binds; #118's regression test imports the other one, so "
               "this used to be completely invisible.",
        "baseline": "0 failed (2153 passed)",
        "min_failures": 3,
        "patch": """
    import src.granular.common as common
    common._api_constant = lambda obj, name: None
    common._has_method = lambda obj, name: True
""",
    },
    "has_method_always_true": {
        "why": "The fabrication bug _has_method exists to defend against: the bridge "
               "makes hasattr() True for any name, so a gate stuck open calls methods "
               "that do not exist and silently gets None back.",
        "baseline": "10 failed, in 10 files",
        "min_failures": 30,
        "patch": """
    import src.core.envelope as env
    import src.granular.common as common
    env._has_method = common._has_method = lambda obj, name: True
""",
    },
    "has_method_always_false": {
        "why": "The inverse: every capability reported missing, so working features "
               "are refused and capability reports tell the agent nothing works.",
        "baseline": "32 failed",
        "min_failures": 45,
        "patch": """
    import src.core.envelope as env
    import src.granular.common as common
    env._has_method = common._has_method = lambda obj, name: False
""",
    },
    "api_constant_truthiness": {
        "why": "Reject falsy constants — EXPORT_NONE is 0.0, so an `if value:` guard "
               "silently drops a valid subtype and passes the name instead.",
        "baseline": "not previously measured",
        "min_failures": 2,
        "patch": """
    import src.core.envelope as env
    _real = env._api_constant

    def _truthy_only(obj, name):
        value = _real(obj, name)
        return value if value else None

    env._api_constant = _truthy_only
""",
    },
    # ── Guards going blind (#121 task 2) ─────────────────────────────────────
    #
    # The mutations above break PRODUCTION code and expect tests to notice. These
    # break the STATIC GUARDS themselves — the ten drift/audit meta-tests — and
    # expect the guards' own scan-count assertions to notice.
    #
    # That is the failure mode #110 actually hit: two guards moved folders in the
    # #52 restructure while CI kept the flat paths, so they ImportError'd on every
    # publish and were not running at all. A guard whose scanner silently matches
    # nothing reports "no offenders" forever, which reads exactly like safety.
    "drift_scanner_blind": {
        "why": "The action-list drift guard's dispatch scanner returns nothing, so "
               "every tool looks like it implements no actions and no drift can "
               "ever be reported.",
        "baseline": "not previously measured — the guard had no scan-count assertion",
        "min_failures": 1,
        "patch": """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path.cwd() / "tests"))
    import test_action_list_drift as guard
    guard._implemented_actions = lambda fn: set()
""",
    },
    "live_harness_scan_blind": {
        "why": "The live-harness naming guard's file glob matches nothing, so the "
               "check that no test_*.py reaches a live Resolve passes vacuously — "
               "audit #111's findings 1-3 would sail through again.",
        "baseline": "not previously measured",
        "min_failures": 1,
        "patch": """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path.cwd() / "tests"))
    import test_live_harness_naming as guard
    guard._test_modules = lambda: []
""",
    },
    "double_audit_scan_blind": {
        "why": "The hand-rolled-double audit finds no classes, so a second copy of "
               "the bridge's fabrication model — the #119 bug class itself — would "
               "no longer be reported.",
        "baseline": "not previously measured",
        "min_failures": 1,
        "patch": """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path.cwd() / "tests"))
    import test_hand_rolled_double_audit as guard
    guard._hand_rolled_doubles = lambda: []
""",
    },
    "discarded_return_scan_blind": {
        "why": "The discarded-mutator-return scanner stops recognising mutator "
               "names, so every dropped Resolve return (the #110 / #111 finding-5 "
               "and -6 shape) becomes invisible and the baseline reads as fixed.",
        "baseline": "not previously measured",
        "min_failures": 1,
        "patch": """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path.cwd() / "tests"))
    import test_discarded_mutator_returns as guard
    guard._is_mutator = lambda name: False
""",
    },
    "text_encoding_scan_blind": {
        "why": "The text-encoding guard's src/ walk matches nothing, so every "
               "encoding-less subprocess(text=True)/open() reads as absent — the "
               "#124 bug class reintroduced with the guard still green.",
        "baseline": "not previously measured — guard added by #124",
        "min_failures": 1,
        "patch": """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path.cwd() / "tests"))
    import test_text_encoding_guard as guard
    guard.SRC = pathlib.Path.cwd() / "no_such_src_directory"
""",
    },
    "granular_gate_open": {
        "why": "The granular confirm gate always says 'proceed' — #138 itself: the "
               "same destructive op guarded through the compound tool and unguarded "
               "through `src/server.py --full`.",
        "baseline": "0 failed before #138 — no granular module referenced the gate",
        "min_failures": 5,
        "patch": """
    import src.granular.folder as _folder
    import src.granular.graph as _graph
    import src.granular.media_pool as _mp
    import src.granular.media_pool_item as _mpi
    import src.granular.project as _project
    import src.granular.timeline as _timeline

    # Each granular module star-imports the guard, so it holds its OWN binding —
    # patching guards.confirm_gate alone would leave every call site untouched.
    for _mod in (_folder, _graph, _mp, _mpi, _project, _timeline):
        _mod.confirm_gate = lambda **kw: None
""",
    },
    "granular_ledger_silent": {
        "why": "The granular AI-ops ledger records nothing — #139: a --full session "
               "leaves the ledger empty while it still reads as an authoritative "
               "record of what AI work ran against the project.",
        "baseline": "0 failed before #139 — no granular module referenced the ledger",
        "min_failures": 3,
        "patch": """
    import contextlib
    import src.granular.folder as _folder
    import src.granular.media_pool_item as _mpi
    import src.granular.project as _project

    class _Rec:
        success = False
        output_path = None
        output_bytes = None

    @contextlib.contextmanager
    def _noop(op, **kw):
        yield _Rec()

    for _mod in (_folder, _mpi, _project):
        _mod.ledger_timed = _noop
""",
    },
    "granular_guard_scan_blind": {
        "why": "The granular guard-drift scanner finds no modules, so a new "
               "destructive or AI granular tool landing ungated/unrecorded is no "
               "longer reported — #138/#139 reintroduced with the guard still green.",
        "baseline": "not previously measured — guard added by #138/#139",
        "min_failures": 1,
        "patch": """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path.cwd() / "tests"))
    import test_granular_guard_drift as guard
    guard._granular_modules = lambda: iter(())
""",
    },
    "catastrophic_sink_scan_blind": {
        "why": "The catastrophic-sink scanner returns no call sites, so a new "
               "DeleteAllRenderJobs()/DeleteProject() anywhere in src/ is no longer "
               "flagged — #110 finding 3 with the guard still green.",
        "baseline": "not previously measured",
        "min_failures": 1,
        "patch": """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path.cwd() / "tests"))
    import test_destructive_registry_drift as guard
    guard._sink_call_sites = lambda: {}
""",
    },
}

_PLUGIN_TEMPLATE ='''"""Generated by scripts/mutation_gate.py — do not check in."""


def pytest_configure(config):{patch}
'''

_SUMMARY_RE = re.compile(r"(\d+) failed")


def _run_mutation(name: str, spec: dict, workdir: pathlib.Path) -> tuple[bool, str]:
    plugin_name = f"mutation_{name}"
    patch = spec["patch"].rstrip("\n")
    if not patch.strip():
        return False, "empty patch"
    (workdir / f"{plugin_name}.py").write_text(
        _PLUGIN_TEMPLATE.format(patch=patch), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", plugin_name],
        cwd=REPO_ROOT,
        env={**_env(), "PYTHONPATH": f"{workdir}:{REPO_ROOT}"},
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    match = _SUMMARY_RE.search(output)
    failures = int(match.group(1)) if match else 0

    if proc.returncode == 0:
        return False, f"SURVIVED — suite stayed green ({failures} failed)"
    if failures < spec["min_failures"]:
        return False, (f"too weakly killed — {failures} failed, "
                       f"floor is {spec['min_failures']}")
    return True, f"killed by {failures} failing tests (floor {spec['min_failures']})"


def _env():
    import os
    return dict(os.environ)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true",
                        help="Print the mutation inventory and exit.")
    parser.add_argument("--only", action="append", default=[],
                        help="Run only the named mutation (repeatable).")
    args = parser.parse_args(argv)

    if args.list:
        for name, spec in MUTATIONS.items():
            print(f"{name}\n    {spec['why']}\n    floor: {spec['min_failures']} "
                  f"(baseline before #119: {spec['baseline']})")
        return 0

    selected = args.only or list(MUTATIONS)
    unknown = [n for n in selected if n not in MUTATIONS]
    if unknown:
        print(f"unknown mutation(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    survivors = []
    with tempfile.TemporaryDirectory(prefix="mutation_gate_") as tmp:
        workdir = pathlib.Path(tmp)
        for name in selected:
            print(f"=== {name} ===", flush=True)
            print(f"    {MUTATIONS[name]['why']}", flush=True)
            killed, detail = _run_mutation(name, MUTATIONS[name], workdir)
            print(f"    {'OK' if killed else 'FAIL'}: {detail}", flush=True)
            if not killed:
                survivors.append(f"{name}: {detail}")

    print()
    if survivors:
        print(f"MUTATION GATE FAILED — {len(survivors)}/{len(selected)} survived:")
        for line in survivors:
            print(f"  - {line}")
        print("\nThe offline suite can no longer see a defect that has shipped "
              "before. Add coverage; do not lower the floor.")
        return 1
    print(f"MUTATION GATE PASSED — {len(selected)}/{len(selected)} mutations killed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
