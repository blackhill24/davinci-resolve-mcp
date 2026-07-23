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

There is no offline counterpart — annotation state lives only inside the open
Resolve project.

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
