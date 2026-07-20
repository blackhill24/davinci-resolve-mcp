# Timeline Edit Kernel Boundary

The timeline edit kernel is the supported MCP layer for clip duplication,
range-copy workflows, and timeline-item state copying. It is built only on
DaVinci Resolve's public scripting API and preserves source media integrity:
it references existing Media Pool items and never transcodes, renders, proxies,
or creates derivatives of source media.

## Live Probe Summary

The current boundary was validated on DaVinci Resolve Studio 20.3.2.9 with
disposable projects and generated synthetic media.

Final exhaustive probe result from May 9, 2026:

| Status | Count |
| --- | ---: |
| Supported | 255 |
| Partially supported | 4 |
| Unsupported | 138 |
| Version/page dependent | 4 |
| Errors | 0 |

The probe first runs the live duplicate/range validation harness, then creates
a separate disposable project to inspect runtime method availability, timeline
operations, item properties, keyframes, metadata, cache/voice/take/Fusion/grade
surfaces, and known API boundaries.

Run it with:

```bash
.venv/bin/python tests/live_duplicate_clips_validation.py --output-dir /tmp/timeline-kernel-probe
```

The output directory receives both JSON and Markdown reports. Reports are
generated artifacts and should not be committed.

## Supported Kernel Features

### Clip duplication

`timeline(action="duplicate_clips")` duplicates existing video timeline items
by re-appending the same Media Pool item with the same source trim through
`MediaPool.AppendToTimeline([{clipInfo}])`.

Supported placement modes:

- `same_time`
- `offset`
- `at_playhead`
- `track_above`
- `after_source`
- `next_gap`

Supported placement controls:

- `clip_ids` from timeline item unique IDs
- `selected=True` when Resolve exposes selected/current item state
- `target_track_index`
- `track_offset`
- `record_frame`
- `record_frame_offset`

`copy_clips` is an alias for duplication. `move_clips` duplicates successfully
first, then deletes the original source items.

### Linked audio

`include_linked=True` duplicates linked audio items alongside the source video
item when Resolve exposes linked item handles. The kernel restores the
video/audio link state on the duplicated items.

### Range operations

The range tools build exact append operations from overlapping source segments:

- `copy_range`
- `duplicate_range`
- `overwrite_range`
- `lift_range`

`copy_range` and `duplicate_range` copy exact video/audio source segments from
explicit `start_frame`/`end_frame` values or the current mark in/out. The
destination uses `record_frame` and optional track targeting.

`overwrite_range` deletes whole destination items that overlap the destination
range, then appends the copied range.

`lift_range` deletes whole timeline items in a range. Partial overlaps are
blocked by default because Resolve does not expose a public razor/split
primitive. Passing `allow_partial_item_delete=True` confirms deletion of whole
overlapping items; it does not perform a partial trim.

### Copyable item state

The kernel can copy these state groups when Resolve exposes readable/writable
item APIs for the current item type and page state:

- `transform`
- `crop`
- `composite`
- `audio`
- `retime`
- `dynamic_zoom`
- `scaling`
- `stabilization`
- `clip_color`
- `markers`
- `flags`
- `enabled`
- `cache`
- `voice_isolation`
- `fusion`
- `grades`
- `takes`
- `keyframes`

`copy_properties=True` copies all supported groups. A list or comma-separated
string can scope copying to specific groups. `copy_keyframes=True` adds the
`keyframes` group.

### Capability reporting

`timeline(action="edit_kernel_capabilities")` returns the maintained support
map for supported, partially supported, and unsupported behavior.

`timeline(action="probe_edit_kernel_item")` is a read-only item probe. It
reports method availability, `GetProperty()` output, known property values,
keyframe counts, and linked item summaries.

`timeline(action="title_property_scan")` inspects undocumented title and
generator `TimelineItem.GetProperty()` keys for the selected item scope.
`set_title_text` and `bulk_set_title_text` use explicit or scanned keys when a
Resolve build accepts `SetProperty()` writes for title text payloads.

## Partial Support

The probe classifies a feature as partially supported when Resolve exposes a
public API surface, but success can vary by item type, page, build, or current
UI state.

Current partial areas:

- Audio properties: some timeline audio `SetProperty` calls return false on
  Resolve Studio 20.3.2.9.
- Dynamic zoom, scaling, and stabilization: copied through exposed
  `TimelineItem.GetProperty`/`SetProperty` keys when the build accepts writes.
- Edit-page title and generator text: `title_property_scan` can expose
  undocumented Text+ keys, but key names and write acceptance vary by item type,
  page, and Resolve build.
- Cache and voice isolation: copied only when item-level read/write APIs are
  callable for the item.
- Keyframes: copied for exposed properties, but Resolve does not expose enough
  interpolation detail for full-fidelity readback in every case.

## Unsupported Boundaries

These are blocked by Resolve's public scripting API, not by MCP plumbing:

- Transition cloning: no public timeline-item transition clone/read/write API.
  Offline workaround: `timeline(action="add_transition"|"list_transitions")` —
  see Advanced (offline) server below.
- Razor/split edits: no direct public timeline split primitive.
  Offline workaround: `timeline(action="split_clip")` — see below.
- Trim/move/slip/slide: no TimelineItem position setters (`docs/reference/
  api-limitations.md`). Offline workaround: `timeline(action="trim_clip"|
  "move_clip"|"slide_clip"|"slip_clip")` — see below.
