# Orchestrate Kernel Boundary

The `orchestrate` compound tool is a resumable ingest-to-deliver post
conductor: a durable job record that sequences the existing domain tools
(`media_pool`, `media_analysis`, `auto_edit`, `timeline`,
`timeline_item_color`, `render`, `timeline_markers`) across ten stages. It
never reimplements craft — every stage delegates to the domain tool that
already owns it; `orchestrate` owns only sequencing, durable state, gates,
and recovery. Its sole justification for existing as a tool (rather than
host prose replaying the same calls) is surviving context death: a job
spans hours and multiple sessions, and losing the whole ledger on a context
reset is the failure this exists to prevent.

## Stage graph

```
intake  ingest  analysis  edit  conform  grade  audio  [fusion]  deliver  review
```

`fusion` is opt-in (`include_fusion` at `start_job`) and has no default ops
yet — it stays unimplemented until a genre needs it. Every other stage is
in the default manifest; `start_job(stages=[...])` overrides the whole
manifest explicitly.

## Actions

| Action | Notes |
|--------|-------|
| `start_job` | Validates files (exist on disk), infers the stage manifest, marks `intake` done, persists, acquires the initial lease. `gates`: `auto\|standard\|paranoid` (default `standard`). Does **not** auto-run any other stage. |
| `job_status` | Read-only; never touches the lease. |
| `list_jobs` | Reads the global index (auto-rebuilds if missing; `rebuild=true` forces a fresh scan). |
| `check_resume` | Compares a current (or explicitly-passed) fingerprint against the last done stage's recorded checkpoint. Never mutates. |
| `force_replan_stage` | Resets a drifted done stage back to pending (clears its gate) — the concrete remediation `check_resume` points at. |
| `plan_stage` / `revise_stage` | Talking-head `edit` only today: kicks/polls `auto_edit`'s `start_brief` → `plan_cut` (and `revise_cut`), recording `brief_id`/`plan_id` as foreign keys. A revision voids any G1 approval. Other stages/genres refuse honestly. |
| `approve_gate` | G1 (edit) / G2 (grade) / G3 (deliver). Fingerprint-bound — a drifted approval auto-voids. G1 on a talking-head job **adopts `auto_edit.approve_cut` verbatim** instead of minting a second confirm-token. G2 always requires `vision_assessment` + `preview_frame_path`, even under `force`. GC's the gated stage's pre-stage snapshots live on success. |
| `run_stage` | Runs the current cursor stage (or an explicit `stage`), delegating per the table below. Reversible stages snapshot before mutating; a failure leaves the stage `failed` with the snapshot intact — never auto-rolls-back. |
| `rollback_stage` | Restores the stage's latest recorded snapshot live and resets it to pending for a clean retry. |
| `finish_job` | Refuses unless every manifest stage is done; verifies the deliver stage's `output_path`, purges every remaining namespaced snapshot (count reported; `keep_snapshots` opts out), marks the job finished. |

## `run_stage` delegation per stage

