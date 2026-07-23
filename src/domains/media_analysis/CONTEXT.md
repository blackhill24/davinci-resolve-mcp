# Media Analysis — Context (ICM Layer 3)

Reading or analyzing source media (technical, visual, or transcription) to
inform Resolve actions. Prompt: `/analyze_media`. Largest domain by utils file
count — the former `media_analysis.py` monolith (7.7k lines) is now 13 files
(Phase 5 / #50), plus a dozen longer-standing siblings.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Entry point / dispatch | `actions.py` (`media_analysis`) | `granular/` | one big action-dispatch tool |
| Caps gating (token/frame budgets) | `utils/caps_gating.py` | — | zero cross-file deps — the "leaf" module, extend here first |
| Identity/registry, capabilities/planning, ffprobe probing | `utils/clip_identity_registry.py`, `utils/capabilities_and_planning.py`, `utils/technical_probe.py` | — | hash/registry, install+plan, scene/cut detection |
| Sampling, subtitles/reuse, transcription | `utils/sampling_and_frames.py`, `utils/subtitles_and_reuse.py`, `utils/transcription.py` | — | frame-time selection, SRT/VTT + reuse match, whisper |
| Vision prompting / marker-plan synthesis | `utils/vision_prompt.py`, `utils/marker_plan.py` | — | split at the natural fps-helper boundary |
| Plan execution + reports | `utils/execute_engine.py`, `utils/reports.py` | — | async/sync exec; commit/load/summarize/coverage |
| SQLite analysis index build vs query | `utils/analysis_index_build.py`, `utils/analysis_index_query.py` | — | |
| DB-canonical clip-analysis store | `utils/analysis_store.py` | — | source of truth (schema v9+); JSON is a derived export |
| Deep vision, entities, relationships, embeddings | `utils/deep_vision.py`, `utils/entities.py`, `utils/shot_relationships.py`, `utils/embeddings.py` | — | Phase B/D/§4 of the analysis program |
| Memory (bin summaries), batch jobs, strata, sync detect | `utils/analysis_memory.py`, `utils/media_analysis_jobs.py`, `utils/strata*.py`, `utils/sync_detection.py` | — | |

## Conventions & gotchas

- This module must not import media_analysis at module level from
  `analysis_store.py`/etc — several sibling files use a lazy `_ma()`-style
  accessor into a specific split file to avoid write-path cycles; check for
  that pattern before adding a new cross-file call.
- Chat-context visual analysis requires the live MCP request path (host
  sampling) — the standalone dashboard cannot call it.
- Live: `media_analysis`. Offline (resolve-advanced): `media`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Depth: `docs/kernels/README.md` + `docs/guides/media-analysis-guide.md`.
> Skill: `.claude/skills/resolve-media-analysis/SKILL.md`. Keep this file ≲40 lines.
