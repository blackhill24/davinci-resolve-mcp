# Project / Database / Archive — Context (ICM Layer 3, stub)

Domain-specific code for **Project / Database / Archive** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/cloud_operations.py`
- `utils/cloud_project_live_probe.py`
- `utils/project_cleanup.py`
- `utils/project_lifecycle_live_probe.py`
- `utils/project_lint.py`
- `utils/project_properties.py`
- `utils/project_spec.py`

## Depth

- Kernel: `docs/kernels/project-lifecycle-kernel.md`
- Claude Code skill: `.claude/skills/` (`resolve-project-lifecycle`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
