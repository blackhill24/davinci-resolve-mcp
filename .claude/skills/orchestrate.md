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

**Phase 3 (current):** `run_stage` delegates every stage to its domain tool
(craft is never reimplemented), plus `rollback_stage` and `finish_job`.
`fusion` stays unimplemented (opt-in, no default ops yet).

## The loop

1. `orchestrate(action="start_job", params={files, music?, target_duration_seconds?,
   genre?, deliverable?, title_text?, options?, stages?, include_fusion?, gates?})`
   — validates the files, infers the stage manifest (or honors an explicit
   `stages` override), marks `intake` done, persists the job, and acquires
   the lease. `gates`: `auto|standard|paranoid` (default `standard`). Returns
   `{job_id, job, holder_id}`.
2. `job_status(job_id)` to check `cursor` (the next non-done stage) and
   `manifest`. Read-only — safe to call anytime, never touches the lease.
3. Drive the pipeline with `run_stage(job_id)` (defaults to the cursor
   stage) — it delegates, snapshots reversible stages first, and reports
   `waiting_on` when a stage needs something from you rather than treating
   that as an error:
   - `ingest`/`analysis`(non-talking-head)/`conform` run to completion (or
     report a QC refusal — pass `accept_gaps`/`accept_missing` to proceed
     anyway).
   - **Talking-head edit**: `run_stage` kicks/polls the brief and plan for
     you — when it reports `waiting_on: "G1_approval"`, show the cut summary
     (`plan_id` in the response) and call `approve_gate(job_id, gate="G1",
     vision_assessment?, ...)`, which **adopts `auto_edit.approve_cut`
     verbatim** (its confirm-token ceremony, not a second one). Iterate first
     with `revise_stage(job_id, notes, edits)` if the user wants changes —
     each revision voids any G1 approval (expected, not a bug).
   - **Any other genre**: `run_stage("edit")` reports `waiting_on:
     "byo_timeline"` — cut it in Resolve, then call `run_stage(job_id,
     stage="edit", byo_ready=true)`, which fingerprints whatever timeline
     now exists and marks the stage done.
   - `grade`/`audio` are no-ops unless the brief (or the call) supplies
     `options.grade`/`options.audio` — pass `grade={drx_path|cdl, ...}` or
     `audio={...}` to `run_stage` to actually apply something.
   - `deliver` requires **G3** approved first (`run_stage` reports
     `waiting_on: "G3_approval"` otherwise); it's special-cased — no
     snapshot, no auto-rollback. A failure marks
     `failed-resumable-via-Resolve`; lean on Resolve's own render-queue
     resume rather than restarting from scratch.
4. `approve_gate(job_id, gate="G2", vision_assessment, preview_frame_path)`
   for the post-grade checkpoint — **G2 always needs a host-supplied look
   assessment of a rendered frame.** Never approve it blind, never fabricate
   an assessment.
5. A reversible stage that fails (`run_stage` returns `success: false`,
   stage status `failed`) does **not** auto-rollback — call
   `rollback_stage(job_id, stage)` to restore the pre-mutation snapshot and
   reset the stage to pending, then `run_stage` again for a clean retry.
6. Resuming after a gap: `check_resume(job_id)` before trusting `cursor` —
   if it reports `drifted: true`, call `force_replan_stage(job_id, stage)`
   rather than pushing forward past a checkpoint that no longer holds.
7. Once every manifest stage is done: `finish_job(job_id)` — verifies the
   `output_path`, purges every remaining namespaced snapshot (reports the
   count), and marks the job finished. `keep_snapshots=true` opts out.
8. `list_jobs()` to resume across sessions — reads the global index (auto-
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
  persistence + lease + fingerprints/gates/snapshots). Tool wiring:
  `src/server.py` (`orchestrate`).
- Kernel doc (`docs/kernels/orchestration-kernel.md`) lands in a later phase.
