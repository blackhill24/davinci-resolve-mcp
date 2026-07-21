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

**Phase 2 (current):** job lifecycle + fingerprints/drift-refuse + gates exist.
`run_stage` (full delegation to grade/audio/render) and `rollback_stage` land
in a later phase — until then only the `edit` stage has a planner (talking-head,
via `auto_edit`); every other stage stays `pending` until run_stage ships.

## The loop

1. `orchestrate(action="start_job", params={files, music?, target_duration_seconds?,
   genre?, deliverable?, title_text?, options?, stages?, include_fusion?, gates?})`
   — validates the files, infers the stage manifest (or honors an explicit
   `stages` override), marks `intake` done, persists the job, and acquires
   the lease. `gates`: `auto|standard|paranoid` (default `standard`). Returns
   `{job_id, job, holder_id}`.
2. `job_status(job_id)` to check `cursor` (the next non-done stage) and
   `manifest`. Read-only — safe to call anytime, never touches the lease.
3. Talking-head edit stage: `plan_stage(job_id)` (kicks/polls analysis, then
   `plan_cut`) → show the returned cut summary → `revise_stage(job_id, notes,
   edits)` to iterate (each revision voids any G1 approval — expected, not a
   bug) → `approve_gate(job_id, gate="G1", vision_assessment?, ...)`, which
   **adopts `auto_edit.approve_cut` verbatim** (its confirm-token ceremony,
   not a second one). Any other genre gets a bring-your-own-timeline refusal
   from `plan_stage` — cut it in Resolve; full handoff is a later phase.
4. `approve_gate(job_id, gate="G2", vision_assessment, preview_frame_path)`
   for the post-grade checkpoint — **G2 always needs a host-supplied look
   assessment of a rendered frame.** Never approve it blind, never fabricate
   an assessment. `approve_gate(job_id, gate="G3", ...)` gates pre-render.
5. Resuming after a gap: `check_resume(job_id)` before trusting `cursor` —
   if it reports `drifted: true`, call `force_replan_stage(job_id, stage)`
   rather than pushing forward past a checkpoint that no longer holds.
6. `list_jobs()` to resume across sessions — reads the global index (auto-
   rebuilds if missing); pass `rebuild=true` after any out-of-band record
   change.

## Rules that bind this skill

- **Resumability is the point.** Never re-derive a job's state from
  conversation memory — always re-read `job_status` (and `check_resume`
  after any gap) rather than assuming the last-known cursor still holds.
- **Zero decision logic in prose.** Stage sequencing, drift checks, and gate
  ceremonies belong in the tool. This skill only relays what the tool
  returns and captures user consent at gates.
- **G2 is a real look, not a formality.** Never call `approve_gate(gate="G2")`
  with a fabricated or generic `vision_assessment` — read the frame at
  `preview_frame_path`, then describe what's actually there.
- Source media stays read-only through every stage, same posture as
  `resolve-auto-edit`.

## Depth

- Design + phased build plan: GitHub epic (`orchestrate`), locked design
  decisions.
- Core module: `src/utils/orchestrate.py` (state machine + two-file
  persistence + lease). Tool wiring: `src/server.py` (`orchestrate`).
- Kernel doc (`docs/kernels/orchestration-kernel.md`) lands in a later phase
  alongside `run_stage`/gates.
