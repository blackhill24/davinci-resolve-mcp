---
name: resolve-orchestrate
description: Resumable ingest-to-deliver post-production conductor in the DaVinci Resolve MCP. Apply when a task spans multiple domains (edit + grade + audio + deliver, or a full ingest→deliver pass) AND needs to survive a context reset across sessions. For a single-domain task, or a talking-head edit that fits in one session, use the matching domain skill (resolve-auto-edit, resolve-color, ...) directly instead.
---

# Resolve Orchestrate — Claude Code Skill

Host orchestration for the `orchestrate` compound tool: a durable job record
that sequences the domain tools across ten stages (intake → ingest → analysis
→ edit → conform → grade → audio → [fusion] → deliver → review) and survives
context death. The tool owns all state and decision logic; this skill is a
thin driver — if a step here has to *compute* anything, that belongs in the
tool, not here.

**Phase 1 (current):** only `start_job`, `job_status`, and `list_jobs` exist.
`run_stage`, the three approval gates, and rollback land in later phases —
until then a job can be created and inspected, but stages after `intake`
stay `pending`. Don't promise stage execution yet.

## The loop (Phase 1)

1. `orchestrate(action="start_job", params={files, music?, target_duration_seconds?,
   genre?, deliverable?, title_text?, options?, stages?, include_fusion?})`
   — validates the files, infers the stage manifest (or honors an explicit
   `stages` override), marks `intake` done, persists the job, and acquires
   the lease. Returns `{job_id, job, holder_id}`.
2. `job_status(job_id)` to check `cursor` (the next non-done stage) and
   `manifest`. Read-only — safe to call anytime, never touches the lease.
3. `list_jobs()` to resume across sessions — reads the global index (auto-
   rebuilds if missing); pass `rebuild=true` after any out-of-band record
   change.

## Rules that bind this skill

- **Resumability is the point.** Never re-derive a job's state from
  conversation memory — always re-read `job_status` after a context gap.
- **Zero decision logic in prose.** Stage sequencing, drift checks, and gate
  ceremonies belong in the tool. This skill only relays what the tool
  returns and captures user consent at gates (once gates exist).
- Source media stays read-only through every stage, same posture as
  `resolve-auto-edit`.

## Depth

- Design + phased build plan: GitHub epic (`orchestrate`), locked design
  decisions.
- Core module: `src/utils/orchestrate.py` (state machine + two-file
  persistence + lease). Tool wiring: `src/server.py` (`orchestrate`).
- Kernel doc (`docs/kernels/orchestration-kernel.md`) lands in a later phase
  alongside `run_stage`/gates.
