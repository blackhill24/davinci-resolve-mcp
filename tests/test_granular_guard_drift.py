"""Drift guard: the granular (`--full`) surface may not bypass the gate or the ledger.

#138 and #139 were the same defect in two subsystems. `src/core/tool_kernel.py`
gates destructive compound actions behind a two-call confirm token and records
every AI/render op in the Resolve-AI ledger, and NO granular module referenced
either one — so the same Resolve API call was guarded through the compound tool
and unguarded through `python src/server.py --full`, and a `--full` session left
the ledger empty while it still read as an authoritative record.

Fixing the ten sites is not the durable half. This file is: it AST-scans
`src/granular/` for calls to the destructive / AI-op API methods and fails when a
caller is not registered in `src/granular/guards.py` or does not actually invoke
the guard. A NEW destructive granular tool therefore cannot land on the wrong
side of the decision — which is exactly how the original gap survived review.

Both directions are checked, because each catches a different bug:

SOUNDNESS  — every registry entry names a granular function that exists and that
             really gates on the action string claimed for it. Catches a rename
             or a deleted gate that leaves a stale, reassuring table entry.
COMPLETENESS — every granular call to a listed API method has a registry entry.
             Catches the new-tool-lands-ungated case.

Plus two cross-surface ties, so the granular strings cannot quietly drift away
from what the compound server actually issues and records:
  * each gated action string must be issued by a real compound `_issue_confirm_token`
  * each ledger op string must be an `OP_META` key
"""
from __future__ import annotations

import ast
import pathlib
import unittest

