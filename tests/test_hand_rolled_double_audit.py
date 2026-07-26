"""Audit of the hand-rolled `Fake*`/`Stub*`/`Dummy*` doubles (#119 task 5).

The issue asked for these ~30 classes (51, as counted below) to be audited for the
same divergence that makes `MagicMock` dangerous, and folded into the shared double
where they model a bridge object. The audit's finding, which this file pins:

**They diverge in the opposite, safe direction.** A hand-rolled double is an ordinary
Python class, so `dir()` lists exactly the methods it defines — which is precisely
what the real bridge does, and precisely what `_has_method` needs. Where they differ
from the bridge is fabrication: `getattr(fake, 'MadeUpName')` raises `AttributeError`
where the bridge returns `None`, and `hasattr` is `False` where the bridge is always
`True`. That makes a hand-rolled double *stricter* than production, so it surfaces
unexpected calls rather than hiding them. It is not the failure mode behind #119.

`MagicMock` fails the other way, and that is the dangerous one: `dir()` on a mock
lists only the children a test has touched, so any method the test did not configure
reads as absent, the capability gate closes, and the test asserts on the fallback
having never run the path it was written for.

So the audit outcome is: leave the hand-rolled doubles alone where they only need
`dir()` fidelity, and use `ResolveBridgeDouble` wherever a test needs **fabrication**
semantics or exercises a `_has_method` / `_api_constant` gate. What this file
enforces is that nobody re-implements the bridge's fabrication behaviour by hand —
that is the one thing that must have a single definition, since a second, subtly
wrong copy of it is how the class of bug in #119 propagates.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent

_DOUBLE_PREFIXES = ("Fake", "Stub", "Dummy")

# The shared double lives here and is the only place allowed to model fabrication.
_SHARED_DOUBLE = TESTS / "bridge_double.py"


def _test_modules():
    return sorted(p for p in TESTS.rglob("test_*.py") if "__pycache__" not in p.parts)


def _hand_rolled_doubles():
    """(path, class name, methods) for every hand-rolled double under tests/."""
    found = []
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.startswith(_DOUBLE_PREFIXES):
                methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
                found.append((path, node.name, methods))
    return found


class HandRolledDoubleAuditTest(unittest.TestCase):
    def test_the_audit_scan_finds_something(self):
        """A scan matching nothing would make every assertion below vacuous."""
        doubles = _hand_rolled_doubles()
        self.assertGreater(len(doubles), 20)
        self.assertGreater(len({p for p, _n, _m in doubles}), 10)

    def test_no_hand_rolled_double_reimplements_bridge_fabrication(self):
        """`__getattr__`/`__dir__` on a test double = a second copy of the bridge model.

        If a test needs an object that fabricates unknown attributes as `None` and
        hides constants from `dir()`, it needs `ResolveBridgeDouble` — which is
        pinned by `tests/core/test_bridge_double_fidelity.py`. A hand-rolled version
        is unpinned by construction, and a *subtly wrong* one is exactly how this
        class of bug spreads.
        """
        offenders = []
        for path, name, methods in _hand_rolled_doubles():
            if path == _SHARED_DOUBLE:
                continue
            for dunder in ("__getattr__", "__dir__"):
                if dunder in methods:
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}::{name} defines {dunder}")
        self.assertEqual(
            [], offenders,
            "hand-rolled doubles must not model the bridge's fabrication behaviour; "
            "use tests.bridge_double.ResolveBridgeDouble instead (#119 task 5):\n  "
            + "\n  ".join(offenders))

    def test_a_plain_class_reports_its_own_methods_through_dir(self):
        """The audit's core claim, asserted rather than assumed."""
        class FakeTimeline:
            def GetName(self):
                return "cut_v3"

        fake = FakeTimeline()
        self.assertIn("GetName", dir(fake))
        self.assertNotIn("TotallyMadeUp", dir(fake))

    def test_a_plain_class_raises_where_the_bridge_fabricates(self):
        """The divergence — strict, not permissive, hence safe to leave in place."""
        class FakeTimeline:
            pass

        fake = FakeTimeline()
        self.assertFalse(hasattr(fake, "TotallyMadeUp"))   # bridge: True
        with self.assertRaises(AttributeError):            # bridge: returns None
            fake.TotallyMadeUp

    def test_the_shared_double_is_actually_adopted(self):
        """Task 4/5 only counts if the double is used, not merely available."""
        users = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in _test_modules()
            if "bridge_double" in path.read_text(encoding="utf-8")
            and path.name != "test_hand_rolled_double_audit.py"
        ]
        self.assertGreaterEqual(
            len(users), 8,
            f"only {len(users)} test modules use the shared double: {users}")


if __name__ == "__main__":
    unittest.main()