- True partial lift: no safe partial-item delete without a split primitive.
- Source-less item cloning through append: titles, generators, Fusion
  compositions, and subtitles can exist on the timeline, but source-less items
  do not provide a Media Pool item that `AppendToTimeline([{clipInfo}])` can
  clone.
- Deep speed-ramp semantics: exposed retime properties and supported keyframes
  can be copied, but opaque speed-ramp curves are not independently inspectable.

## Version/Page Dependent Behavior

Some behavior depends on Resolve's current page or bridge state:

- `selected=True` can fail to resolve selected/current timeline items when
  Resolve does not expose selection state to the scripting bridge.
- After inserting or probing source-less items, some Resolve bridge states lose
  a callable `Project.SetCurrentTimeline`; the probe reports this as
  version/page dependent rather than a kernel failure.
- `probe_edit_kernel_item` is stable when the project has a current timeline;
  if Resolve loses current timeline state, the harness records the bridge
  condition separately.

## Advanced (offline) server — editorial interchange + changelist

The kernel above restructures a *running* timeline. The companion advanced server
(`davinci-resolve-advanced`, see `resolve-advanced/README.md`) authors/diffs
timelines and reasons over editorial interchange with **no Resolve running**:

- **`editorial`** — `parse_interchange` (EDL/OTIO/XMEML; **AAF = honest refuse**),
  `turnover_changelist` (moved/retimed/replaced/new/gone between two cuts, with
  timing silent-lie guards), `conform_manifest`, `marker_roundtrip`.
- **`drt`** — `.drt` timeline file authoring + structural diff.

Use these to answer "what changed between v3 and v4" or to hand a conform an
accurate change list without opening either timeline. For conforming/relinking
that change list, see the Timeline Conform / Interchange kernel and the
`resolve-conform` skill; for the edit ↔ offline routing, see the `resolve-edit`
skill (`.claude/skills/timeline-edit.md`).

### Stage 3.1 UI-gap workarounds (issue #21)

A second family of `timeline` actions closes the trim/razor/transition/edit-mode
gaps above by exporting the *current* timeline to `.drt`, running verified
`resolve-advanced` (drp-format) ops on it, and reimporting the result as a
**NEW** `"<name> (edited)"` timeline — the original is never modified (same
convention as `auto_edit.polish_timeline`). Requires Node.js 18+ on PATH
(`advanced_bridge.node_available()`); honestly refuses otherwise.

- `trim_clip(clip_id, edge?, new_duration?|frames?, ripple?)` — tail or head trim.
- `move_clip(clip_id, to_track?, to_start?)` / `slide_clip(clip_id, to_start)` —
  reposition, no ripple/collision-check.
- `slip_clip(clip_id, frames)` — shifts source content later (`frames > 0`
  only; drp-format has no head-extend primitive to retreat the in-point).
- `split_clip(clip_id?|track_type+track_index, at_frame)` — razor.
- `add_transition(track_index, at_frame, duration_frames?)` /
  `list_transitions()` — cross-dissolve; `list_transitions` is read-only (no
  reimport).
- `replace_edit(clip_id, media_pool_item_id, source_start_frame?)` /
  `place_on_top_edit(media_pool_item_id, record_frame, source_end_frame, ...)`
  — pure live-API, mutate the CURRENT timeline in place (position/duration
  don't change, so no drt surgery is needed).
- `insert_edit(media_pool_item_id, record_frame, track_index, source_end_frame,
  ...)` — drt-surgery ripple (`ripple_timeline`, all tracks, keeps A/V sync)
  + a live append into the opened gap on the new timeline.

Deferred to a follow-up pass: clip speed/retime (needs a new drp-format
mutate primitive), Render in Place (pure live-API, unrelated machinery), and
native multicam clip creation (needs drt-diff investigation first).

### Stage 3.2 audio UI-gap workarounds (issue #22, in progress)

Same export->drp-ops->reimport convention as Stage 3.1, for the audio clusters
in `api-limitations.md` the scripting API can't reach.

- `set_clip_volume(clip_id, volume_db)` — generalizes the t14 DRT
  volume-automation writer (issue #14) into a standalone action.
- `set_clip_pan(clip_id, pan_value)` — reverse-engineered the same way
  (export-diff a hand-edited Inspector Pan value); see the "Clip audio pan"
  api_truth entry for the byte-level encoding.

Both are audio-clip-only (`_advanced_edit_resolve_clip` must resolve an audio
track) and require Node.js on PATH like the rest of the drt-surgery actions.

Deferred: track/bus-level fader gain (3.2.1's `set_track_level` — Sm2TiTrack
has no EffectFiltersBA; the fader almost certainly lives in the project-level
FLStudioModelBA blob the `fairlight` tool already reads for bus routing, which
needs its own drt-diff-style investigation, not just a variant of this writer).
EQ/FairlightFX/automation beyond volume (3.2.2), mono/stereo channel format
(3.2.3), and the subtitle clusters (3.2.4-3.2.6) are separate, not-yet-started
sub-stages.

## Development Guardrails

- Use disposable projects and synthetic media for live validation.
- Never write to, transcode, render, proxy, or create derivatives of source
  media while testing timeline-kernel behavior.
- Treat generated probe reports as local artifacts.
- Add new API experiments to the live probe when expanding the boundary map.
