# Auto Edit (brief → render) — Context (ICM Layer 3, stub)

Domain-specific code for **Auto Edit (brief → render)** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/auto_edit.py`
- `utils/cut_ir.py`
- `utils/edit_engine.py`
- `utils/montage_edit.py`
- `utils/music_analysis.py`

## Depth

- Kernel: `docs/kernels/auto-edit-kernel.md`
- Claude Code skill: `.claude/skills/` (`resolve-auto-edit`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
