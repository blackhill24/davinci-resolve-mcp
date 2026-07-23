"""Drift guard for the `.claude/skills/` surface (offline, static, dependency-light —
same posture as `tests/test_doc_tool_counts.py`, which is why this can run in the
publish gate).

Asserts:
  - every `.claude/skills/` entry is a directory containing `SKILL.md`; no stray flat `.md`
  - each `SKILL.md` has non-empty `name:` / `description:` frontmatter, and `name` ==
    the directory name
  - every `docs/...` / `resolve-advanced/...` path a skill cites exists on disk
  - reverse direction: no file in the repo cites a `.claude/skills/...` path that
    does not exist on disk (this is the assertion that would have caught the 22
    stale citations from the flat-file era)
  - every `DOMAINS[].skill` in `scripts/agent-rules/generate.mjs` has a matching
    skill directory
  - all three indexes — the `resolve-mcp` router skill, `docs/README.md`, and
    `docs/kernels/README.md` — mention every skill directory by name

Historical records are exempt from the reverse-citation check: they describe the
repo layout as it was at the time and are not supposed to track renames.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / ".claude" / "skills"

HISTORICAL_EXEMPT = {
    ROOT / "docs" / "decisions" / "0001-domain-taxonomy.md",
    ROOT / "CHANGELOG.md",
}

# Files/dirs to scan for citations, in both directions. Keep this dependency-light:
# walk the repo but skip heavy/irrelevant trees.
SCAN_EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "__pycache__", ".icm", ".claude/worktrees",
}


def _iter_scannable_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SCAN_EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix not in {".md", ".py", ".mjs", ".json"}:
            continue
        yield path


def _parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    fields = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _skill_dirs():
    return sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())


class ClaudeSkillsLayoutTest(unittest.TestCase):
    def test_only_directories_with_skill_md(self):
        stray_files = [p.name for p in SKILLS_DIR.iterdir() if p.is_file()]
        self.assertEqual(
            stray_files, [],
            f"stray flat files directly under .claude/skills/ (must be <name>/SKILL.md): {stray_files}",
        )

        missing_skill_md = [p.name for p in _skill_dirs() if not (p / "SKILL.md").is_file()]
        self.assertEqual(
            missing_skill_md, [],
            f"skill directories missing SKILL.md: {missing_skill_md}",
        )

    def test_frontmatter_name_and_description(self):
        problems = []
        for skill_dir in _skill_dirs():
            text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            fm = _parse_frontmatter(text)
            name = fm.get("name", "")
            description = fm.get("description", "")
            if not name:
                problems.append(f"{skill_dir.name}/SKILL.md: empty or missing `name:`")
            elif name != skill_dir.name:
                problems.append(
                    f"{skill_dir.name}/SKILL.md: name {name!r} != directory name {skill_dir.name!r}"
                )
            if not description:
                problems.append(f"{skill_dir.name}/SKILL.md: empty or missing `description:`")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_cited_paths_exist(self):
        # A skill may cite docs/... or resolve-advanced/... paths as pointers to depth.
        pattern = re.compile(r"\b(docs/[A-Za-z0-9_\-./]+\.md|resolve-advanced/[A-Za-z0-9_\-./]+)")
        problems = []
        for skill_dir in _skill_dirs():
            text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            for cited in set(m.group(1) for m in pattern.finditer(text)):
                if not (ROOT / cited).exists():
                    problems.append(f"{skill_dir.name}/SKILL.md cites missing path: {cited}")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_no_dangling_flat_skill_citations(self):
        # Reverse direction: nothing in the repo may cite a .claude/skills/<name>.md
        # flat path (pre-migration shape), nor a resolve-*/SKILL.md path that doesn't exist.
        flat_pattern = re.compile(r"\.claude/skills/([A-Za-z0-9_\-]+)\.md\b")
        dir_pattern = re.compile(r"\.claude/skills/([A-Za-z0-9_\-]+)/SKILL\.md\b")
        problems = []
        for path in _iter_scannable_files():
            if path in HISTORICAL_EXEMPT:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in flat_pattern.finditer(text):
                problems.append(f"{path.relative_to(ROOT)}: stale flat citation .claude/skills/{m.group(1)}.md")
            for m in dir_pattern.finditer(text):
                cited_dir = SKILLS_DIR / m.group(1)
                if not (cited_dir / "SKILL.md").is_file():
                    problems.append(
                        f"{path.relative_to(ROOT)}: cites .claude/skills/{m.group(1)}/SKILL.md, "
                        "which does not exist"
                    )
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_domains_have_matching_skill_dirs(self):
        generate_mjs = (ROOT / "scripts" / "agent-rules" / "generate.mjs").read_text(encoding="utf-8")
        domain_skills = re.findall(r"skill:\s*'([^']+)'", generate_mjs)
        self.assertTrue(domain_skills, "could not find any DOMAINS[].skill entries in generate.mjs")

        existing = {p.name for p in _skill_dirs()}
        missing = [s for s in domain_skills if s not in existing]
        self.assertEqual(
            missing, [],
            f"DOMAINS[].skill entries with no matching .claude/skills/<name>/ directory: {missing}",
        )

    def test_three_indexes_list_every_skill(self):
        router_text = (SKILLS_DIR / "resolve-mcp" / "SKILL.md").read_text(encoding="utf-8")
        readme_text = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        kernels_readme_text = (ROOT / "docs" / "kernels" / "README.md").read_text(encoding="utf-8")

        indexes = {
            "resolve-mcp router (.claude/skills/resolve-mcp/SKILL.md)": router_text,
            "docs/README.md": readme_text,
            "docs/kernels/README.md": kernels_readme_text,
        }

        problems = []
        for skill_dir in _skill_dirs():
            name = skill_dir.name
            for index_label, text in indexes.items():
                if name not in text:
                    problems.append(f"{index_label} does not mention skill {name!r}")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
