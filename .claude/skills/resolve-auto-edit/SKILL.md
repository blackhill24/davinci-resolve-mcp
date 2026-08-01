---
name: resolve-auto-edit
description: Autonomous brief-to-rendered-video pipeline in the DaVinci Resolve MCP — talking-head/interview, and montage (B-roll cut to music, genre="montage"). Apply when the user names source files, optional music, and the kind of video they want and expects a finished cut — "edit this interview down to 3 minutes with music and a title", "cut this B-roll into a highlight reel set to this track", "make a music video / sizzle reel / supercut from these", or anything asking for clips synced or cut to a beat. Mixed source frame rates are fine; montage needs a music track. Orchestrates start_brief → analysis → plan_cut → the ONE approve_cut checkpoint → build_timeline → finish (grade/motion/subtitles/render/QC) — the same execution for both genres; only the planning step differs.
---

# Resolve Auto Edit — Claude Code Skill

Host orchestration for the `auto_edit` compound tool. The pipeline is
autonomous BETWEEN checkpoints, not instead of them: exactly one human
approval (`approve_cut`) sits between planning and execution.

## The loop

1. `auto_edit(action="start_brief", params={files, music?, genre?, deliverable?,
   target_duration_seconds?, title_text?, options?})` — validates media, scaffolds
   Footage/Music bins, kicks the analysis batch.
   **`genre` selects the decision layer**: `"talking_head"` (default) or
   `"montage"`. Montage additionally *requires* `music` — its length sets the
   runtime; `target_duration_seconds` still applies there but only as a **cap**
   (`min(track, target)`), never to stretch past the track. Montage ignores
   `title_text` — its plans carry no titles (see step 5 for how to add one).

   **`options` — vision is what makes a montage possible at all.** Montage
   builds its entire candidate pool, and its `select_potential` ranking, from
   vision-derived shot descriptions: the `shots` table has one writer, fed only
   by the vision pass. **`genre="montage"` therefore defaults `options.vision`
   to `true`** (talking-head still defaults it off — transcription carries that
   genre). Passing `options={"vision": false}` on a montage is honoured but
   warned about, and it will fail at step 3. `start_brief` returns
   `vision_enabled` — check it. Other keys: `options={"sampling_mode": …}`.

   **Failure signature to recognise:** a `plan_cut` error of `"no usable
   shots"` or `"not enough distinct shots"` means the vision pass never ran.
   Both errors now carry a `remediation` string. The fix is to re-run
   `start_brief` with `options={"vision": true}`, or to run `media_analysis`
   with vision enabled over the same clips — not to retry `plan_cut`.

   **`deliverable` is a stored label only** — it is validated and saved, and
   nothing downstream reads it. It does not select an output spec, so
   `"youtube_1080p"` (the default) does not make the render 1080p. The render is
   controlled at step 8 by `render={target_dir, format?, codec?, settings?}`;
   discover valid values with `render(probe_render_matrix)` or
   `render(list_presets)`.
2. Poll `brief_status(brief_id)`. While the job runs, complete any
   `commit_vision` handoffs the analysis requests (host reads frames, returns
   JSON) — deep passes feed better cut decisions.
3. `plan_cut(brief_id)` → CutList + markdown summary.
   **Montage has a scout gate here.** Instead of a plan, `plan_cut` may return a
   `deep_vision` in-point scout OFFER for ONE not-yet-scouted clip — read its
   frames, commit, then call `plan_cut` again. That is the normal path, **not a
   failure**; it is cache-aware, so it never re-offers a shot on a later
   revision and never blocks. Escape hatches: `plan_cut(brief_id, scout=false)`,
   or just ignore the offer and call `plan_cut` again to plan with whatever
   `best_moment`/shot-start in-points already exist.
   `scout_confirm_token` completes the offer's estimate/confirm handshake.
4. **Show the summary to the user verbatim.** This is the checkpoint artifact.
   Talking-head: runtime, segment table with transcript excerpts, removed-cut
   counts, title, music line and the music-bed consent line. Montage renders a
   different table (`render_montage_summary`) — role / description / pacing
   columns, and no consent line. Relay whichever you actually got; don't
   describe columns that aren't there.
   `get_cut_summary(plan_id, format="markdown"|"json")` re-reads a saved plan's
   summary if you lost it.
5. Iterate with `revise_cut(brief_id, notes, edits=[{op: reorder|drop|keep|title, …}])`
   — that is the whole op set — until the user is happy. Revisions are new
   plans; old ones stay loadable. A `title` op is how a montage gets a title
   at all. **On a beat-locked montage, `drop`/`reorder`/`keep` cost the beat
   lock**: the revision re-packs record frames, so cuts after the change fall
   off the grid and the cut runs shorter than the track. The plan says so
   (`beat_lock_broken`, and a note in the summary) — re-plan for a clean
   result, or relay the tradeoff. `title` is safe (video and music shift
   together).