from src.core.resolve_ai_ledger import OP_META
from src.granular.guards import (
    GATED_API_METHODS,
    GRANULAR_CONFIRM_SITES,
    LEDGERED_API_METHODS,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
GRANULAR_DIR = ROOT / "src" / "granular"
DOMAIN_ACTION_FILES = sorted((ROOT / "src" / "domains").glob("*/actions.py"))

# guards.py holds the registries themselves and common.py only re-exports them;
# neither calls the Resolve API, so scanning them would only find the tables.
SKIP_MODULES = {"guards", "common", "__init__"}


def _granular_modules():
    for path in sorted(GRANULAR_DIR.glob("*.py")):
        if path.stem in SKIP_MODULES:
            continue
        yield path.stem, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _called_methods(fn):
    """Attribute-call names inside fn: `x.DeleteClips(...)` -> "DeleteClips"."""
    return {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _guard_actions(fn):
    """String literals passed as `action=` to confirm_gate() inside fn."""
    actions = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "confirm_gate":
            continue
        for kw in node.keywords:
            if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                actions.add(kw.value.value)
    return actions


def _ledger_ops(fn):
    """First positional string of each ledger_timed(...) call inside fn."""
    ops = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "ledger_timed":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            ops.add(node.args[0].value)
    return ops


def _compound_issued_actions():
    """Every `action=` literal handed to _issue_confirm_token across the domains."""
    issued = set()
    for path in DOMAIN_ACTION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "_issue_confirm_token":
                continue
            for kw in node.keywords:
                if kw.arg == "action" and isinstance(kw.value, ast.Constant):
                    issued.add(kw.value.value)
    return issued


class ScannerIsNotBlind(unittest.TestCase):
    """A scanning guard whose glob matches nothing reads as safety and provides none.

    #110 shipped with two drift guards that had `ImportError`'d out of every CI run
    unnoticed. Every check below is "no counterexample found", so the scan finding
    nothing would pass all of them — pin the scan itself (tests/GUARDS.md).
    """

    def test_the_granular_scan_finds_modules_and_functions(self):
        modules = dict(_granular_modules())
        self.assertGreater(len(modules), 5, "the src/granular/*.py glob went blind")
        total_functions = sum(len(list(_functions(t))) for t in modules.values())
        self.assertGreater(total_functions, 100, "granular modules parsed but empty")

    def test_the_compound_scan_finds_issued_actions(self):
        self.assertGreater(len(_compound_issued_actions()), 5,
                           "the src/domains/*/actions.py scan went blind")

    def test_the_registries_are_not_empty(self):
        self.assertGreaterEqual(len(GRANULAR_CONFIRM_SITES), 10)
        self.assertGreaterEqual(len(LEDGERED_API_METHODS), 7)


class GranularConfirmGateDrift(unittest.TestCase):
    def test_registry_entries_point_at_real_gating_functions(self):
        """SOUNDNESS: every registered site exists and gates on its own action."""
        by_module = {}
        for module, tree in _granular_modules():
            by_module[module] = {fn.name: fn for fn in _functions(tree)}

        for action, (module, func_name) in sorted(GRANULAR_CONFIRM_SITES.items()):
            self.assertIn(module, by_module, f"{action}: no granular module {module!r}")
            fn = by_module[module].get(func_name)
            self.assertIsNotNone(fn, f"{action}: src/granular/{module}.py has no {func_name}()")
            self.assertIn(
                action, _guard_actions(fn),
                f"{action}: {module}.{func_name}() does not call "
                f"confirm_gate(action={action!r}) — the registry says it is gated "
                f"but the code no longer gates it.",
            )
            self.assertIn(
                "confirm_token", [a.arg for a in fn.args.args],
                f"{action}: {module}.{func_name}() has no confirm_token parameter, "
                f"so a caller cannot ever satisfy the gate.",
            )

    def test_every_destructive_api_call_is_registered_and_gated(self):
        """COMPLETENESS: a new ungated destructive granular tool fails here."""
        registered = {(m, f): a for a, (m, f) in GRANULAR_CONFIRM_SITES.items()}
        for module, tree in _granular_modules():
            for fn in _functions(tree):
                hits = _called_methods(fn) & set(GATED_API_METHODS)
                if not hits:
                    continue
                action = registered.get((module, fn.name))
                self.assertIsNotNone(
                    action,
                    f"src/granular/{module}.py:{fn.name}() calls {sorted(hits)} but is "
                    f"not in GRANULAR_CONFIRM_SITES. Destructive granular tools are "
                    f"gated (#138) — register it and call confirm_gate(), or, if it "
                    f"genuinely is not destructive, say why in guards.py.",
                )
                allowed = set().union(*(GATED_API_METHODS[m] for m in hits))
                self.assertIn(
                    action, allowed,
                    f"src/granular/{module}.py:{fn.name}() gates on {action!r}, which "
                    f"is not one of the actions {sorted(allowed)} that {sorted(hits)} "
                    f"may be gated under.",
                )

    def test_gated_actions_match_the_compound_server(self):
        """The two surfaces must name the same operation, not two spellings of it."""
        issued = _compound_issued_actions()
        for action in sorted(GRANULAR_CONFIRM_SITES):
            self.assertIn(
                action, issued,
                f"{action!r} is gated on the granular surface but no compound "
                f"_issue_confirm_token uses that string — one side was renamed.",
            )


class GranularLedgerDrift(unittest.TestCase):
    def test_ledgered_methods_map_to_real_op_meta_keys(self):
        for method, op in sorted(LEDGERED_API_METHODS.items()):
            self.assertIn(
                op, OP_META,
                f"{method} is recorded under op {op!r}, which is not an OP_META key "
                f"— the ledger would carry an op name nothing can aggregate.",
            )

    def test_every_ai_op_call_records_through_the_ledger(self):
        """#139: a `--full` session must not leave the ledger silently empty."""
        for module, tree in _granular_modules():
            for fn in _functions(tree):
                for method in sorted(_called_methods(fn) & set(LEDGERED_API_METHODS)):
                    op = LEDGERED_API_METHODS[method]
                    self.assertIn(
                        op, _ledger_ops(fn),
                        f"src/granular/{module}.py:{fn.name}() calls {method}() without "
                        f"ledger_timed({op!r}). Every AI/render op records on both "
                        f"surfaces (#139), otherwise the ledger reads as authoritative "
                        f"while missing this run.",
                    )

    def test_every_op_meta_key_with_a_granular_site_is_covered(self):
        """OP_META entries reachable from --full must all have a recording site."""
        recorded = set()
        for _module, tree in _granular_modules():
            for fn in _functions(tree):
                recorded |= _ledger_ops(fn)
        for op in sorted(set(LEDGERED_API_METHODS.values())):
            self.assertIn(
                op, recorded,
                f"OP_META op {op!r} has a granular equivalent but no granular "
                f"ledger_timed({op!r}) call site.",
            )


if __name__ == "__main__":
    unittest.main()
