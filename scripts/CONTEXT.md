# tooling — Context (ICM Layer 2)

Installer and maintenance scripts: audits, generators, probes. The generators own several
"generated" docs — edit the script, never its output. (`install.py` lives at repo root.)

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Change install behavior | `../install.py` | this dir | `docs/install.md` |
| Regenerate agent-rule mirrors (AGENTS/.cursor/…) | `agent-rules/generate.mjs`, `agent-rules/README.md` | `../.cursorrules` etc. (outputs) | — |
| Regenerate API limitations doc | `gen_api_limitations.py` | — | `src/core/api_truth.py` (source) |
| Audit API parity / read-write symmetry | `audit_api_parity.py`, `audit_readwrite_symmetry.py` | — | `docs/reference/` |
| Check/raise a per-module coverage floor | `coverage_floor.py`, `../.coveragerc` | — | ratchets a NAMED module list, never a repo average |
| Run the live Resolve suite | `run_live_suite.py` | individual `tests/**/live_*.py` | `docs/process/release-process.md` |
| Reclaim leftover probe/pilot projects | `run_live_suite.py --clean-disposable`, `disposable_projects.py` | — | issue #155 |
| Investigate Resolve exiting by itself | `resolve_vitals.py`, `run_live_suite.py --vitals` | — | issue #153 |
| Diagnose environment | `doctor.py` | — | — |
| Measure bridge cost | `measure_bridge_cost.py` | — | — |

## Key files (only where the name doesn't say enough)

- `agent-rules/generate.mjs` — single generator for the `BEGIN GENERATED` blocks and the
  `.cursorrules`/`.clinerules`/`.windsurfrules`/`.roo` mirrors; those files are outputs.
- `regen_panel_screenshots.py` — regenerates `docs/images/` control-panel screenshots.
- `run_live_suite.py` — the live-suite runner: env, per-harness isolation (scratch project +
  timeline), leak diffing, and the preflight exit-code contract. It partitions cold-launch
  harnesses by reading `gate("closed")` out of their source, so a new one needs no list edit.
- `disposable_projects.py` — decides which projects a bulk delete may take. The prefixes are
  AST-derived from `tests/**/live_*.py`, never hand-listed, so a new harness needs no edit
  here and a name no harness generates is always kept (#155). Only project *creation*
  counts as evidence; `.disposable-keep` (repo root, gitignored) overrides it by name.
- `resolve_vitals.py` — reads Resolve's `/proc` vitals (RSS, fds vs the process's own soft
  limit, threads, descendants, GPU MiB, env slice). Pure `/proc` + `nvidia-smi`, never the
  scripting API, so it survives the exit it exists to describe. `--watch` samples an idle
  Resolve; `run_live_suite.py --vitals` samples between harnesses (#153).

## Conventions & gotchas

- Several outputs are drift-guarded (e.g. api-limitations regeneration is enforced) — after
  editing a generator source, run the generator so the checked-in output matches.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