6. `approve_cut(plan_id, music_bed_consent=<user's explicit choice>)` — the
   confirm-token ceremony. Never assume consent for the ducked-bed render; ask.
   **Talking-head only** — for a montage plan `approve_cut` forces the static
   bed regardless of what consent flags you pass (no voiceover to duck under),
   so don't put a meaningless consent question to the user.
7. `build_timeline(plan_id)` — append-rebuild; check the readback
   (`usage_summary`, `build_errors`, `punch_ins`) and report anomalies. On a
   montage plan `build_errors` also carries the beat-alignment deviations.
7b. *(optional)* `polish_timeline(plan_id, options?)` — exports the built
   timeline to `.drt`, runs the verified `drp-format` vendor ops on it in
   scratch and reimports a NEW `(polished)` timeline, leaving the built one
   intact. The ONLY place transitions and speed ramps can be authored.
   `options`: `lower_thirds[]`, `dissolve_at_segments[]`,
   `dissolve_on_beat_change`, `dissolve_frames`, `lower_third_frames`/`_track`,
   `no_dissolves`, `no_lower_thirds`, `no_retime`. **Montage defaults to
   `no_dissolves`** (every montage cut is a source change; re-enabled, only cuts
   opening a `breathe` section get one) and authors the speed ramps its
   `retime`-flagged segments call for.
   Check `clips_relinked`; a `warning` means real source clips went offline.
8. `finish(plan_id, grade?, motion?, subtitles?, render={target_dir, format?,
   codec?}, target?)` — verify the reported `output_path` exists before
   declaring success. `target` picks the `built` (default) or `polished`
   timeline; polish-only work needs `target="polished"` to reach a render.
   - `motion={}` (montage) turns on the beat-locked pass: zoom ramp + beat
     pulse, flash frames, shake, fade-out, optical-flow retime process,
     vignette, grain, letterbox. `look` is its ONLY sub-key —
     `motion={"look": false}` drops exactly the vignette, grain and letterbox
     and keeps the rest. Omit `motion` and none of it is applied.
   - `grade={"match": …}` is stage 1 — a per-look-bucket match CDL so shots lit
     differently intercut. Pass the plan's own `look_buckets` straight through;
     `{<bucket>: {"cdl": …}}` is the explicit form. `lut_path`/`cdl`/`drx_path`
     still apply uniformly on top as stage 2.
   - `qc` runs on montage after a successful render, returning a host-vision
     handoff. Disable with `qc=false`.
   - On Linux, `subtitles` hits a `SUBTITLE_GENERATION_CRASH_GUARD` refusal
     (issue #90: `CreateSubtitlesFromAudio` kills the Resolve process, no
     exception). Skip it and import an offline-generated `.srt` via
     `timeline(action="import_srt")` — live-proven, avoids the crashing API.
9. *(montage, if `finish` returned `qc`)* look at the frames it names, then
   `commit_qc(plan_id, qc_report=…)` — normalizes the findings and, for one tied
   to a specific cut, hands back a suggested `revise_cut` edit. Read-only.

`list_briefs()` recovers saved briefs (newest first) after a context reset.

## From what the user asked for to the knob (montage)

Nothing parses the brief's prose — *you* pick the parameter. Where there is no
knob, say so instead of inventing one.

| the user asks for | what you actually do |
|---|---|
| "cut this to the track" | `start_brief(genre="montage", music=…)` — the track's length IS the runtime |
| "make it 30 seconds" | `target_duration_seconds` — trims only; it can never run past the track |
| "faster cuts" / "more energy" | **no parameter exists.** Cut length = the section arrangement × the track's tempo. A different track is the real lever; otherwise hand-cut after the build (`timeline split_clip`/`trim_clip`, drt). Don't invent an option |
| "start on the wide" / "reorder" | `revise_cut({op:"reorder"})` — costs the beat lock (step 5) |
| "lose that shot" | `revise_cut({op:"drop", index})` — same cost |
| "put a title on it" | `revise_cut({op:"title", text})`; lower-thirds instead → `polish_timeline(lower_thirds=[…])` |
| "use the best bit of each clip" | let step 3's scout gate run and commit its frames — don't reach for `scout=false` |
| "leave it clean / no effects" | omit `motion` entirely |
| "cinematic" | `motion={}` — vignette, grain and letterbox ride along |
| "lose the letterbox/grain" | `motion={"look": false}` |
| "make the shots match" | `grade={"match": plan["look_buckets"]}` — the plan already computed them |
| "apply my LUT / this look" | `grade={"lut_path": …}` (or `cdl`/`drx_path`) — stage 2, on top of match |
| "add dissolves" | `polish_timeline(options={"no_dissolves": false})` — montage suppresses them by default |
| "add some slow-mo" | already planned — but you don't place it: `retime` is raised on `build` and `accelerate` sections only. `polish_timeline` authors the real ramp, so render it with `finish(target="polished")` |
| "flash / shake on that hit" | not addressable either — `flash` fires on every section-opening downbeat, `shake` on `drop`/`high`, `fadeout` on the outro's last shot. Take them or leave them (`motion` omitted) |
| "check it before you call it done" | let `qc` run, look at the frames, `commit_qc` |
| "just render it" | `finish(render={target_dir}, qc=false)` |

