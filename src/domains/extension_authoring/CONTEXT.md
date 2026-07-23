# Extension Authoring — Context (ICM Layer 3, stub)

Domain-specific code for **Extension Authoring** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/dctl_templates.py`
- `utils/extension_authoring_live_probe.py`
- `utils/fuse_templates.py`
- `utils/script_templates.py`

## Depth

- Kernel: `docs/kernels/extension-authoring-kernel.md`
- Claude Code skill: `.claude/skills/` (`resolve-extension-authoring`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
