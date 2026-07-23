---
name: resolve-orchestration
description: Resumable ingest-to-deliver post-production conductor in the DaVinci Resolve MCP. Apply when a task spans multiple domains (edit AND grade AND deliver, "take this footage all the way to delivery") AND needs to survive a context reset across sessions. For a single-domain task, or a talking-head edit that fits in one session, use the matching domain skill (resolve-auto-edit, resolve-color-grade, ...) directly instead.
---

# Resolve Orchestrate — Claude Code Skill

Host driver for the `orchestrate` compound tool: a durable job record that
sequences the domain tools across ten stages (intake → ingest → analysis →
edit → conform → grade → audio → [fusion] → deliver → review) and survives
context death. The tool owns all state, drift checks, and gate ceremonies —
if a step here has to *compute* anything, that belongs in the tool, not here.

## The loop

1. `start_job(files, music?, target_duration_seconds?, genre?, deliverable?,
   options?, stages?, include_fusion?, gates?)` → `{job_id}`. `gates`:
   `auto|standard|paranoid` (default `standard`).
2. `job_status(job_id)` — read `cursor`; safe anytime, never touches the lease.
3. `run_stage(job_id)` (defaults to cursor) drives one stage: it delegates,
   snapshots reversible stages first, and reports `waiting_on` rather than
   erroring when it needs something from you:
   - **Auto-assembling genres (talking-head, montage)**: kicks/polls the
     brief+plan (genre-dispatched inside `auto_edit`); on
     `waiting_on: "G1_approval"` show the cut summary (`plan_id`), iterate
     with `revise_stage(job_id, notes, edits)` if needed, then
     `approve_gate(job_id, gate="G1", ...)` — **adopts `auto_edit.approve_cut`
     verbatim**, not a second checkpoint. Montage requires `music` in the
     brief (its length sets the runtime) and has no voiceover/ducking.
   - **Any other genre**: `waiting_on: "byo_timeline"` — cut it in Resolve,
     then `run_stage(job_id, stage="edit", byo_ready=true)`.
   - `grade`/`audio` no-op unless the brief (or the call) supplies
     `options.grade`/`options.audio`.
   - `deliver` requires **G3** approved (`waiting_on: "G3_approval"`
     otherwise); special-cased — no snapshot, no auto-rollback. A failure
     marks `failed-resumable-via-Resolve`; lean on Resolve's own render-queue
     resume rather than restarting.
4. `approve_gate(job_id, gate="G2", vision_assessment, preview_frame_path)`
   for the post-grade checkpoint. **Always a real look at the rendered
   frame** — never blind, never fabricated.
5. A failed reversible stage does **not** auto-rollback: call
   `rollback_stage(job_id, stage)`, then `run_stage` again for a clean retry.
5b. A narrow slice of the advanced server needs the Resolve project CLOSED
   (`conform.fix_reverse_clip`, `offline_ref` LIVE DB link/unlink,
   `project_db.relayout_node_graphs`, `fairlight`'s DB path) — call
   `request_offline_op(job_id, stage, tool, op_action, args)` to park the
   current stage instead of hand-rolling it. This never quits or relaunches
   Resolve itself: **you** do that via `quit_app` → the advanced tool call →
   `launch`, each its own explicit, permissioned step — a real Resolve
   process being killed/relaunched programmatically is meaningfully riskier
   than any other bridge call here, so confirm with the user before the
   first live run. Report the result to `resolve_offline_op(job_id, result)`
   to resume, then `run_stage` again.
6. After a gap, `check_resume(job_id)` before trusting `cursor` — on
   `drifted: true`, call `force_replan_stage(job_id, stage)` rather than
   push forward past a checkpoint that no longer holds.
7. Once every stage is done, `finish_job(job_id)` — verifies `output_path`,
   purges remaining namespaced snapshots, marks the job finished.
8. `list_jobs()` to resume across sessions (auto-rebuilds the index if
   missing).

## Rules that bind this skill

- **Resumability is the point.** Never re-derive job state from
  conversation memory — re-read `job_status`/`check_resume` after any gap.
- **Zero decision logic in prose.** Sequencing, drift, and gates live in the
  tool; this skill only relays results and captures consent at gates.
- **G2 is a real look, not a formality** — read the frame at
  `preview_frame_path`, describe what's actually there.
- Source media stays read-only through every stage, same posture as
  `resolve-auto-edit`.

## Depth

Action boundary + stage-graph/fingerprint/gate/snapshot/lease depth:
`docs/kernels/orchestration-kernel.md`. Core module:
`src/utils/orchestrate.py`. Tool wiring: `src/server.py` (`orchestrate`).
Any-MCP-client prompt: `/orchestrate_workflow`.
