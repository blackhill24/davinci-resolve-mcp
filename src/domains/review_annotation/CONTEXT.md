# Review Annotation — Context (ICM Layer 3)

Adding, reading, copying, or moving markers, flags, or clip colors across a
timeline, timeline item, or media pool item, or producing a read-only
annotation/review report. Prompt: `/review_annotation_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Timeline markers (add/get/delete/copy) | `actions.py` (`timeline_markers`) | `granular/` | frame ids are RELATIVE to timeline start, not absolute timecode — see test fixtures for the conversion gotcha |
| Timeline-item markers | `actions.py` (`timeline_item_markers`) | — | |
| Media-pool-item markers | `actions.py` (`media_pool_item_markers`) | — | |
| Live capability probe | `utils/review_annotation_live_probe.py` | — | regenerates kernel counts |

## Key files (only where the name doesn't say enough)

- `actions.py` — 3 `@mcp.tool()`s: `media_pool_item_markers`, `timeline_markers`,
  `timeline_item_markers`. All timeline marker CRUD lives here, not in
  `timeline_edit` — a non-obvious ownership split worth checking before assuming
  marker code lives with the rest of timeline editing.

## Conventions & gotchas

- `Timeline.AddMarker` frame ids are relative to timeline start (frame 0 = first
  frame); `GetCurrentTimecode`/`SetCurrentTimecode` use absolute timecode as
  shown in the Resolve UI — converting between the two wrong is the most common
  bug class here.
- Annotation state lives only inside the open Resolve project — no offline
  (resolve-advanced) counterpart exists or is planned.
- Live: `timeline_markers`, `timeline_item_markers`, `media_pool_item_markers`.
  Offline: none.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/review-annotation-kernel.md`. Skill: `.claude/skills/resolve-review-annotation/SKILL.md`.
> Keep this file ≲40 lines.
