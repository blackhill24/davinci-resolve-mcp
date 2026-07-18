# docs — Context (ICM Layer 2)

Durable reference for the project: per-domain depth (kernels), decision guides, API
reference, authoring formats, and release process. Route here; don't duplicate into code.

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Get per-domain action depth | `kernels/<domain>-kernel.md` (index: `kernels/README.md`) | `authoring/` | matching `.claude/skills/` |
| Make a color/edit decision | `guides/color-decision-guide.md`, `guides/editorial-decision-guide.md` | `kernels/` | — |
| Auto-edit pipeline depth | `kernels/auto-edit-kernel.md`, `guides/editorial-decision-guide.md` (Auto-Edit Heuristics) | `authoring/` | `.claude/skills/auto-edit.md` |
| Source-safe media analysis | `guides/media-analysis-guide.md` | `reference/` | — |
| Check API coverage / limitations | `reference/api-coverage.md`, `reference/api-limitations.md` | `authoring/` | `scripts/gen_api_limitations.py` |
| Author Fuse/DCTL/settings files | `authoring/` (`fuse-dctl-authoring.md`, `setting-files/`) | `kernels/` | — |
| Release / version bump | `process/release-process.md` | everything else | — |

## Key files (only where the name doesn't say enough)

- `SKILL.md` — top-level operating guide for the MCP (start here for behavior).
- `reference/api-limitations.md` — **generated** from `src/utils/api_truth.py`; edit the
  source + regenerate, never the file.
- `reference/resolve_scripting_api.txt` — bundled Blackmagic API text (large; grep, don't read).

## Conventions & gotchas

- Keep README concise; durable detail belongs under `docs/`.
- `images/` holds screenshots referenced by guides (regenerated via
  `scripts/regen_panel_screenshots.py`).

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
