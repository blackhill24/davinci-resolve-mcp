# Media Pool / Ingest — Context (ICM Layer 3, stub)

Domain-specific code for **Media Pool / Ingest** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/media_pool_changes.py`
- `utils/media_pool_ingest_live_probe.py`
- `utils/multicam.py`

## Depth

- Kernel: `docs/kernels/media-pool-ingest-kernel.md`
- Claude Code skill: `.claude/skills/` (`resolve-media-pool-ingest`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
