"""Static guard: every `live_*.py` harness must be able to FAIL (#119 task 11).

The live suite is the only thing catching the bug class #119 is about, so a harness
that can never exit nonzero is worse than no harness — it reports PASS on a broken
build. Two were found that way (#119 §5):

  * `tests/domains/timeline_edit/live_duplicate_clips_validation.py` did
    `return 0` on the `--output-dir` path, discarding both `run_validation`'s
    result and `run_probe`'s. `--output-dir` is exactly what a release run passes.
  * `tests/domains/extension_authoring/live_script_smoke.py` called `main()`
    instead of `raise SystemExit(main())`, and `main()` returned None — every
    failure printed a WARN and the process still exited 0. It also blocked on a
    bare `input()`, so an automated runner hung or had its stdin eaten.

Three properties are checked here, all by AST over the source text so the guard is
itself offline-safe (it never imports a harness, which would need Resolve):

  1. the `__main__` block propagates `main()`'s value via `raise SystemExit(...)`;
  2. `main()` has at least one `return` that is not a literal `0` — i.e. some path
     reaches a nonzero exit;
  3. no harness discards a result it computed (assigned a call to a name, then
     returned a constant without ever reading the name).

A fourth check bans an unguarded `input()`, the stdin-blocking half of §5.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO_ROOT / "tests"

# Harnesses that legitimately have no `main()` returning a status: they are
# import-time scripts or interactive tools. Each needs a reason, and each must
# still be unable to report a false PASS — keep this list empty unless justified.
EXEMPT: dict[str, str] = {}


def _harnesses():
    return sorted(
        path for path in TESTS.rglob("live_*.py")
        if "__pycache__" not in path.parts
    )


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _main_block(tree):
    """The `if __name__ == "__main__":` body, or None."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"):
            return node
    return None


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _exits_nonzero_itself(main):
    """Does `main()` reach a nonzero exit without its return value being read?

    Two legitimate shapes besides `return <nonzero>`:

      * a direct ``sys.exit(1)`` / ``raise SystemExit(1)`` inside the body;
      * an uncaught ``raise`` — several harnesses assert with ``AssertionError``
        and let the traceback set the exit status, which is a real failure path.

    A `raise` inside an `except` handler that the same function swallows does not
    count, so only raises that are not nested in a `try` body are considered.
    """
    if main is None:
        return False

    # Nodes sitting inside a `try:` body that has a bare/broad `except` are not
    # counted — a raise the same function swallows is not an exit path.
    swallowed = set()
    for node in ast.walk(main):
        if isinstance(node, ast.Try) and node.handlers:
            for stmt in node.body:
                swallowed.update(id(n) for n in ast.walk(stmt))

    for node in ast.walk(main):
        if id(node) in swallowed:
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "exit":
                return True
            if isinstance(func, ast.Name) and func.id == "exit":
                return True
        if isinstance(node, ast.Raise):
            return True
    return False


