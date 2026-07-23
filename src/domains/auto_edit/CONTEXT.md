# Auto Edit (brief → render) — Context (ICM Layer 3)

The user names source files, optional music, and the kind of video they want and
expects a finished cut — autonomous talking-head/interview AND montage editing
with one approval checkpoint. Prompt: `/auto_edit_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Brief-to-render pipeline, revise loop | `actions.py` (`auto_edit`) | `granular/` | dispatches into `audio_fairlight` (subtitles) and `timeline_conform_interchange` (import/export) lazily |
| Edit-engine plan/execute (selects, tighten, swap) | `actions.py` (`edit_engine`), `utils/edit_engine.py` | — | |
| Talking-head decision layer | `utils/auto_edit.py` | — | |
| Montage decision layer (sibling to talking-head, same CutList IR) | `utils/montage_edit.py` | — | genre dispatch wired in #41 |
| Cut intermediate representation | `utils/cut_ir.py` | — | shared IR consumed by both decision layers |
| Ducking ladder + beat detection | `utils/music_analysis.py` | — | |

## Key files (only where the name doesn't say enough)

- `actions.py` — 2 `@mcp.tool()`s: `edit_engine`, `auto_edit`.
- `utils/cut_ir.py` — the CutList intermediate representation both `auto_edit.py`
  (talking-head) and `montage_edit.py` (montage) decision layers produce/consume;
  changing its shape affects both genres.

## Conventions & gotchas

- Talking-head and montage are sibling decision layers over the SAME CutList IR
  — a fix to one genre's edge case may need mirroring in the other.
- The offline decision layer (`cut_ir`/`auto_edit`) mirrors the live pipeline for
  planning without Resolve running; keep the two in sync.
- Live: `auto_edit`, `edit_engine`. Offline (resolve-advanced): `cut_ir`/`auto_edit`
  decision layer.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/auto-edit-kernel.md` + `docs/guides/editorial-decision-guide.md`.
> Skill: `.claude/skills/resolve-auto-edit/SKILL.md`. Keep this file ≲40 lines.
