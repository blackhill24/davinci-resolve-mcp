# Fusion Composition — Context (ICM Layer 3, stub)

Domain-specific code for **Fusion Composition** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/fusion_composition_live_probe.py`
- `utils/fusion_group_settings.py`

## Depth

- Kernel: `docs/kernels/fusion-composition-kernel.md`
- Claude Code skill: `.claude/skills/` (`resolve-fusion-composition`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
