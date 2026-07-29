---
name: resolve-timeline-edit
description: Editing, cutting, trimming, pacing, and timeline restructuring in the DaVinci Resolve MCP. Apply when duplicating/moving clips, copying ranges, building variants, tightening or restructuring a cut, editing selects, or generating an editorial changelist/turnover — live in a running Resolve OR offline against .drt timeline files. Routes to the live edit tools, the offline editorial tools, and the project's editorial craft guidance.
---

# Resolve Timeline Edit — Claude Code Skill

Bridges editorial *craft* to this repo's *tools*. Open the right manual; don't
re-derive it here.

- **Craft / story** — `docs/guides/editorial-decision-guide.md`. The global
  `editor` / `assistant-editor` skills add editorial philosophy and cutting-room
  practice; use them for *why to cut*, not tool mechanics.
- **Live tool mechanics** — `docs/kernels/timeline-edit-kernel.md` (the `timeline`
  edit-kernel boundary: duplicate/copy/move, range ops, item-state copy).
- **Offline timeline / changelist** — `resolve-advanced/README.md` → `drt`
  (timeline file authoring) and `editorial` (interchange + turnover).

## Two servers

| Job | Server | Tools |
|---|---|---|
| Restructure a **running** timeline | `davinci-resolve` (Python, live) | `timeline` (edit kernel), `timeline_item`, `edit_engine`, `timeline_markers`, `timeline_item_takes`, `timeline_ai` |
| Author/diff a `.drt` **file**, or parse/compare editorial interchange with **no Resolve open** | `davinci-resolve-advanced` (Node) | `drt`, `editorial` |

**Granular (`--full`) equivalents.** `src/granular/timeline.py` (track add/
delete/enable/lock, marker CRUD, `timeline_delete_clips`,
`timeline_set_clips_linked`) and `src/granular/timeline_item.py` (per-item
transform/crop/retime/take reads and writes) expose one method per API call
when a compound action doesn't cover it. **Prompt** — `timeline_edit_workflow`
(`src/server.py`) routes a cut/trim/restructure/changelist ask across both
servers. **Resources** — `status://current_timeline`,
`capabilities://installed_tools`.

## Live edit-kernel essentials

- Duplicate/relocate: `duplicate_clips` (modes `same_time`/`offset`/
  `at_playhead`/`track_above`/`after_source`/`next_gap`), `copy_clips` (alias),
  `move_clips` (duplicate-then-delete). `include_linked=True` carries linked audio.
- Ranges: `copy_range`, `duplicate_range`, `overwrite_range`, `lift_range`.
  **No public razor/split** — partial overlaps in `lift_range` are blocked unless
  `allow_partial_item_delete=True` (whole-item delete, not a trim).
- Item state copy: the `copy_properties` **parameter** of `duplicate_clips` /
  `copy_range` / `move_clips` (transform/crop/composite/audio/retime/markers/
  flags/grades/takes/keyframes …); scope with a group list. There is no
  `copy_properties` *action* — it is always a parameter of those append verbs.
- `edit_engine` drives higher-level selects/tighten/swap flows
  (plan → confirm → execute); tighten variants can carry audio via `keep_ranges`
  mirror / `include_audio`.
- **Takes** — `timeline_item_takes` is the take-stack tool (`add`, `get_count`,
  `get_selected_index`, `get_by_index`, `select`, `delete`, `finalize`), keyed
  by `track_type`/`track_index`/`item_index` (`item_index` is 0-BASED). The
  `takes` entry in `copy_properties` only *carries* a take stack during a
  duplicate; building or choosing one is this tool's job.
- **Timeline-wide AI/analysis** — `timeline_ai`: `detect_scene_cuts`,
  `analyze_dolby_vision`, `grab_still`, `grab_all_stills`, `create_subtitles`.
  These are long ops — pass `background=true` for a `job_id` and poll
  `resolve_control(action="job_status", params={"job_id": …})` instead of
  blocking. **`create_subtitles` is refused on Linux**
  (`SUBTITLE_GENERATION_CRASH_GUARD`, issue #90 — `CreateSubtitlesFromAudio`
  kills the Resolve process); import an offline-built `.srt` via
  `timeline(action="import_srt")` instead. `grab_all_stills` is the bulk
  gallery-still grab the color domain builds on.

## Offline editorial (`editorial` actions)

- `parse_interchange` — EDL / OTIO / XMEML (AAF = an honest refuse, not a fake).
- `turnover_changelist` — moved / retimed / replaced / new / gone between two
  cuts, with timing silent-lie guards (it flags what it cannot verify).
- `conform_manifest`, `marker_roundtrip`.

Use these to answer "what changed between v3 and v4" or to hand a conform an
accurate change list **without** opening either timeline in Resolve. For carrying
a *conform* across a re-edit, see the `resolve-timeline-conform-interchange` skill.

## Source-media safety (AGENTS.md)

Edit operations reference existing Media Pool items — they never transcode,
render, proxy, or create derivatives of source media. Keep it that way. Treat
generated probe reports as local scratch artifacts, not committed files.
