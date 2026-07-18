# Auto Edit Kernel Boundary

The `auto_edit` compound tool is the autonomous brief-to-rendered-video
pipeline (Phase 1 genre: talking head / interview). It is a thin executor over
evidence the analysis program already produces: word-level transcripts
(`transcript_words`), story beats, select potential, and the similarity index.
The decision layer (`src/utils/auto_edit.py`) is pure planning — no Resolve
imports — and the executor uses only the proven `MediaPool.AppendToTimeline`
append-rebuild mechanism. Timelines are stateless artifacts: revisions rebuild;
nothing existing is mutated.

## The single human checkpoint

`approve_cut` is THE one checkpoint. Its confirm-token preview embeds the full
markdown cut summary plus the music-bed-render consent line: consenting makes
the ducking mode `rendered_bed` (an ffmpeg-rendered DERIVATIVE bed written
under the analysis root — per AGENTS.md source-media safety it is never
produced without this consent); declining keeps a static music level.

## Actions

| Action | Stage | Notes |
|--------|-------|-------|
| `start_brief` | intake | Validates files (exist + ffprobe), scaffolds `Footage`/`Music` bins via safe import, kicks a `media_analysis` batch job. Returns `{brief_id, analysis_job_id}`. |
| `brief_status` / `status` | intake | Brief state machine (`created → analyzing → ready → planned → approved → built → finished`); polls the analysis job. |
| `plan_cut` | plan | Builds the CutList: word-level Pass-1 (fillers/false starts; cue-level fallback), dead-air windows, duration fit, jump-cut smoothing (b-roll via similarity, else punch-in), title, music gain via loudness. Returns the markdown checkpoint summary. |
| `revise_cut` | plan | Structured overrides — `reorder` / `drop` / `keep` / `title` — producing revision+1 as a new plan; old revisions stay loadable. |
| `get_cut_summary` | plan | Markdown (default) or JSON view of a saved CutList. |
| `approve_cut` | checkpoint | Confirm-token gated; records approval + music-bed consent. |
| `build_timeline` | execute | Append-rebuild: intro title at the head of V1, V1 speech with `mediaType:2` audio mirroring, V2 b-roll positioned appends, punch-in `ZoomX`/`ZoomY`, A2 music trimmed to the cut (ducked bed only when consented). Readback-verified. |
| `finish` | execute | Grade (`lut_path` / `cdl` / `drx_path`), optional subtitles (`CreateSubtitlesFromAudio`), validated render (`prepare_render_job` → `StartRendering`); verifies the output file exists and reports its path. |
| `list_briefs` | — | Saved briefs, newest first. |

## Build strategy (evidence-backed)

The scripting API cannot add transitions, trim/move existing items, blade,
retime, or automate audio levels (`src/utils/api_truth.py`). Hence:

- **Phase 1 — append-rebuild** (this kernel): per-clip in/out (half-open),
  `recordFrame`, `trackIndex`, `mediaType:2` mirroring — the mechanism proven
  in `edit_engine.execute_selects/tighten/swap`.
- **Phase 2 — hybrid drt surgery**: export the approved timeline as `.drt`,
  run verified `resolve-advanced` vendor ops (cross-dissolves, lower thirds),
  reimport. Tracked in the epic's Phase-2 issues.
- **Audio ducking — tiered**: Tier 1 is the consent-gated ffmpeg bed
  (`music_analysis.render_ducked_bed`); Tier 2 (drt volume automation) and the
  xmeml probe are Phase 2.

## Evidence & persistence

- CutList schema + validators: `src/utils/cut_ir.py` (`kind="auto_edit_cut"`,
  half-open frames throughout).
- Briefs and CutLists persist via `edit_engine.save_plan` — content
  fingerprint + stale-plan protection; a tampered plan refuses to build.
- Word-level Pass-1 degrades gracefully to cue-level when the configured
  Whisper backend has no word timestamps.

## Offline tests / live validation

Offline: `tests/test_cut_ir_words.py`, `tests/test_auto_edit.py`,
`tests/test_auto_edit_tool.py`, `tests/test_music_analysis.py`.
Live: `tests/live_auto_edit_validation.py` (requires Resolve Studio; see the
release process).
