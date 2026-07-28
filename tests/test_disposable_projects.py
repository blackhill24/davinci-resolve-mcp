"""What `scripts/disposable_projects.py` will and will not hand to a bulk delete (#155).

This module decides which projects `run_live_suite.py --clean-disposable --yes`
erases from the project database, so the interesting tests are the *refusals*.
A false negative leaves clutter; a false positive deletes somebody's footage.

The derivation is also load-bearing in a way a green test file can hide: the
prefixes come from the live harnesses, so this file checks the real
`tests/**/live_*.py` tree too — if a harness stops being readable, the sweep
quietly reclaims nothing and the pile #155 is about starts growing again.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import disposable_projects as dp  # noqa: E402


class PrefixDerivationTest(unittest.TestCase):
    def test_module_level_assignment_to_a_project_name(self):
        source = 'PILOT = f"auto_edit_pilot_{time.strftime(\'%H%M%S\')}"\n'
        self.assertEqual(dp.prefixes_in_source(source), {"auto_edit_pilot_"})

    def test_plain_constant_assignment(self):
        source = 'PROJECT_NAME = "ZZ_fusion_bug_reverify"\n'
        self.assertEqual(dp.prefixes_in_source(source), {"ZZ_fusion_bug_reverify"})

    def test_local_handed_to_create_project_one_hop_later(self):
        """The shape `live_multicam_drt_probe` uses: the variable is called
        `name`, which no role pattern matches, so only the call resolves it."""
        source = (
            "def phase_setup():\n"
            '    name = f"multicam_probe_{int(time.time())}"\n'
            "    pm.CreateProject(name)\n"
        )
        self.assertEqual(dp.prefixes_in_source(source), {"multicam_probe_"})

    def test_project_name_keyword_argument(self):
        source = 'PILOT = f"edit_engine_pilot_{ts}"\ns.auto_edit("start", project_name=PILOT)\n'
        self.assertIn("edit_engine_pilot_", dp.prefixes_in_source(source))

    def test_loading_a_project_is_not_evidence_of_owning_it(self):
        """A harness that opens an existing project by name must not make that
        name reclaimable — otherwise a probe touching real work marks it for
        deletion."""
        source = 'pm.LoadProject("Wedding Feature Grade")\n'
        self.assertEqual(dp.prefixes_in_source(source), set())

    def test_a_name_built_from_a_variable_yields_nothing(self):
        source = "pm.CreateProject(build_name(kind, stamp))\n"
        self.assertEqual(dp.prefixes_in_source(source), set())

    def test_an_fstring_opening_with_its_interpolation_is_rejected(self):
        """Its literal prefix is "", which would match every project there is."""
        source = 'PROJECT = f"{prefix}_probe"\n'
        self.assertEqual(dp.prefixes_in_source(source), set())

    def test_short_prefixes_are_rejected(self):
        source = 'PROJECT = f"ab{ts}"\n'
        self.assertEqual(dp.prefixes_in_source(source), set())

    def test_a_string_assigned_to_an_unrelated_name_is_not_a_prefix(self):
        """Pass 1 indexes every literal so calls can resolve one hop, but an
        unrelated constant must not become a deletion pattern on its own."""
        source = 'OUTPUT_DIR = "/home/jon/Videos/Weddings"\n'
        self.assertEqual(dp.prefixes_in_source(source), set())

    def test_unparseable_source_yields_nothing_rather_than_raising(self):
        self.assertEqual(dp.prefixes_in_source("def broken(:\n"), set())


class ClassificationTest(unittest.TestCase):
    PREFIXES = {"fx_probe_", "auto_edit_pilot_", "_mcp_resolve20_api_",
                "ZZ_fusion_bug_reverify"}

    def test_generated_suffixes_are_disposable(self):
        for name in ("fx_probe_110438", "auto_edit_pilot_224036",
                     "_mcp_resolve20_api_1785146974"):
            with self.subTest(name=name):
                self.assertTrue(dp.is_disposable(name, self.PREFIXES))

    def test_an_exact_harness_name_is_disposable(self):
        self.assertTrue(dp.is_disposable("ZZ_fusion_bug_reverify", self.PREFIXES))

    def test_a_prose_suffix_is_not_a_generated_one(self):
        """`fx_probe_keepers` is a person naming a project, not a harness
        stamping a timestamp on one."""
        self.assertFalse(dp.is_disposable("fx_probe_keepers", self.PREFIXES))
        self.assertFalse(dp.is_disposable("auto_edit_pilot_final_v2", self.PREFIXES))

    def test_unrelated_names_are_kept(self):
        for name in ("New Project 1", "Wedding Feature Grade", "debug_in_162145"):
            with self.subTest(name=name):
                self.assertFalse(dp.is_disposable(name, self.PREFIXES))

    def test_always_keep_survives_a_matching_prefix(self):
        prefixes = self.PREFIXES | {"ZZ_live_suite_scratch"}
        self.assertFalse(dp.is_disposable("ZZ_live_suite_scratch", prefixes))
        self.assertFalse(dp.is_disposable("Untitled Project",
                                          prefixes | {"Untitled Project"}))

    def test_classify_partitions_without_dropping_anything(self):
        names = ["fx_probe_110438", "New Project 1", "ZZ_live_suite_scratch"]
        split = dp.classify(names, self.PREFIXES | {"ZZ_live_suite_scratch"})
        self.assertEqual(split["disposable"], ["fx_probe_110438"])
        self.assertEqual(split["kept"], ["New Project 1", "ZZ_live_suite_scratch"])


class AgainstTheRealHarnessesTest(unittest.TestCase):
    """The derivation is only useful if it still reads this repo's harnesses."""

    @classmethod
    def setUpClass(cls):
        cls.prefixes = dp.harness_prefixes(REPO_ROOT)

    def test_the_harness_tree_yields_prefixes(self):
        self.assertGreater(len(self.prefixes), 10,
                           "no harness names resolved — the sweep would reclaim nothing")

    def test_the_leftovers_issue_155_lists_are_all_recognized(self):
        """The names in the issue body, which is the bar this had to clear."""
        for name in ("_mcp_resolve20_api_1785146974", "auto_edit_pilot_001620",
                     "montage_pilot_142741", "duck_pipe_093256", "fx_probe_110438",
                     "chanfmt_probe_110442", "pan_probe_110451",
                     "subtitle_probe_110454", "multicam_probe_1785146928"):
            with self.subTest(name=name):
                self.assertTrue(dp.is_disposable(name, self.prefixes))

    def test_the_runner_scratch_project_is_never_reclaimed(self):
        self.assertFalse(dp.is_disposable("ZZ_live_suite_scratch", self.prefixes))


if __name__ == "__main__":
    unittest.main()