Montage **requires** music — catch that in the brief, not at the failure. Mixed
frame rates across the sources are fine: the timeline is cut at the most common
footage rate, each shot keeps source frames in its own rate, and Resolve
conforms the odd one out by preserving its real-time length, so the beat lock
holds. The plan says which rates it saw.

## Montage editing elements

Applied **for you**: beat-locked cut boundaries and section arrangement, hook
shot, per-shot in-points, zoom/pulse motion, flash, shake, fade-out, letterbox,
vignette/grain, look-bucket match CDLs, speed ramps, static music bed. Below is
what you reach for **by hand**. `drt` = export → mutate in scratch → reimport a
NEW timeline (needs Node.js); the original is never touched.

| need | route | where |
|---|---|---|
| assemble from source ranges | `media_pool(append_to_timeline, clip_infos=[…])`; `timeline(create_variant_from_ranges, pack=true)` | live |
| transition / dissolve | `timeline(add_transition, {track_index, at_frame, duration_frames})` | drt |
| speed change / ramp | `timeline(set_clip_speed, {speed \| keyframes})`, `timeline(fit_to_fill_edit)` | drt |
| razor / trim / slip / slide | `timeline(split_clip \| trim_clip \| slip_clip \| slide_clip \| move_clip)` | drt |
| zoom / pan / rotate / flip | `timeline_item(set_transform)` — Pan, Tilt, ZoomX/Y, RotationAngle, FlipX/Y | live |
| crop / letterbox | `timeline_item(set_crop)` — CropTop + CropBottom | live |
| opacity / composite mode | `timeline_item(set_composite)` | live |
| many clips at once | `timeline(bulk_set_item_properties, ops=[…])` — transform/crop/composite/audio/clip_color in one call | live |
| dynamic zoom, stabilization | `timeline_item(set_property)` on `DynamicZoomEnable`/`StabilizationEnable`…; `timeline_item_color(stabilize \| smart_reframe)` | live |
| animation / keyframes | `fusion_comp(add_keyframe)` / `fusion_comp(bulk_set_expressions)` — the **only** working keyframe path | live |
| nesting | `timeline(create_compound_clip \| create_fusion_clip)`; `timeline(set_clips_linked)` | live |
| in-place edits | `timeline(replace_edit \| place_on_top_edit \| insert_edit \| overwrite_range \| lift_range)` | live/drt |
| titles | `timeline(safe_place_overlay, kind="fusion_title")` then `fusion_comp(set_text_plus)` | live |
| review marks | `timeline_item_markers(set_clip_color \| add_flag)`; `timeline_markers(add)` | live |
| undo safety | `timeline_versioning(begin_run/end_run)` around N destructive calls → ONE archive; `archive_current`, `rollback` | live |

## What the connector cannot do

Don't burn calls on these (`src/core/api_truth.py` is the truth table):

- No live transition API — every transition goes through the `drt` path above.
- `SetProperty("Speed")` returns False on 21.x; a real speed change is `drt` only.
- No freeze-frame anywhere: no live method, and the `drt` retime op requires a
  *positive* speed, so 0× is not authorable either. Closest is a still —
  `gallery_stills(grab_and_export, folder_path=…)` — imported back as media.
- `timeline_item(add_keyframe)` returns `KEYFRAMES_UNSUPPORTED` — TimelineItem
  exposes no keyframe API at all. Use `fusion_comp`. That is also why there is
  no fade handle: an opacity/audio fade is a keyframe ramp, so it has to be a
  Fusion expression (`montage_motion.build_fadeout_expression` is exactly this).
- Generator/title inserts always land on V1; there is no track selector.

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

- Action boundary: `docs/kernels/auto-edit-kernel.md` — "Montage genre" covers
  the genre split, "Reachable editing elements" expands the table above.
- Full editing surface + its Supported/Partial/Unsupported boundaries:
  `docs/kernels/timeline-edit-kernel.md`, and the `resolve-timeline-edit` skill.
- Editorial heuristics in `docs/guides/editorial-decision-guide.md` — **pick the
  section matching the genre**, they do not overlap: "Auto-Edit Heuristics
  (talking head…)" for pacing/punch-in/titles/music levels, "(montage…)" for
  music-as-runtime, beat grid, arrangement, hook, scouting, look buckets, motion.
- Decision layer: `src/domains/auto_edit/utils/` — `auto_edit.py`
  (talking-head + polish ops), `montage_edit.py`, `montage_arrangement.py`
  (section schedule + flags), `montage_motion.py` (Fusion expressions),
  `cut_ir.py`, `music_analysis.py`.
