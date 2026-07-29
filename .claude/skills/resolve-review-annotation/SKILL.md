---
name: resolve-review-annotation
description: Marker, flag, and clip-color annotation work in the DaVinci Resolve MCP. Apply when adding/reading/copying/moving markers across a timeline, timeline item, or media pool item, setting flags/clip color, syncing marker custom data, or producing a read-only review/annotation report — live in a running Resolve. Routes to the live timeline_markers annotation layer.
---

# Resolve Review Annotation — Claude Code Skill

Thin router; depth stays in the kernel.

- **Live tool mechanics** — `docs/kernels/review-annotation-kernel.md` (the
  `timeline_markers` scope-aware annotation layer).

## One server — annotate live

| Job | Server | Tools |
|---|---|---|
| Add/copy/move markers, flags, clip color; export review reports on a **running** Resolve | `davinci-resolve` (Python, live) | `timeline_markers` (`annotation_capabilities`, `probe_annotations`, `normalize_marker_payload`, `copy_annotations`, `move_annotations`, `sync_marker_custom_data`, `clear_annotations_by_scope`, `export_review_report`, `annotation_boundary_report`) |
| Direct marker/flag/clip-color CRUD on **one timeline item** | `davinci-resolve` (Python, live) | `timeline_item_markers` |
| Direct marker/flag CRUD on **one media pool clip** | `davinci-resolve` (Python, live) | `media_pool_item_markers` |

There is no offline counterpart — annotation state lives only inside the open
Resolve project.

## Scope-aware kernel vs. the per-scope tools

`timeline_markers` is the **scope-aware** layer: it takes a `scope` and does the
cross-scope work (copy/move between scopes, bulk clear, review-report export).
The two per-scope compound tools below are the **direct CRUD** path when you
already know the exact item and want one call, without the kernel's ceremony:

- **`timeline_item_markers`** (identify by `track_type`/`track_index`/`item_index`
  — `item_index` is 0-BASED, `track_index` 1-based): `add`, `get_all`,
  `get_by_custom_data`, `update_custom_data`, `get_custom_data`,
  `delete_by_color`, `delete_at_frame`, `delete_by_custom_data`, `add_flag`,
  `get_flags`, `clear_flags`, `get_clip_color`, `set_clip_color`,
  `clear_clip_color`. This is where timeline-item flags and clip color actually
  live — the Scope Matrix's `timeline_item` row is served by this tool.
- **`media_pool_item_markers`** (identify by `clip_id`): the same marker/flag
  action set against a media pool clip, plus `set_name`,
  `link_full_resolution_media`, `monitor_growing_file`,
  `replace_clip_preserve_sub_clip`. Serves the Scope Matrix's
  `media_pool_item` row.

Reach for `timeline_markers` when the job spans scopes or needs a report; reach
for these two when you are annotating one known item.

**Granular (`--full`).** `timeline_markers` is kernel-only — no one-per-method
granular twin; `src/domains/review_annotation/actions.py` is the sole
implementation, live-only. Granular marker CRUD does exist per-scope in
`src/granular/timeline.py` (`timeline_add_marker`/`timeline_get_markers`/…)
and `src/granular/timeline_item.py` (`ti_add_marker`/`ti_add_flag`/…) — but
try the compound `timeline_item_markers` / `media_pool_item_markers` tools
above first; they cover the same ground without the `--full` toggle, so
granular is only for a call those don't expose. **Prompt** —
`review_annotation_workflow` (`src/server.py`). **Resource** —
`status://current_timeline`.

## Scope Matrix

| Scope | Markers | Custom Data | Flags | Clip Color | Frame Space |
|---|---|---|---|---|---|
| `timeline` | Supported | Supported | Not exposed | Not exposed | Timeline frame id or timecode. |
| `timeline_item` | Supported | Supported | Supported | Supported | Timeline item marker frames. |
| `media_pool_item` | Supported | Supported | Supported | Supported | Source/media pool item frames. |

## Gotchas

- Timeline, timeline item, and media pool item frame spaces are **not**
  interchangeable — `copy_annotations`/`move_annotations` use direct frame
  numbers, so map frames explicitly when moving between scopes.
- Flags and clip color are copied only when both source and target expose
  compatible methods; they are review metadata, not marker records.
- Invalid marker colors are rejected before calling Resolve — check
  `annotation_capabilities` for the validated color list.
- Current-playhead marker insertion needs a current timeline with a readable
  current timecode.

Never modify/transcode/derive source media (AGENTS.md).
