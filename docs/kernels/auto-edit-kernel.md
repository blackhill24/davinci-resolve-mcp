# Auto Edit Kernel Boundary

The `auto_edit` compound tool is the autonomous brief-to-rendered-video
pipeline, now spanning two genres — **talking-head** (interview, Phase 1) and
**montage** (B-roll cut to music, epic #38). It is a thin executor over
evidence the analysis program already produces: word-level transcripts
(`transcript_words`), story beats, select potential, and the similarity index.
`start_brief`/`plan_cut` branch by `brief.genre` to the genre's own decision
layer — `src/domains/auto_edit/utils/auto_edit.py` (talking-head) or `src/domains/auto_edit/utils/montage_edit.py`
(montage) — but **share everything downstream**: both produce a
`cut_ir.CutList`, and `build_timeline`/`approve_cut`/`finish`/`revise_cut` are
genre-agnostic executors that only operate on the CutList structure, never on
which decision layer produced it. Both decision layers are pure planning — no
Resolve imports — and the executor uses only the proven
`MediaPool.AppendToTimeline` append-rebuild mechanism. Timelines are stateless
artifacts: revisions rebuild; nothing existing is mutated.

## Montage genre (epic #38)

`montage_edit.build_cut_list_for_brief` ranks candidate shots by
`select_potential` (borrowing `edit_engine.plan_selects`' query approach, not
its execution path), picks a hook shot (highest-ranked overall, prepended),
then assembles the body: local onset DENSITY around each point (from
`music_analysis.detect_beats`'s onset list — no separate DSP) sets the PACING
target, each shot's own `pacing` classification (`still`/`moderate`/`kinetic`/
`variable` — NOT `energy_arc`, which is clip-level only) sets PLACEMENT, and
every cut boundary snaps to the nearest real onset. Shot exhaustion loosens the
select_potential floor then truncates honestly rather than repeating a shot.
Music is required (its length is the runtime); no voiceover/ducking concept —
`approve_cut` forces static ducking for montage regardless of what consent
flags get passed. `render_montage_summary` replaces `render_cut_summary` for
montage plans (detected by CutList segment role, no schema field needed) —
role/description/pacing columns instead of transcript excerpt/smoothing.

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
| `build_timeline` | execute | Append-rebuild: intro title at the head of V1, V1 speech with `mediaType:2` audio mirroring, V2 b-roll positioned appends, punch-in `ZoomX`/`ZoomY`, A2 music trimmed to the cut (ducked bed only when consented). Readback-verified; persists the intro-title `record_offset` for the polish pass. |
| `polish_timeline` | execute (Phase 2) | Pro polish the scripting API can't do. Exports the built timeline as `.drt`, runs verified `drp-format` vendor ops on it in scratch (`place_transition` cross-dissolves at flagged cuts, `place_fusion_title` lower-thirds on an upper track), and reimports a NEW `(polished)` timeline. Export-then-modify preserves media-link blobs byte-for-byte. Op selection is the pure `auto_edit.plan_polish_ops`; execution is `advanced_bridge.run_drp_op_chain`. `options`: `lower_thirds[]`, `dissolve_at_segments[]`, `dissolve_on_beat_change`, `dissolve_frames`, `lower_third_frames`/`_track`, `no_dissolves`, `no_lower_thirds`. |
| `finish` | execute | Grade (`lut_path` / `cdl` / `drx_path`), optional subtitles (`CreateSubtitlesFromAudio`), validated render (`prepare_render_job` → `StartRendering`); verifies the output file exists and reports its path. |
| `list_briefs` | — | Saved briefs, newest first. |

## Build strategy (evidence-backed)

The scripting API cannot add transitions, trim/move existing items, blade,
retime, or automate audio levels (`src/core/api_truth.py`). Hence:

- **Phase 1 — append-rebuild** (this kernel): per-clip in/out (half-open),
  `recordFrame`, `trackIndex`, `mediaType:2` mirroring — the mechanism proven
  in `edit_engine.execute_selects/tighten/swap`.
- **Phase 2 — hybrid drt surgery** (`polish_timeline`): export the built
  timeline as `.drt`, run verified `resolve-advanced` vendor ops
  (cross-dissolves, lower-thirds), reimport. The offline decision layer
  (`plan_polish_ops`) + op-chain (`run_drp_op_chain`) are built and unit-tested.
  **Live-verified on Resolve Studio 21.0.2.4** (epic #12 probes 1–2): the
  exported `.drt` encodes ABSOLUTE frames (timeline StartFrame baked in, e.g.
  86400 @ 24fps), `place_transition`→"Cross Dissolve" and `place_fusion_title`→
  "Text+" land on the container, and reimport keeps both source clips linked
  (generators/transitions have no MediaPoolItem so they read as "offline" — not a
  broken link). `polish_timeline` therefore offsets ops by `StartFrame + intro
  footprint` and renames post-import via `SetName` (the `timelineName` import
  option is ignored for `.drt`). See `api_truth`. Remaining gate: a clean full
  tool-path run (Resolve proved crash-prone under sustained scripting churn).
- **Audio ducking — tiered**: Tier 1 is the consent-gated ffmpeg bed
  (`music_analysis.render_ducked_bed`, mode `rendered_bed`). Tier 2 (mode
  `drt_automation`, issue #14) writes the bed gain straight into the music clip's
  `.drt` volume — no derivative media, no consent needed — via drp-format
  `set_audio_level` (`audio-effect-encoder.js`; encoding verified live on Resolve
  21.0.2.4, see `api_truth`). Opt in with `approve_cut(prefer_drt_ducking=True)`;
  `plan_polish_ops` then emits a `set_audio_level` op applied in the
  polish_timeline drt round-trip. The Tier-3 xmeml probe is no longer needed.

## Evidence & persistence

- CutList schema + validators: `src/domains/auto_edit/utils/cut_ir.py` (`kind="auto_edit_cut"`,
  half-open frames throughout).
- Briefs and CutLists persist via `edit_engine.save_plan` — content
  fingerprint + stale-plan protection; a tampered plan refuses to build.
- Word-level Pass-1 degrades gracefully to cue-level when the configured
  Whisper backend has no word timestamps.

## Offline tests / live validation

Offline: `tests/test_cut_ir_words.py`, `tests/test_auto_edit.py`,
`tests/test_auto_edit_tool.py`, `tests/test_auto_edit_polish.py`,
`tests/test_advanced_bridge_ops.py`, `tests/test_music_analysis.py`; montage
adds `tests/test_montage_edit.py` (the decision layer, incl. a real
click-track end-to-end run) and `tests/test_montage_wiring.py` (verifies —
doesn't assume — that `apply_revision`/G1-adoption/cut-summary dispatch work
against montage CutLists, not just talking-head ones).
Live: `tests/live_auto_edit_validation.py`, `tests/live_montage_probe.py`
(requires Resolve Studio; see the release process). The montage probe
surfaced two real interactions no amount of offline mocking would have caught
— `start_brief` always kicks a real analysis batch job that wipes seeded
editorial data before montage's own `plan_cut` reads it (fix: seed after
ingest, retry once after the expected first failure), and `resolve_clip_id`
must be Resolve's real media-pool unique ID, not a placeholder string.