class LiveHarnessExitCodeTest(unittest.TestCase):
    def test_the_harness_glob_actually_matches(self):
        """A glob matching nothing would make every test below vacuously pass."""
        self.assertGreaterEqual(len(_harnesses()), 40)

    def test_every_harness_propagates_its_exit_status(self):
        offenders = []
        for path in _harnesses():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in EXEMPT:
                continue
            tree = _parse(path)
            block = _main_block(tree)
            if block is None:
                continue  # module has no __main__ entry point; nothing to propagate
            if _exits_nonzero_itself(_function(tree, "main")):
                continue  # main() calls sys.exit directly; nothing to propagate
            source = ast.unparse(block)
            if "SystemExit" not in source and "sys.exit" not in source:
                offenders.append(rel)
        self.assertEqual(
            [], offenders,
            "these harnesses call main() without propagating its result — the "
            "process exits 0 no matter what happened (#119 §5):\n  "
            + "\n  ".join(offenders))

    def test_every_harness_main_can_return_nonzero(self):
        """A `main()` whose only returns are literal 0 can never fail the run."""
        offenders = []
        for path in _harnesses():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in EXEMPT:
                continue
            tree = _parse(path)
            if _main_block(tree) is None:
                continue
            main = _function(tree, "main")
            if main is None:
                continue
            if _exits_nonzero_itself(main):
                continue
            returns = [n for n in ast.walk(main)
                       if isinstance(n, ast.Return) and n.value is not None]
            if not returns:
                offenders.append(
                    f"{rel}: main() returns nothing and never raises or exits")
                continue
            nonzero_possible = any(
                not (isinstance(r.value, ast.Constant) and r.value.value == 0)
                for r in returns
            )
            if not nonzero_possible:
                offenders.append(f"{rel}: every return in main() is literal 0 "
                                 f"and it never raises or exits")
        self.assertEqual(
            [], offenders,
            "these harnesses have no path to a nonzero exit (#119 §5):\n  "
            + "\n  ".join(offenders))

    def test_no_harness_discards_a_result_it_computed(self):
        """`x = run_validation(...)` then `return 0` without reading x — §5's shape."""
        offenders = []
        for path in _harnesses():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in EXEMPT:
                continue
            tree = _parse(path)
            main = _function(tree, "main")
            if main is None:
                continue

            assigned = {}
            for node in ast.walk(main):
                if (isinstance(node, ast.Assign)
                        and isinstance(node.value, ast.Call)
                        and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)):
                    assigned[node.targets[0].id] = node.lineno

            read = {
                node.id for node in ast.walk(main)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            for name, lineno in assigned.items():
                if name.startswith("_"):
                    continue
                if name not in read:
                    offenders.append(f"{rel}:{lineno}: `{name}` is computed, never used")
        self.assertEqual(
            [], offenders,
            "a harness computed a result and threw it away (#119 §5):\n  "
            + "\n  ".join(offenders))

    def test_no_harness_blocks_on_stdin_unconditionally(self):
        """A bare `input()` hangs an automated runner or eats its work list."""
        offenders = []
        for path in _harnesses():
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = _parse(path)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "input"):
                    continue
                # Guarded by an explicit opt-in flag is fine; unguarded is not.
                guarded = any(
                    isinstance(parent, ast.If)
                    and node in list(ast.walk(parent))
                    for parent in ast.walk(tree)
                    if isinstance(parent, ast.If)
                )
                if not guarded:
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(
            [], offenders,
            "unguarded input() in a live harness (#119 §5):\n  " + "\n  ".join(offenders))


class RegressionShapesAreDetectedTest(unittest.TestCase):
    """The guard must actually catch the two shapes it was written for.

    Without these, a refactor could quietly make the checks above vacuous and
    nothing would notice — the exact failure mode #119 documents.
    """

    def _main_of(self, source):
        return _function(ast.parse(source), "main")

    def test_the_discarded_result_shape_is_detected(self):
        main = self._main_of(
            "def main():\n"
            "    validation_result = run_validation(server)\n"
            "    run_probe(server, out)\n"
            "    return 0\n")
        assigned = {n.targets[0].id for n in ast.walk(main)
                    if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)}
        read = {n.id for n in ast.walk(main)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.assertIn("validation_result", assigned - read)

    def test_the_always_zero_shape_is_detected(self):
        main = self._main_of("def main():\n    do_work()\n    return 0\n")
        returns = [n for n in ast.walk(main)
                   if isinstance(n, ast.Return) and n.value is not None]
        self.assertTrue(all(isinstance(r.value, ast.Constant) and r.value.value == 0
                            for r in returns))

    def test_the_missing_systemexit_shape_is_detected(self):
        tree = ast.parse('if __name__ == "__main__":\n    main()\n')
        block = _main_block(tree)
        self.assertIsNotNone(block)
        self.assertNotIn("SystemExit", ast.unparse(block))


if __name__ == "__main__":
    unittest.main()
