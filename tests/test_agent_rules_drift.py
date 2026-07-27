"""Drift guard for cross-platform agent rule files.

The per-IDE rule files (.cursor/rules/*, .github/instructions/*, .windsurf/rules/*,
.cursorrules, .windsurfrules, the AGENTS.md domain-routing block, and
.github/copilot-instructions.md) are GENERATED from one manifest by
scripts/agent-rules/generate.mjs, which also parses tool/action counts from their
canonical docs. This test fails if any generated file is stale — i.e. someone
edited a generated file by hand, or a canonical count changed without regenerating.

Fix a failure with:  node scripts/agent-rules/generate.mjs
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / "scripts" / "agent-rules" / "generate.mjs"


class AgentRulesDriftTest(unittest.TestCase):
    def test_generator_exists(self):
        self.assertTrue(GENERATOR.is_file(), f"missing generator: {GENERATOR}")

    def test_generated_files_are_in_sync(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not on PATH; cannot verify generated agent-rule files")
        proc = subprocess.run(
            [node, str(GENERATOR), "--check"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=(
                "Agent-rule files are stale. Regenerate with "
                "`node scripts/agent-rules/generate.mjs`.\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            ),
        )

    def test_domain_prompts_registered_in_server(self):
        # The dynamic, every-MCP-client layer: each domain must have a slash prompt.
        server = (REPO / "src" / "server.py").read_text(encoding="utf-8")
        for name in (
            "color_grade_workflow",
            "timeline_edit_workflow",
            "conform_workflow",
            "delivery_workflow",
            "extension_authoring_workflow",
            "project_lifecycle_workflow",
            "review_annotation_workflow",
            "server_ops_workflow",
        ):
            self.assertIn(
                f'name="{name}"',
                server,
                msg=f"missing @mcp.prompt {name} in src/server.py",
            )


class GeneratorCheckIsNotVacuousTest(unittest.TestCase):
    """`generate.mjs --check` is verified to actually detect a stale output.

    `test_generated_files_are_in_sync` asserts an exit code of 0. That proves
    nothing on its own: a generator whose `--check` path stopped comparing, or
    that silently caught its own error, would also exit 0 forever (#121 task 2).

    The check runs in a throwaway tree — a copy of the generator plus the three
    docs it parses counts from — so nothing here can write into the real repo.
    The tree is seeded by running the generator once without `--check`, which
    means the sync state under test is built the same way the repo's is.
    """

    SOURCE_DOCS = (
        "docs/SKILL.md",
        "docs/kernels/README.md",
        "resolve-advanced/README.md",
    )

    def _sandbox(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "scripts" / "agent-rules").mkdir(parents=True)
        shutil.copy2(GENERATOR, root / "scripts" / "agent-rules" / "generate.mjs")
        for rel in self.SOURCE_DOCS:
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        return root

    def _run(self, root, *args):
        return subprocess.run(
            [shutil.which("node"), str(root / "scripts" / "agent-rules" / "generate.mjs"), *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node not on PATH")

    def test_check_passes_immediately_after_a_generate(self):
        root = self._sandbox()
        generated = self._run(root)
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        checked = self._run(root, "--check")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_check_fails_when_a_generated_file_is_hand_edited(self):
        root = self._sandbox()
        self._run(root)
        target = root / ".cursorrules"
        self.assertTrue(target.is_file(), "generator did not produce .cursorrules")
        target.write_text(
            target.read_text(encoding="utf-8") + "\nhand-edited line\n", encoding="utf-8"
        )
        result = self._run(root, "--check")
        self.assertNotEqual(
            result.returncode, 0,
            "generate.mjs --check did not notice a hand-edited generated file:\n"
            f"{result.stdout}\n{result.stderr}",
        )

    def test_check_fails_when_a_generated_file_is_deleted(self):
        root = self._sandbox()
        self._run(root)
        (root / ".cursorrules").unlink()
        result = self._run(root, "--check")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_fails_when_the_generated_block_is_edited_in_place(self):
        # AGENTS.md is emitted as a BEGIN/END block inside a hand-written file —
        # a different code path (emitBlock) from the whole-file outputs above.
        root = self._sandbox()
        self._run(root)
        agents = root / "AGENTS.md"
        self.assertTrue(agents.is_file())
        text = agents.read_text(encoding="utf-8")
        agents.write_text(
            text.replace("## Domain Routing", "## Domain Routing (edited)"), encoding="utf-8"
        )
        result = self._run(root, "--check")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
