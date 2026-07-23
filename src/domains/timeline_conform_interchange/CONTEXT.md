# Conform / Interchange — Context (ICM Layer 3)

Importing/relinking editorial, checking a conform against a reference, repairing
reversed/retimed subclips, tracing grades across a re-conform, or QCing a
conformed timeline. Prompt: `/conform_workflow`.

**No tools of its own** — `actions.py` holds only private (`_`-prefixed) helpers.
Entry points are `timeline`/`timeline_item` in `timeline_edit` (conform snapshot,
gap/overlap detection, story-spine, AAF/DRP/PrProj import/export), plus lazy
calls from `auto_edit` (export/import checked) and `media_pool_ingest` (readback).

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| AAF/DRP/PrProj import, export, relink | `actions.py` (`_import_timeline_checked`, `_export_timeline_checked`, `_binary_post_import_relink`, `_import_from_drp`) | `granular/` | called from `timeline_edit`, `auto_edit` |
| Conform snapshot / gap-overlap / story-spine | `actions.py` (`_timeline_conform_snapshot`, `_detect_gaps_overlaps_from_snapshot`, `_story_spine_from_snapshot`) | — | called from `timeline_edit.timeline` |
| Timeline compare / roundtrip probe | `actions.py` (`_compare_timelines`, `_probe_interchange_roundtrip`) | — | |
| `.drt`/`.drp` export-diff reverse engineering | `utils/drt_diff.py` | — | ground-truth differ, not a live tool |
| Timeline XML sanitize/analyze | `utils/timeline_xml.py` | — | used by AAF/XML import paths across domains |
| Live capability probe | `utils/timeline_conform_live_probe.py` | — | regenerates kernel counts |

## Conventions & gotchas

- New conform/interchange behavior is added here but WIRED into `timeline_edit`'s
  `timeline`/`timeline_item` actions — don't expect a standalone entry point.
- Live: `timeline` (conform actions). Offline (resolve-advanced): `conform`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/timeline-conform-interchange-kernel.md`.
> Skill: `.claude/skills/timeline-conform-interchange.md`. Keep this file ≲40 lines.
