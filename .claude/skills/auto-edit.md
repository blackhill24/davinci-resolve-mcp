---
name: resolve-auto-edit
description: Autonomous brief-to-rendered-video pipeline in the DaVinci Resolve MCP — talking-head/interview, and montage (B-roll cut to music, genre="montage"). Apply when the user names source files, optional music, and the kind of video they want and expects a finished cut — "edit this interview down to 3 minutes with music and a title", or "cut this B-roll into a highlight reel set to this track". Orchestrates start_brief → analysis → plan_cut → the ONE approve_cut checkpoint → build_timeline → finish (grade/subtitles/render) — the same execution for both genres; only the planning step differs.
---

# Resolve Auto Edit — Claude Code Skill

Host orchestration for the `auto_edit` compound tool. The pipeline is
autonomous BETWEEN checkpoints, not instead of them: exactly one human
approval (`approve_cut`) sits between planning and execution.

## The loop

1. `auto_edit(action="start_brief", params={files, music?, target_duration_seconds?, title_text?})`
   — validates media, scaffolds Footage/Music bins, kicks the analysis batch.
2. Poll `brief_status(brief_id)`. While the job runs, complete any
   `commit_vision` handoffs the analysis requests (host reads frames, returns
   JSON) — deep passes feed better cut decisions.
3. `plan_cut(brief_id)` → CutList + markdown summary.
4. **Show the summary to the user verbatim.** This is the checkpoint artifact:
   runtime, segment table with excerpts, removed-cut counts, title, music line
   and the music-bed consent line.
5. Iterate with `revise_cut(brief_id, notes, edits=[{op: reorder|drop|keep|title, …}])`
   until the user is happy. Revisions are new plans; old ones stay loadable.
6. `approve_cut(plan_id, music_bed_consent=<user's explicit choice>)` — the
   confirm-token ceremony. Never assume consent for the ducked-bed render; ask.
7. `build_timeline(plan_id)` — append-rebuild; check the readback
   (`usage_summary`, `build_errors`, `punch_ins`) and report anomalies.
8. `finish(plan_id, grade?, subtitles?, render={target_dir, format?, codec?})`
   — verify the reported `output_path` exists before declaring success.

## Rules that bind this skill

- Source media is READ-ONLY. The only derivative this pipeline may create is
  the consent-gated ducked music bed, and it lands under the analysis root.
- Revisions = rebuild. Never hand-patch a built timeline; change the plan and
  rebuild (`build_timeline` on the new plan_id).
- A fingerprint-mismatched plan refuses to build — re-plan, don't override.
- Report honestly: if analysis lacked word timestamps the plan says
  `basis: cues`; tell the user detection ran coarser than usual.

## Depth

- Action boundary: `docs/kernels/auto-edit-kernel.md`
- Editorial heuristics (pacing, punch-in vs b-roll, titles, music):
  `docs/guides/editorial-decision-guide.md` → "Auto-Edit Heuristics"
- Decision layer internals: `src/utils/auto_edit.py` (talking-head),
  `src/utils/montage_edit.py` (montage — genre="montage", music required,
  no ducking), `src/utils/cut_ir.py`,
  `src/utils/music_analysis.py`
