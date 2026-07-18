# tooling — Context (ICM Layer 2)

Installer and maintenance scripts: audits, generators, probes. The generators own several
"generated" docs — edit the script, never its output. (`install.py` lives at repo root.)

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Change install behavior | `../install.py` | this dir | `docs/install.md` |
| Regenerate agent-rule mirrors (AGENTS/.cursor/…) | `agent-rules/generate.mjs`, `agent-rules/README.md` | `../.cursorrules` etc. (outputs) | — |
| Regenerate API limitations doc | `gen_api_limitations.py` | — | `src/utils/api_truth.py` (source) |
| Audit API parity / read-write symmetry | `audit_api_parity.py`, `audit_readwrite_symmetry.py` | — | `docs/reference/` |
| Diagnose environment | `doctor.py` | — | — |
| Measure bridge cost | `measure_bridge_cost.py` | — | `tests/benchmark_server.py` |

## Key files (only where the name doesn't say enough)

- `agent-rules/generate.mjs` — single generator for the `BEGIN GENERATED` blocks and the
  `.cursorrules`/`.clinerules`/`.windsurfrules`/`.roo` mirrors; those files are outputs.
- `regen_panel_screenshots.py` — regenerates `docs/images/` control-panel screenshots.

## Conventions & gotchas

- Several outputs are drift-guarded (e.g. api-limitations regeneration is enforced) — after
  editing a generator source, run the generator so the checked-in output matches.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
