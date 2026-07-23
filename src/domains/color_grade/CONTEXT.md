# Color / Grade — Context (ICM Layer 3, stub)

Domain-specific code for **Color / Grade** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/cdl.py`
- `utils/color_grade_live_probe.py`
- `utils/lut_paths.py`

## Depth

- Kernel: `docs/kernels/color-grade-kernel.md`
- Claude Code skill: `.claude/skills/` (`resolve-color-grade`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