| Stage | Delegate | Behavior |
|-------|----------|----------|
| `ingest` | `media_pool` (via `auto_edit`'s own bin-scaffold helpers) | Additive — imports the brief's files into Footage/Music bins. No snapshot (nothing existed before an import). |
| `analysis` | Talking-head: fused with the edit stage's own brief pipeline (a separate pass would be redundant). Other genres: `media_analysis.start_batch_job`/`batch_job_status` | Kicked/polled, never auto-driven slice-by-slice — analysis is expensive by design and was never meant to auto-run in one shot. |
| `edit` | `auto_edit` (talking-head) or a pause (every other genre) | Talking-head: plan → requires G1 valid → `build_timeline`. Other genres: the bring-your-own-timeline escape hatch — reports `waiting_on: "byo_timeline"` until the caller passes `byo_ready=true`, then just fingerprints whatever timeline now exists. |
| `conform` | `timeline.detect_gaps_overlaps` / `detect_missing_media` | Read-only QC; refuses on findings unless `accept_gaps`/`accept_missing`. Relink is left to the host driving `timeline` directly. |
| `grade` | `timeline_item_color.safe_apply_drx` / `safe_set_cdl`; `options.grade.compute` offline-computes first via `advanced_bridge.run_drx_compute` (epic #37 phase A) | No-op done unless the brief (or the call) supplies `options.grade` — G2's vision checkpoint still fires regardless. `compute` (`{action, clips, outDir, ...}`) calls the advanced server's `drx` tool in-band — Resolve stays open, no quit/relaunch — then applies the first computed grade via the same `safe_apply_drx` path; per-clip individualized application across multiple timeline items is future work (#39). |
| `audio` | `timeline.safe_set_audio_properties` | No-op done unless `options.audio` is supplied. |
| `deliver` | `render` (`prepare_render_job` → `StartRendering` → poll → verify) | **Special-cased**: requires G3 approved first, no pre-stage snapshot, no auto-rollback on failure — a failure marks `failed-resumable-via-Resolve` and leans on Resolve's own render-queue resume rather than restarting. |
| `review` | `timeline_markers.export_review_report` | — |

## Fingerprints, drift, and gates

Coarse per-stage fingerprint: `{timeline_item_count, grade_version_id,
media_path_set_hash}` — cheap live probes, never a deep content hash.
Resume only ever compares "now" against the **last done stage's** recorded
fingerprint (the frontier just behind cursor): pipeline state is
monotonic, so every earlier checkpoint is superseded by design and isn't
independently re-checkable against one current probe. A mismatch refuses
outright rather than blind-continuing.

Gates are fixed 1:1 onto stage names (`G1`→`edit`, `G2`→`grade`,
`G3`→`deliver`) and store the fingerprint they were approved against — a
drifted approval auto-voids and the gate reopens. `auto` mode
pre-authorizes a gate but still halts on drift; `force` bypasses only the
drift-halt, never G2's vision requirement. `paranoid` mode never
short-circuits an already-valid approval — it always re-prompts.

Each gate checkpoints a different POINT in its stage's lifecycle — "the
stage is done" is the right precondition for exactly one of them. G1
(post-plan, **pre**-build) and G3 (**pre**-render) fire while their stage
is still mid-flight — requiring "done" first would deadlock, since neither
stage can finish without its gate approved first. G1's real precondition is
"a plan exists" (`foreign_keys.plan_id`); G3's is "everything before
deliver in the manifest is done" (the pipeline has actually reached it).
G2 (post-grade) is the one gate that genuinely waits for its stage to
finish, since it checkpoints the *result*. G2 also gates every stage
downstream of grade (`run_stage("audio")` refuses with
`waiting_on: "G2_approval"` until it's valid) — not just deliver.

## Snapshots

Namespaced `_orch_{job_id}_{stage}`. Kind per stage: `grade_version`
(`AddVersion`/`LoadVersionByName` — cheap, in-page, non-consuming) for
`grade`; `timeline_duplicate` (`DuplicateTimeline`) for stages that mutate
the timeline structurally (`edit`, `conform`, `audio`, `fusion`). GC'd
(live deletion + record clear) when the gated stage's gate is approved;
swept job-wide by `finish_job` for anything a gate never reached. Cleanup
only ever touches tool-namespaced artifacts, never a user asset.

## Lease (crash recovery)

`{holder_id, acquired_at, heartbeat_at}` on the job record. Resuming a job
is stealing an expired lease — no special-cased recovery path. A live
lease held by a different holder refuses (documented posture: one active
job per project). Read-only actions (`job_status`, `list_jobs`,
`check_resume`) never touch it.

## Evidence & persistence

Two-file persistence, record = truth: `{analysis_root}/memory/jobs/{job_id}.json`,
content-fingerprinted like `edit_engine` plans — a tampered record refuses
to load, never silently proceeds. Global index (rebuildable cache) at
`{analysis_base_root}/_jobs/index.json`, mirroring the `_soul` convention;
written after the record, never before, and self-heals via
`list_jobs(rebuild=true)`.

## Host / tool firewall

The tool owns all sequencing, drift checks, and gate ceremonies. The host
(skill/prose) owns only conversation, the G2 vision look, and consent
capture — if a step needs to *compute* anything, that belongs in the tool.

## Offline tests / live validation

Offline: `tests/test_orchestrate.py`, `tests/test_orchestrate_tool.py`,
`tests/test_orchestrate_gates.py`, `tests/test_orchestrate_gates_tool.py`,
`tests/test_orchestrate_run_stage.py`, `tests/test_orchestrate_run_stage_tool.py`
(state machine, persistence, lease, fingerprints, drift-refuse, snapshot
bookkeeping/GC, the gate matrix, and every `run_stage` delegation path with
the domain-tool calls mocked — this suite verifies orchestrate's own logic,
not the domain tools' internals). Live: `tests/live_orchestrate_probe.py`
(requires Resolve Studio; gated by `tests/preflight.py`).

**Live-verified on Resolve Studio 21.0.2.4** (18/20 checks): a talking-head
brief runs end-to-end — `start_job` → `ingest` → `analysis` (fused brief
pipeline) → G1 (adopts `auto_edit.approve_cut`) → `edit` (`build_timeline`)
→ `conform` → a forced grade failure + `rollback_stage` + clean no-op retry
→ G2 (real extracted-frame look) → `audio` → G3 → `deliver` (validated
render, output verified via ffprobe) → `review` → `finish_job` (output
re-verified). The live probe's own bugs caught two real design mistakes
before this: G1/G3's precondition originally required their gated stage to
already be `done`, which deadlocks a pre-execution gate (fixed —
`_gate_precondition_ok`, see Gates above), and G2 was never actually wired
as a precondition anywhere downstream (fixed — `run_stage("audio")` now
requires it). **Remaining gap:** the `grade_version` pre-stage snapshot
(`AddVersion`) didn't take on the synthetic pilot's timeline item, so the
forced-failure/rollback check ran with no snapshot to restore (best-effort
degrades correctly — the failure and clean retry both still worked; only
the snapshot-COVERED-failure path is unverified). The
bring-your-own-timeline (non-talking-head) path is offline-tested only.
