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

## Montage genre (epic #38, rebuilt by the montage-quality epic #175)

`montage_edit.build_cut_list_for_brief` ranks candidate shots by
`select_potential` (borrowing `edit_engine.plan_selects`' query approach, not
its execution path), picks a hook shot (highest-ranked overall, prepended),
then assembles the body. Music is required (its length is the runtime); no
voiceover/ducking concept — `approve_cut` forces static ducking for montage
regardless of what consent flags get passed. `render_montage_summary` replaces
`render_cut_summary` for montage plans (detected by CutList segment role, no
schema field needed) — role/description/pacing columns instead of transcript
excerpt/smoothing.

**Cut placement is grid-locked, not density-driven.** When
`music_analysis.detect_beats` reports `grid_available` with ≥2 beats, cut
length is a property of the ARRANGEMENT:

1. **Beat grid** — `detect_beats` returns `onsets, tempo_bpm, beat_grid,
   bar_grid, downbeats, sections, tempo_confidence, beat_zero, grid_available,
   provisional_tempo_bpm, provisional_beat_grid`. Tempo, phase lock and
   downbeat phase all run on **kick-band** novelty (`lowpass=f=150`), falling
   back to the full mix only when the track has no low end.
2. **Arrangement** (`montage_arrangement.plan_arrangement`) — turns the beat
   grid + sections into an ordered `{beat_index, beat_length, section, role,
   flags}` schedule covering `[0, len(beat_grid))` with no gaps or overlaps.
   `SECTION_CUT_BEATS` sets the cadence per label (intro 4, build 4→2 ramp,
   mid 2, low 4, high 2, drop 2, breathe 6, accelerate 1, outro 6). Flags:
   `flash` (every section opening + the drop), `shake` (drop + `high`),
   `retime` (`build` + `accelerate`), `fadeout` (last entry of the outro).
