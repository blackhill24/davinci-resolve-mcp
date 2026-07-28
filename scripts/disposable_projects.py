#!/usr/bin/env python3
"""Decide which Resolve projects are *disposable* — harness/pilot artifacts, not work.

`run_live_suite.py --clean-leaks` can only reclaim what the current sweep created:
it diffs the project list around the run, so it is safe by construction and can
never touch real work. That leaves the other half of the problem (issue #155):
projects leaked by *earlier* sessions accumulate forever, and cleaning them up
means someone eyeballing 30-odd names and guessing which are footage and which
are a probe. Nineteen had piled up before anyone asked.

The tempting fix — a hand-written regex list of probe prefixes — rots the day a
harness is added or renamed, and rots *dangerously*: a stale pattern that stops
matching leaves clutter (harmless), but a broad one written to cover the gap
("anything with `_probe_`") is one careless project name away from deleting
footage. So the prefixes are not written down here at all. They are **derived
from the harness sources**, the same discipline `run_live_suite.py:is_cold()`
uses to partition cold-launch harnesses: a project name is disposable only if
some `tests/**/live_*.py` in this repo demonstrably generates it. Add a harness
and its projects become reclaimable with no list to edit; delete a harness and
its prefix stops matching, which fails toward keeping things.

That derivation is the safety property, and it is deliberately conservative:

* Only *creation* counts. A harness assigning a literal to a project-ish
  variable name proves nothing on its own — the name has to reach a call that
  makes or owns the project (`CreateProject`, `project_name=`, or the MCP tool's
  `("create", {"name": …})`). Reading role-named literals directly, as this
  once did, would let a harness holding a media path in `probe_dir` mark a real
  `duck_footage_2024` for deletion.
* Only string constants and f-strings are read — a name assembled at runtime
  from a variable yields no prefix and its projects are simply kept.
* A prefix shorter than `MIN_PREFIX` is discarded. An f-string that starts with
  its interpolation has a leading literal of `""`, which would otherwise match
  every project in the database.
* `ALWAYS_KEEP` names are never disposable however they match — the live
  suite's own scratch project is infrastructure, not a leftover.
* Whatever all of that concludes, a name listed in `.disposable-keep` at the
  repo root (or passed to the runner as `--keep NAME`) is kept. Derivation is
  the default; that file is the user's override for their own projects.

Nothing here talks to Resolve; it is pure text-in/verdict-out so it can be
tested offline. The caller does the deleting.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Calls whose first argument is a project name the harness *owns*. `LoadProject`
# is deliberately absent: loading a name proves nothing about who made it, so a
# harness that opens a real project by name would otherwise make it reclaimable.
PROJECT_CALLS = {"CreateProject", "DeleteProject", "create_project"}

# Most harnesses do not touch the ProjectManager directly — they go through the
# MCP tool: `server.project_manager("create", {"name": project_name})`. That is
# the same evidence as CreateProject and has to be read, or six probes' projects
# are unreclaimable. The action word is required, so a read-only call carrying a
# `name` key is not mistaken for creation.
PROJECT_TOOL_ACTIONS = {"create", "delete"}
PROJECT_NAME_KEYS = {"name", "project_name"}

# Below this, a derived prefix is too weak to be evidence of anything. Four
# characters is enough for real prefixes (`duck`, `_mcp`) and rejects the ""
# that an f-string opening with `{...}` produces.
MIN_PREFIX = 4

# Names that match a derived prefix but must survive a sweep regardless: the
# live suite's own scratch project is infrastructure, and Resolve's default
# project is never persisted, so deleting it is not a cleanup (see #155).
ALWAYS_KEEP = frozenset({"ZZ_live_suite_scratch", "Untitled Project"})

# A user's own projects live in the same database as the harness leftovers, and
# the derivation above cannot know which of the two a name is if a real project
# ever collides with a probe prefix. This file is the manual override: one
# project name per line (`#` comments, blanks ignored), never disposable
# whatever the prefixes say. It is the answer to "don't touch my projects" —
# nothing here is derived, so nothing here can rot.
KEEP_FILE = ".disposable-keep"


def user_keeps(root: Path) -> frozenset:
    """Project names the user has declared off-limits in `<root>/.disposable-keep`.

    A missing or unreadable file yields nothing, which is the same as declaring
    nothing — the derived rules still apply. It never *adds* anything to a
    sweep, so a broken keep file cannot widen a delete.
    """
    try:
        text = root.joinpath(KEEP_FILE).read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    names = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return frozenset(n for n in names if n)


def _literal_prefix(node: ast.AST) -> str | None:
    """The fixed leading text of a string node, or None if it is not a string.

    `"fx_probe"` gives the whole thing; `f"fx_probe_{ts}"` gives `"fx_probe_"`;
    `f"{name}_probe"` gives `""` (rejected later by MIN_PREFIX); anything that
    is not a string literal at all gives None.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return ""
    return None


