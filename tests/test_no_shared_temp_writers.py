"""No writer may build its temp file at a name another writer could also pick.

`os.replace` is atomic; the temp file it publishes is not. Two writers that open
the *same* temp path share one buffer — the second truncates the first's write
mid-flight, and `os.replace` then publishes the spliced result. For
`corrections.json` that is unrecoverable in practice: `_v2_read_corrections`
runs with `strict=True` and refuses to overwrite an unparseable file, so a
single lost race freezes that clip's human edit history until someone repairs
the JSON by hand.

#169 fixed thirteen such sites, then #170 found two more that its grep could not
see, because #169 matched `path + ".tmp"` and `f"{path}.tmp"` but not the
subscript form `f"{paths['progress_json']}.tmp"`. That is the lesson this guard
exists to encode: **a twin search written from the one example you have will
miss the variants.** A grep cannot be trusted here, so this walks the AST
instead and catches every spelling of "a string literal ending in `.tmp`" at
once — concatenation, plain f-string, and subscript f-string alike.

The approved idiom is a per-writer name, which the repo spells:

    f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"

Its final f-string segment is an interpolation, not the literal `.tmp`, so it is
invisible to the rule below — as is any other name carrying a uniquifying
suffix. If this test fails, do not special-case it: give the writer a unique
temp name and reclaim it on the failure path.
"""

from __future__ import annotations

import ast
import os
import unittest

_SRC_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _ends_in_bare_tmp(node: ast.AST) -> bool:
    """True when this expression yields a string ending in the literal ``.tmp``.

    Covers ``x + ".tmp"`` and any f-string whose trailing segment is ``.tmp``.
    A unique name ends in an interpolation, so it never matches.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        right = node.right
        return isinstance(right, ast.Constant) and isinstance(right.value, str) and right.value.endswith(".tmp")
    if isinstance(node, ast.JoinedStr) and node.values:
        last = node.values[-1]
        return isinstance(last, ast.Constant) and isinstance(last.value, str) and last.value.endswith(".tmp")
    return False


def _iter_python_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


class NoSharedTempNameTest(unittest.TestCase):
    def test_no_writer_builds_a_shared_temp_name(self):
        offenders = []
        for path in _iter_python_files(_SRC_ROOT):
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            try:
                tree = ast.parse(source, filename=path)
            except SyntaxError as exc:  # pragma: no cover - a parse error is its own bug
                self.fail(f"{path} does not parse: {exc}")
            for node in ast.walk(tree):
                if _ends_in_bare_tmp(node):
                    rel = os.path.relpath(path, os.path.dirname(_SRC_ROOT))
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(
            [],
            offenders,
            "these build a temp path at a name a concurrent writer could also "
            "pick, which defeats the atomicity os.replace provides; use "
            'f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}" '
            f"and reclaim it on the failure path: {offenders}",
        )

    def test_the_rule_recognises_all_three_spellings(self):
        # The guard is only worth its runtime if it sees the variant #169's grep
        # missed. Each of these is a real form that appeared in this repo.
        for source in (
            'tmp = path + ".tmp"',                      # concatenation (#169)
            'tmp = f"{path}.tmp"',                      # plain f-string (#169)
            'tmp = f"{paths[\'progress_json\']}.tmp"',  # subscript f-string (#170)
        ):
            with self.subTest(source=source):
                node = ast.parse(source).body[0].value
                self.assertTrue(_ends_in_bare_tmp(node), f"rule missed: {source}")

    def test_the_rule_accepts_the_unique_idiom(self):
        source = 'tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"'
        node = ast.parse(source).body[0].value
        self.assertFalse(_ends_in_bare_tmp(node), "the approved unique-name idiom must not be flagged")


if __name__ == "__main__":
    unittest.main()
