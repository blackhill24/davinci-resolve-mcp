# Timeline Edit — Context (ICM Layer 3, stub)

Domain-specific code for **Timeline Edit** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/clip_query.py`
- `utils/timeline_kernel_live_probe.py`
- `utils/timeline_kernel_probe.py`
- `utils/timeline_title_text.py`

## Depth

- Kernel: `docs/kernels/timeline-edit-kernel.md`
- Claude Code skill: `.claude/skills/` (`resolve-timeline-edit`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
