---
name: resolve-auto-edit
description: Autonomous brief-to-rendered-video pipeline in the DaVinci Resolve MCP — talking-head/interview, and montage (B-roll cut to music, genre="montage"). Apply when the user names source files, optional music, and the kind of video they want and expects a finished cut — "edit this interview down to 3 minutes with music and a title", or "cut this B-roll into a highlight reel set to this track". Orchestrates start_brief → analysis → plan_cut → the ONE approve_cut checkpoint → build_timeline → finish (grade/subtitles/render) — the same execution for both genres; only the planning step differs.
---

# Resolve Auto Edit — Claude Code Skill

Host orchestration for the `auto_edit` compound tool. The pipeline is
autonomous BETWEEN checkpoints, not instead of them: exactly one human
approval (`approve_cut`) sits between planning and execution.

## The loop

1. `auto_edit(action="start_brief", params={files, music?, genre?, deliverable?,
   target_duration_seconds?, title_text?})` — validates media, scaffolds
   Footage/Music bins, kicks the analysis batch.
   **`genre` selects the decision layer**: `"talking_head"` (default) or
   `"montage"`. Montage additionally *requires* `music` — its length sets the
   runtime, so `target_duration_seconds` is not the driver there.
   `deliverable` defaults to `"youtube_1080p"`.
2. Poll `brief_status(brief_id)`. While the job runs, complete any
   `commit_vision` handoffs the analysis requests (host reads frames, returns
   JSON) — deep passes feed better cut decisions.
3. `plan_cut(brief_id)` → CutList + markdown summary.
4. **Show the summary to the user verbatim.** This is the checkpoint artifact.
   Talking-head: runtime, segment table with transcript excerpts, removed-cut
   counts, title, music line and the music-bed consent line. Montage renders a
   different table (`render_montage_summary`) — role / description / pacing
   columns, and no consent line. Relay whichever you actually got; don't
   describe columns that aren't there.
   `get_cut_summary(plan_id, format="markdown"|"json")` re-reads a saved plan's
   summary if you lost it.
5. Iterate with `revise_cut(brief_id, notes, edits=[{op: reorder|drop|keep|title, …}])`
   until the user is happy. Revisions are new plans; old ones stay loadable.
6. `approve_cut(plan_id, music_bed_consent=<user's explicit choice>)` — the
   confirm-token ceremony. Never assume consent for the ducked-bed render; ask.
   **Talking-head only** — for a montage plan `approve_cut` forces the static
   bed regardless of what consent flags you pass (no voiceover to duck under),
   so don't put a meaningless consent question to the user.
7. `build_timeline(plan_id)` — append-rebuild; check the readback
   (`usage_summary`, `build_errors`, `punch_ins`) and report anomalies.
7b. *(optional)* `polish_timeline(plan_id, options?)` — the pro polish the
   scripting API cannot do: exports the built timeline to `.drt`, runs the
   verified `drp-format` vendor ops on it in scratch (cross-dissolves at
   flagged cuts, Fusion lower-thirds on an upper track) and reimports a NEW
   `(polished)` timeline — the built one is left intact. Genre-agnostic: it
   operates on the built timeline, so it works on montage plans too. `options`:
   `lower_thirds[]`, `dissolve_at_segments[]`, `dissolve_on_beat_change`,
   `dissolve_frames`, `lower_third_frames`/`_track`, `no_dissolves`,
   `no_lower_thirds`. **`finish` still targets the BUILT timeline**, not the
   polished one — so polish is a review/hand-off artifact, and if the user wants
   the polished cut rendered they render it themselves (or you drive
   `render` against it). Check `clips_relinked`; a `warning` means real source
   clips went offline in the `.drt` round-trip.
8. `finish(plan_id, grade?, subtitles?, render={target_dir, format?, codec?})`
   — verify the reported `output_path` exists before declaring success.
   On Linux, `subtitles` hits a `SUBTITLE_GENERATION_CRASH_GUARD` refusal
   (issue #90: `CreateSubtitlesFromAudio` kills the Resolve process, no
   exception). Skip `subtitles` on the `finish` call and instead import an
   offline-generated `.srt` via `timeline(action="import_srt")` — live-proven,
   does not call the crashing API.

`list_briefs()` recovers saved briefs (newest first) after a context reset.

## Rules that bind this skill

- Source media is READ-ONLY. The only derivative this pipeline may create is
  the consent-gated ducked music bed, and it lands under the analysis root.
- Revisions = rebuild. Never hand-patch a built timeline; change the plan and
  rebuild (`build_timeline` on the new plan_id).
- A fingerprint-mismatched plan refuses to build — re-plan, don't override.
- Report honestly: if analysis lacked word timestamps the plan says
  `basis: cues`; tell the user detection ran coarser than usual.

## Granular (`--full`) + prompt/resource

`auto_edit` is kernel-only — no one-per-method granular twin; the decision
layer above is the sole implementation, live-only (no `resolve-advanced`
counterpart — `cut_ir` is an internal module, not a Node offline tool).
**Prompt** — `auto_edit_workflow` (`src/server.py`) starts the loop above.
**Resources** — `status://current_project`, `analysis://recent_reports`
(the analysis batch's output), `capabilities://installed_tools`.

## Depth

- Action boundary: `docs/kernels/auto-edit-kernel.md` (its "Montage genre"
  section covers the genre split)
- Editorial heuristics — **pick the section that matches the genre**, they do
  not overlap:
  - talking-head (pacing, punch-in vs b-roll, titles, music levels):
    `docs/guides/editorial-decision-guide.md` → "Auto-Edit Heuristics
    (talking head…)"
  - montage (music-as-runtime, hook shot, select_potential tiers, onset
    density → cut length, pacing-zone placement, onset snapping):
    `docs/guides/editorial-decision-guide.md` → "Auto-Edit Heuristics
    (montage…)"
- Decision layer internals: `src/domains/auto_edit/utils/auto_edit.py` (talking-head),
  `src/domains/auto_edit/utils/montage_edit.py` (montage — genre="montage", music required,
  no ducking), `src/domains/auto_edit/utils/cut_ir.py`,
  `src/domains/auto_edit/utils/music_analysis.py`
