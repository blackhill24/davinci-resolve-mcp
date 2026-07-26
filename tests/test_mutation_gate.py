"""Fast structural checks on the mutation gate itself (#119 task 12).

`scripts/mutation_gate.py` runs the whole offline suite once per mutation, so it is
a CI step rather than a unit test. This file is the cheap half: it verifies the gate
is well-formed and actually wired up, so a typo or a dropped CI step cannot silently
disable it — which would put the repo straight back in the position #119 describes,
believing a green suite means something it does not.

It deliberately does NOT run the mutations.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import mutation_gate  # noqa: E402

WORKFLOW = REPO_ROOT / ".github/workflows/npm-publish.yml"


class MutationInventoryTest(unittest.TestCase):
    def test_the_helpers_that_regressed_are_all_covered(self):
        patches = "\n".join(spec["patch"] for spec in mutation_gate.MUTATIONS.values())
        self.assertIn("_api_constant", patches)
        self.assertIn("_has_method", patches)

    def test_both_helper_copies_are_targeted(self):
        """§1: the granular binding is the one the export path uses."""
        patches = "\n".join(spec["patch"] for spec in mutation_gate.MUTATIONS.values())
        self.assertIn("src.core.envelope", patches)
        self.assertIn("src.granular.common", patches)

    def test_every_mutation_declares_why_and_a_floor(self):
        for name, spec in mutation_gate.MUTATIONS.items():
            with self.subTest(mutation=name):
                self.assertTrue(spec["why"].strip(), "a mutation needs a rationale")
                self.assertIn("baseline", spec)
                self.assertIsInstance(spec["min_failures"], int)
                self.assertGreater(spec["min_failures"], 0,
                                   "a floor of 0 would let the mutation survive")

    def test_every_patch_is_valid_python_that_assigns_something(self):
        for name, spec in mutation_gate.MUTATIONS.items():
            with self.subTest(mutation=name):
                body = spec["patch"]
                tree = ast.parse(f"def pytest_configure(config):{body}")
                assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
                self.assertTrue(assigns,
                                f"{name} patches nothing — it would trivially 'pass'")

    def test_no_two_mutations_are_identical(self):
        patches = [spec["patch"].strip() for spec in mutation_gate.MUTATIONS.values()]
        self.assertEqual(len(patches), len(set(patches)))

    def test_the_gate_rejects_an_unknown_mutation_name(self):
        self.assertEqual(2, mutation_gate.main(["--only", "no_such_mutation"]))

    def test_listing_the_inventory_is_cheap_and_succeeds(self):
        self.assertEqual(0, mutation_gate.main(["--list"]))


class MutationGateIsWiredIntoCiTest(unittest.TestCase):
    def test_the_publish_workflow_runs_the_gate(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("scripts/mutation_gate.py", workflow,
                      "the mutation gate is not run by CI — #119 task 12 was undone")

    def test_the_gate_runs_before_publishing(self):
        """A gate that runs after the npm publish step gates nothing."""
        workflow = WORKFLOW.read_text(encoding="utf-8")
        gate_at = workflow.index("scripts/mutation_gate.py")
        publish_at = workflow.rindex("npm publish")
        self.assertLess(gate_at, publish_at)


if __name__ == "__main__":
    unittest.main()
