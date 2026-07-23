# Review Annotation — Context (ICM Layer 3, stub)

Domain-specific code for **Review Annotation** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/review_annotation_live_probe.py`

## Depth

- Kernel: `docs/kernels/review-annotation-kernel.md`
- Claude Code skill: `.claude/skills/` (`resolve-review-annotation`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