3. **Scout** (issue #178) — `plan_cut` defaults to `scout=True` and offers a
   `deep_vision` in-point handoff for ONE not-yet-scouted clip, returning the
   offer INSTEAD of a plan. Cache-aware, so it never re-offers; `scout=false`
   or simply re-calling `plan_cut` proceeds with `best_moment`/shot-start
   in-points. In-point preference is scout > best_moment > shot start.
4. **Look buckets** (issue #179) — shots are grouped by colour signature
   (scout data if present, else an ffmpeg signature) and
   `compute_match_cdls` derives a per-bucket match CDL, applied by
   `finish(grade={"match": …})` as stage 1 under the uniform creative look.
5. **Motion/look** (issue #180) — `montage_motion.compute_motion_directive`
   attaches a per-section `motion` directive to each segment; `finish(motion=…)`
   realizes it as a per-clip Fusion comp.
6. **Visual QC** (issue #181) — `finish` builds a host-vision QC request from
   the rendered output; `commit_qc` normalizes the findings back into a
   suggested `revise_cut` edit.

Because every entry's `beat_index` is exactly the previous entry's
`beat_index + beat_length`, each cut's source length is derived from its record
length rather than rounded twice.

**Fallback path** — with no usable grid, the pre-#175 model still runs: local
onset DENSITY around each point (a ~4s window over the onset list, no separate
DSP) sets the PACING target, and each shot's own `pacing` classification
(`still`/`moderate`/`kinetic`/`variable` — NOT `energy_arc`, which is
clip-level only) sets PLACEMENT.

Cut boundaries, though, snap to the **`provisional_beat_grid`** — the
kick-phase-locked pulse `detect_beats` keeps even when confidence misses
`MIN_TEMPO_CONFIDENCE` — and only fall back to raw onset peaks when no tempo
could be estimated at all (the plan says which one ran). Onset peaks are a poor
snap target and this is measured, not assumed: on the reference track they land
within ±12% of a beat 0.228 of the time against a 0.240 chance level, and
filtering to the kick band alone gives 0.243. Peak-picking does not recover the
pulse in ANY band; a phase-locked grid is on it by construction (1.000). The
regression test lives in `tests/domains/auto_edit/test_music_analysis.py`
(`test_onset_peaks_do_not_follow_the_pulse`).

**Mixed frame rates are supported.** The grid is in SECONDS, and Resolve
RESAMPLES off-rate media to preserve its wall-clock length — live-verified on
21.0.2.4 (`tests/domains/auto_edit/live_mixed_fps_probe.py`: 60 frames of
59.94fps media cost 30 frames on a 29.97fps timeline, next clip butting up with
no gap). So the timeline runs at the most common footage rate (ties → the
higher), each segment's SOURCE frames are in its own clip's rate, and its
RECORD length is carried explicitly as `record_length_frames`
(`cut_ir.segment_record_length`, which falls back to the source span so
same-rate and talking-head plans are unchanged). The plan notes the rates it
saw. Shot exhaustion loosens the select_potential floor then truncates honestly
rather than repeating a shot, on both paths.

## The single human checkpoint

`approve_cut` is THE one checkpoint. Its confirm-token preview embeds the full
markdown cut summary plus the music-bed-render consent line: consenting makes
the ducking mode `rendered_bed` (an ffmpeg-rendered DERIVATIVE bed written
under the analysis root — per AGENTS.md source-media safety it is never
produced without this consent); declining keeps a static music level.

## Actions

| Action | Stage | Notes |
|--------|-------|-------|
| `start_brief` | intake | Validates files (exist + ffprobe), scaffolds `Footage`/`Music` bins via safe import, kicks a `media_analysis` batch job. Returns `{brief_id, analysis_job_id, vision_enabled}`. **`options.vision` defaults ON for `genre="montage"`, OFF otherwise** (`auto_edit.resolve_vision_default` / `VISION_REQUIRED_GENRES`): montage's whole candidate pool is the `shots` table, whose single writer is fed by `visual.shot_descriptions` — a vision-only artifact — so vision-off is not a conservative default but a guaranteed `plan_cut` failure. An explicit `options={"vision": false}` is honoured and warned about. `deliverable` is validated and stored but read by nothing: it is a label, not an output spec. |
| `brief_status` / `status` | intake | Brief state machine (`created → analyzing → ready → planned → approved → built → finished`); polls the analysis job. |
| `plan_cut` | plan | Scout-offer round trip: the offer carries its token under BOTH `confirm_token` (deep_vision's name) and `scout_confirm_token`, and this action accepts either — echoing the key you were handed used to re-serve the identical offer forever with no error (#193). Builds the CutList: word-level Pass-1 (fillers/false starts; cue-level fallback), dead-air windows, duration fit, jump-cut smoothing (b-roll via similarity, else punch-in), title, music gain via loudness. Returns the markdown checkpoint summary. **Montage**: `scout` (default true) may return a `deep_vision` in-point offer instead of a plan — see the genre section; `scout_confirm_token` completes its handshake. |
| `revise_cut` | plan | Structured overrides — `reorder` / `drop` / `keep` / `title`, the whole op set — producing revision+1 as a new plan; old revisions stay loadable. **On a grid-locked montage** any op that changes the segment SEQUENCE re-packs record frames off the beat grid (the beat frames are not persisted, so re-snapping is impossible): the revision sets `beat_lock_broken` and says so in `problems`/the summary. **Read `beat_lock_broken` off the returned plan rather than assuming which ops are safe**: EVERY op re-walks the whole cut's record frames, and the flag is set from what actually moved. A title-only revision is expected to leave it unset — `montage_edit.normalize_grid_phase` starts the cut at record frame 0 and the arrangement schedule is contiguous, so the walk reproduces every start — but that is a planner invariant, not a property of the op. (`build_timeline` shifts the music row by the same title `record_offset`.) |
| `get_cut_summary` | plan | Markdown (default) or JSON view of a saved CutList. |
| `approve_cut` | checkpoint | Confirm-token gated; records approval + music-bed consent. |
| `build_timeline` | execute | Result key paths: only `usage_summary` is inside `readback`; `build_errors`, `title` and `beat_alignment` are siblings at the top level, and `punch_ins` is talking-head only (always `[]` on montage). `beat_alignment {checked, deviations}` is montage-only and present only on a grid-locked plan — it proves Resolve placed items where the PLAN said, not that the plan is on the music (`grid_available` decides that). Append-rebuild: intro title at the head of V1, V1 speech with `mediaType:2` audio mirroring, V2 b-roll positioned appends, punch-in `ZoomX`/`ZoomY`, A2 music trimmed to the cut (ducked bed only when consented). Readback-verified; persists the intro-title `record_offset` for the polish pass. |
| `polish_timeline` | execute (Phase 2) | **Refuses with an empty op set on the default montage case — expected, not a failure** (#193): montage defaults to `no_dissolves` and never consults `dissolve_on_beat_change`, so its only default ops are the speed ramps that exist when the arrangement produced `build`/`accelerate` sections. The montage re-enable knob is `options={"no_dissolves": false}`. Pro polish the scripting API can't do. Exports the built timeline as `.drt`, runs verified `drp-format` vendor ops on it in scratch (`place_transition` cross-dissolves at flagged cuts, `place_fusion_title` lower-thirds on an upper track, `retime_clip` speed ramps on `retime`-flagged montage segments), and reimports a NEW `(polished)` timeline. **Montage defaults to `no_dissolves`** — every montage cut is a source change, so the talking-head heuristic would dissolve the whole edit; re-enabled, only a cut that OPENS a `breathe` section gets one. Export-then-modify preserves media-link blobs byte-for-byte. Op selection is the pure `auto_edit.plan_polish_ops`; execution is `advanced_bridge.run_drp_op_chain`. `options`: `lower_thirds[]`, `dissolve_at_segments[]`, `dissolve_on_beat_change`, `dissolve_frames`, `lower_third_frames`/`_track`, `no_dissolves`, `no_lower_thirds`. |
| `finish` | execute | Grade (`lut_path` / `cdl` / `drx_path`), optional subtitles (`CreateSubtitlesFromAudio`), validated render (`prepare_render_job` → `StartRendering`); verifies the output file exists and reports its path. **Montage**: `grade={"match": …}` applies per-look-bucket CDLs as stage 1 beneath the uniform look — both the explicit `{bucket: {"cdl": …}}` and the plan's own `look_buckets` (`{bucket: <raw CDL>}`, from `montage_edit.compute_match_cdls`) are accepted, so the suggestion can be fed straight back. **Only `lut_path` composes with `match`** (`SetLUT` is a separate node control); a uniform `cdl` is `item.SetCDL` on the same items and REPLACES it, and `drx_path` replaces the whole graph — `graded["match"]["overwritten_by"]` + `warning` report the destructive combination, because `match.applied` counts what was set, not what survived; `motion={}` applies the beat-locked Fusion motion/flash/shake/fadeout/look pass (`motion={"look": false}` drops vignette/grain/letterbox); `qc` runs after a successful render unless `qc=false`. `target` selects the `built` (default) or `polished` timeline. |
| `commit_qc` | review | Finishes `finish`'s visual-QC host-vision handoff: normalizes the returned findings and, for one tied to a specific cut, hands back a suggested `revise_cut` edit. Read-only against the plan — no confirm token. |
| `list_briefs` | — | Saved briefs, newest first. |

## Build strategy (evidence-backed)

The scripting API cannot add transitions, trim/move existing items, blade,
retime, or automate audio levels (`src/core/api_truth.py`). Hence:

- **Phase 1 — append-rebuild** (this kernel): per-clip in/out (half-open),
  `recordFrame`, `trackIndex`, `mediaType:2` mirroring — the mechanism proven
  in `edit_engine.execute_selects/tighten/swap`.
- **Phase 2 — hybrid drt surgery** (`polish_timeline`): export the built
  timeline as `.drt`, run verified `resolve-advanced` vendor ops
  (cross-dissolves, lower-thirds, montage speed ramps via `retime_clip` —
  `newDuration` pinned to the segment's beat-locked record length and `ripple`
  off, so the grid survives), reimport. The offline decision layer
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

## Reachable editing elements (montage)

What the pipeline applies **for you**: beat-grid-locked cut boundaries and
section arrangement, hook shot, per-shot in-points, zoom/pulse motion, flash
frames, shake, fade-out, letterbox, vignette/grain, look-bucket match CDLs,
speed ramps, static music bed.

Everything else is reached by hand, after `build_timeline`. `live` = the
scripting API; `drt` = export → mutate in scratch → reimport a NEW timeline
(needs Node.js; the original is never touched). The canonical
Supported/Partial/Unsupported boundary lists live in
`docs/kernels/timeline-edit-kernel.md`; the negative truth table is
`src/core/api_truth.py`.

| Need | Route | Where |
|---|---|---|
| Assemble from source ranges | `media_pool(append_to_timeline, clip_infos=[…])`; `timeline(create_variant_from_ranges, pack=true)` | live |
| Transition / dissolve | `timeline(add_transition, {track_index, at_frame, duration_frames})` | drt |
| Speed change / ramp | `timeline(set_clip_speed, {speed \| keyframes})`, `timeline(fit_to_fill_edit)` | drt |
| Razor / trim / slip / slide | `timeline(split_clip \| trim_clip \| slip_clip \| slide_clip \| move_clip)` | drt |
| Zoom / pan / rotate / flip | `timeline_item(set_transform)` — Pan, Tilt, ZoomX/Y, RotationAngle, FlipX/Y | live |
| Crop / letterbox | `timeline_item(set_crop)` — CropTop + CropBottom | live |
| Opacity / composite mode | `timeline_item(set_composite)` | live |
| Many clips at once | `timeline(bulk_set_item_properties, ops=[…])` — transform/crop/composite/audio/clip_color in one call | live |
| Dynamic zoom, stabilization | `timeline_item(set_property)` on `DynamicZoomEnable`/`StabilizationEnable`…; `timeline_item_color(stabilize \| smart_reframe)` | live |
| Animation / keyframes | `fusion_comp(add_keyframe)` / `fusion_comp(bulk_set_expressions)` — the only working keyframe path | live |
| Nesting | `timeline(create_compound_clip \| create_fusion_clip)`; `timeline(set_clips_linked)` | live |
| In-place edits | `timeline(replace_edit \| place_on_top_edit \| insert_edit \| overwrite_range \| lift_range)` | live/drt |
| Titles | `timeline(safe_place_overlay, kind="fusion_title")` then `fusion_comp(set_text_plus)` | live |
| Review marks | `timeline_item_markers(set_clip_color \| add_flag)`; `timeline_markers(add)` | live |
| Undo safety | `timeline_versioning(begin_run/end_run)` around N destructive calls → ONE archive; `archive_current`, `rollback` | live |

Not reachable at all on 21.x: a live transition API; `SetProperty("Speed")`
(returns False); any freeze-frame method; a fade-handle API;
`timeline_item(add_keyframe)` (returns `KEYFRAMES_UNSUPPORTED` — TimelineItem
exposes no keyframe API). Generator and title inserts always land on V1; there
is no track selector.

## Evidence & persistence

- CutList schema + validators: `src/domains/auto_edit/utils/cut_ir.py` (`kind="auto_edit_cut"`,
  half-open frames throughout).
- Briefs and CutLists persist via `edit_engine.save_plan` — content
  fingerprint + stale-plan protection; a tampered plan refuses to build.
- Word-level Pass-1 degrades gracefully to cue-level when the configured
  Whisper backend has no word timestamps.

## Offline tests / live validation

Offline: `tests/domains/auto_edit/test_cut_ir_words.py`, `tests/domains/auto_edit/test_auto_edit.py`,
`tests/domains/auto_edit/test_auto_edit_tool.py`, `tests/domains/auto_edit/test_auto_edit_polish.py`,
`tests/core/test_advanced_bridge_ops.py`, `tests/domains/auto_edit/test_music_analysis.py`; montage
adds `tests/domains/auto_edit/test_montage_edit.py` (the decision layer, incl. a real
click-track end-to-end run) and `tests/domains/auto_edit/test_montage_wiring.py` (verifies —
doesn't assume — that `apply_revision`/G1-adoption/cut-summary dispatch work
against montage CutLists, not just talking-head ones).
The montage speed ramp is covered end-to-end offline, through the REAL Node
vendor op on a synthetic `.drt`, by
`tests/core/test_advanced_bridge_ops.py::test_montage_speed_ramp_op_spec_holds_the_record_duration`
— it asserts the beat-grid invariant (the record slot does not move) that
pinning `newDuration` exists to protect.
Live: `tests/domains/auto_edit/live_auto_edit_validation.py`,
`tests/domains/auto_edit/live_montage_quality.py` (the phase 1–6 gate; 20/20 on
Studio 21.0.2.4), `tests/domains/auto_edit/live_montage_expressions_probe.py`
(the `shake`/`fadeout` expression targets the quality gate cannot reach, because
a track whose arrangement yields only intro/outro never raises a `shake` flag —
confirms Transform exposes `Angle`, and that `MCP_Flash` and `MCP_Fadeout` hold
independent Gain expressions), `tests/domains/auto_edit/live_montage_probe.py`
(requires Resolve Studio; see the release process). The montage probe
surfaced two real interactions no amount of offline mocking would have caught
— `start_brief` always kicks a real analysis batch job that wipes seeded
editorial data before montage's own `plan_cut` reads it (fix: seed after
ingest, retry once after the expected first failure), and `resolve_clip_id`
must be Resolve's real media-pool unique ID, not a placeholder string.
