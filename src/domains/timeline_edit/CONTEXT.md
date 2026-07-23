# Timeline Edit — Context (ICM Layer 3)

Cutting, trimming, pacing, duplicating/moving clips, copying ranges, building
variants, tightening a cut, or generating an editorial changelist. Prompt:
`/timeline_edit_workflow`. Largest domain by line count (4746) — it's also the
dispatch hub for `audio_fairlight` and `timeline_conform_interchange`, which
have no tools of their own.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Timeline-level ops (tracks, markers-adjacent, mark in/out) | `actions.py` (`timeline`) | `granular/` | biggest single tool |
| AI-assisted editing (conform snapshot, gap detection, subtitles, audio) | `actions.py` (`timeline_ai`) | — | dispatches into `audio_fairlight`/`timeline_conform_interchange` helpers |
| Per-item ops (variant, retime, keyframes, takes) | `actions.py` (`timeline_item`, `timeline_item_takes`) | — | |
| Clip search/filter across a timeline | `utils/clip_query.py` | — | |
| Title/text clip generation | `utils/timeline_title_text.py` | — | |
| Live capability probes | `utils/timeline_kernel_probe.py`, `utils/timeline_kernel_live_probe.py` | — | regenerate kernel counts |

## Key files (only where the name doesn't say enough)

- `actions.py` — 4 `@mcp.tool()`s: `timeline`, `timeline_ai`, `timeline_item`,
  `timeline_item_takes`. Imports `audio_fairlight.actions` and
  `timeline_conform_interchange.actions` helpers lazily (function-local) to
  avoid the cross-domain import cycle that would otherwise result.

## Conventions & gotchas

- Adding an audio or conform/interchange feature? Check whether it belongs in
  `audio_fairlight`/`timeline_conform_interchange` first — this file is the
  DISPATCHER for those, not necessarily where the logic should live.
- Live: `timeline`, `timeline_ai`, `timeline_item`, `timeline_item_takes`.
  Offline (resolve-advanced): `editorial`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/timeline-edit-kernel.md` + `docs/guides/editorial-decision-guide.md`.
> Skill: `.claude/skills/resolve-timeline-edit/SKILL.md`. Keep this file ≲40 lines.
