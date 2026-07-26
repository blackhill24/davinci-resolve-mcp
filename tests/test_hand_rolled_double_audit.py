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
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

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

    def test_internal_helper_patches_use_autospec(self):
        """The other half of the policy: mocks stay, but they must carry a spec.

        `ResolveBridgeDouble` replaces a mock only where the object is a *bridge*
        object. Patching an internal helper (`_get_mp`, `_get_timeline`,
        `_find_clip_by_id`) is still a mock's job — but an unspecced one accepts any
        signature, so a test keeps passing after the helper it patches changes
        arity. `autospec=True` makes that a failure. Before #119 there were zero
        uses of `spec=`/`autospec` anywhere under tests/.
        """
        gate_tests = [
            TESTS / "core/test_capability_gate_behaviour.py",
            TESTS / "core/test_granular_export_path.py",
        ]
        for path in gate_tests:
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                patched_functions = source.count('mock.patch.object(granular')
                autospecced = source.count("autospec=True")
                self.assertGreater(autospecced, 0)
                # The module-level `resolve` handle is data, not a callable, so it
                # is patched without a spec; everything else is a function.
                self.assertGreaterEqual(
                    autospecced, patched_functions - source.count('"resolve", resolve'),
                    f"{path.name} patches a helper without autospec")

    def test_autospec_actually_catches_signature_drift(self):
        """Non-vacuous check on the claim above."""
        from unittest import mock

        class Helper:
            @staticmethod
            def get(clip_id):
                return clip_id

        with mock.patch.object(Helper, "get", autospec=True, return_value="x"):
            with self.assertRaises(TypeError):
                Helper.get("a", "b")          # arity drift is caught

        with mock.patch.object(Helper, "get", return_value="x"):
            self.assertEqual("x", Helper.get("a", "b"))   # unspecced: silently fine

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


class FabricationDetectorIsNotVacuousTest(unittest.TestCase):
    """The fabrication check is fed a fabricating double (#121 task 2).

    `test_no_hand_rolled_double_reimplements_bridge_fabrication` asserts an
    empty offender list. That is indistinguishable from an AST walk that stopped
    recognising class definitions — and the whole point of #119 was that a
    second, unpinned copy of the bridge model is how the bug class spreads. So
    the offender is recreated in a temp tree and the failure asserted.
    """

    def _tests_tree(self, files: dict):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        for name, body in files.items():
            (root / name).write_text(textwrap.dedent(body), encoding="utf-8")
        return mock.patch.multiple(
            sys.modules[__name__],
            TESTS=root,
            REPO_ROOT=root.parent,
            _SHARED_DOUBLE=root / "bridge_double.py",
        )

    def test_detects_a_double_that_fabricates_attributes(self):
        files = {
            "test_synthetic_double.py": """
            class FakeTimeline:
                def __getattr__(self, name):
                    return None
            """
        }
        with self._tests_tree(files):
            with self.assertRaises(AssertionError) as caught:
                HandRolledDoubleAuditTest(
                    "test_no_hand_rolled_double_reimplements_bridge_fabrication"
                ).debug()
        message = str(caught.exception)
        self.assertIn("FakeTimeline defines __getattr__", message)

    def test_detects_a_double_that_hides_names_from_dir(self):
        files = {
            "test_synthetic_double.py": """
            class StubResolve:
                def __dir__(self):
                    return ["GetProjectManager"]
            """
        }
        with self._tests_tree(files):
            with self.assertRaises(AssertionError) as caught:
                HandRolledDoubleAuditTest(
                    "test_no_hand_rolled_double_reimplements_bridge_fabrication"
                ).debug()
        self.assertIn("StubResolve defines __dir__", str(caught.exception))

    def test_the_shared_double_is_exempt(self):
        # bridge_double.py is the one place allowed to model fabrication; if the
        # exemption broke, the guard would fail on the very file it points people at.
        files = {
            "bridge_double.py": """
            class FakeBridgeObject:
                def __getattr__(self, name):
                    return None
            """
        }
        with self._tests_tree(files):
            HandRolledDoubleAuditTest(
                "test_no_hand_rolled_double_reimplements_bridge_fabrication"
            ).debug()


if __name__ == "__main__":
    unittest.main()