def prefixes_in_source(source: str) -> set:
    """Every project-name prefix a single harness source demonstrably generates.

    Every shape read here is a *creation*, because that is the only thing that
    proves the harness owns the name. The harnesses use four: a literal passed
    straight into a project call, a `project_name=` keyword, a plain local
    holding the literal that is handed to the call a line later
    (`name = f"multicam_probe_{ts}"; pm.CreateProject(name)`), and the MCP tool
    form `server.project_manager("create", {"name": project_name})` — which most
    probes use, so missing it left six of them unreclaimable. The local-variable
    shapes need one hop of constant lookup; without it a harness is silently
    unreclaimable for a reason no reader would ever guess from the name.

    The lookup is deliberately one hop and literals-only. Anything built from a
    variable, a join, or a call resolves to nothing and its projects are kept.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    # Pass 1: every variable in the file that is assigned a string literal,
    # regardless of what it is called. This is the lookup table for pass 2, not
    # a source of prefixes on its own — an arbitrary string assigned to an
    # arbitrary name is not evidence that a project was created.
    literals = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            prefix = _literal_prefix(node.value)
            if prefix is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    literals[target.id] = prefix

    def resolve(node: ast.AST) -> str | None:
        direct = _literal_prefix(node)
        if direct is not None:
            return direct
        return literals.get(node.id) if isinstance(node, ast.Name) else None

    # Only *creation* is evidence. Naming a variable `PILOT`/`probe_name` is not:
    # this used to seed the prefix set from any string literal assigned to a
    # project-ish name, so a future harness writing `probe_dir = "duck_footage"`
    # would have made a real `duck_footage_2024` reclaimable. Every prefix now
    # comes from a call that demonstrably makes or owns the project.
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        call = func.attr if isinstance(func, ast.Attribute) else \
            func.id if isinstance(func, ast.Name) else None
        if call in PROJECT_CALLS and node.args:
            prefix = resolve(node.args[0])
            if prefix is not None:
                found.add(prefix)
        for kw in node.keywords:
            if kw.arg == "project_name":
                prefix = resolve(kw.value)
                if prefix is not None:
                    found.add(prefix)
        # `server.project_manager("create", {"name": project_name})`
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and first.value in PROJECT_TOOL_ACTIONS:
            for arg in node.args[1:]:
                if not isinstance(arg, ast.Dict):
                    continue
                for key, value in zip(arg.keys, arg.values):
                    if isinstance(key, ast.Constant) and key.value in PROJECT_NAME_KEYS:
                        prefix = resolve(value)
                        if prefix is not None:
                            found.add(prefix)

    return {p for p in found if len(p) >= MIN_PREFIX}


def harness_prefixes(root: Path) -> set:
    """Union of the project-name prefixes across every live harness under `root`.

    `root` is the repo root; harnesses are `tests/**/live_*.py`, the same set
    `run_live_suite.py:discover()` sweeps.
    """
    prefixes = set()
    for path in sorted(root.joinpath("tests").rglob("live_*.py")):
        try:
            prefixes |= prefixes_in_source(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return prefixes


def is_disposable(name: str, prefixes: set, keep=frozenset()) -> bool:
    """True when `name` is a harness artifact: kept names never are, otherwise
    it must start with a derived prefix *and* carry a generated suffix.

    The suffix check is what keeps a hand-made project safe from a bare-constant
    prefix. `fx_probe_110438` is disposable; `fx_probe` — the literal name a
    harness reuses across runs, which a person could equally have typed — is
    only disposable when the harness names it exactly, which the equality arm
    below covers. Anything past the prefix must look machine-generated: a
    timestamp or counter, not prose.
    """
    if name in ALWAYS_KEEP or name in keep:
        return False
    for prefix in prefixes:
        if name == prefix:
            return True
        if name.startswith(prefix):
            rest = name[len(prefix):].lstrip("_")
            if rest.isdigit():
                return True
    return False


def classify(names, prefixes: set, keep=frozenset()) -> dict:
    """Split a project list into what a sweep may delete and what it must keep."""
    disposable = [n for n in names if is_disposable(n, prefixes, keep)]
    return {
        "disposable": disposable,
        "kept": [n for n in names if n not in set(disposable)],
    }


if __name__ == "__main__":  # pragma: no cover — a look at what would match
    import json
    import sys

    root = Path(__file__).resolve().parents[1]
    derived = sorted(harness_prefixes(root))
    print(json.dumps({"prefixes": derived,
                      "classify": classify(sys.argv[1:], set(derived))}, indent=2))
