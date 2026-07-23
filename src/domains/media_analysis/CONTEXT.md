# Media Analysis — Context (ICM Layer 3, stub)

Domain-specific code for **Media Analysis** (restructure epic #52). This is a stub —
Phase 2 (#47) only moved files here; the full per-domain routing table lands
in Phase 7 (#49) once `server.py`'s tool functions also move here (Phase 3,
#46).

## Files

- `utils/analysis_caps.py`
- `utils/analysis_memory.py`
- `utils/analysis_store.py`
- `utils/deep_vision.py`
- `utils/embeddings.py`
- `utils/entities.py`
- `utils/media_analysis.py`
- `utils/media_analysis_jobs.py`
- `utils/shot_relationships.py`
- `utils/strata.py`
- `utils/strata_analyzers.py`
- `utils/strata_faces.py`
- `utils/strata_queries.py`
- `utils/strata_story.py`
- `utils/sync_detection.py`

## Depth

- Kernel: `docs/kernels/README.md`
- Claude Code skill: `.claude/skills/` (`resolve-media-analysis`)

> Upkeep: when files here change (add/remove/rename), fix the list above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
