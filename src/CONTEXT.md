# live-server — Context (ICM Layer 2)

Python MCP that drives a **running** DaVinci Resolve via its scripting API.
Domain-specific code lives under `domains/<domain>/utils/`, cross-domain shared
infra under `core/`. This file is a domain **index**; task-level routing lives
one layer down, in each domain's own `CONTEXT.md`.

## Domain index

| Domain | Path | Purpose |
|--------|------|---------|
| Color / Grade | `domains/color_grade/` | grading, LUTs/CDLs/DRX, node graph, gallery |
| Timeline Edit | `domains/timeline_edit/` | cut/trim/pace, variants, dispatch hub for audio + conform |
| Conform / Interchange | `domains/timeline_conform_interchange/` | AAF/DRP/PrProj import-export, conform QC (no tools of its own — dispatched via `timeline_edit`) |
| Delivery / Deliverable QC | `domains/render_deliver/` | render queue/settings/proxies, deliverable QC |
| Fusion Composition | `domains/fusion_composition/` | Fusion node graphs, titles, masks, trackers |
| Audio / Fairlight | `domains/audio_fairlight/` | audio props/sync/subtitles/Fairlight (no tools of its own — dispatched via `timeline_edit`) |
| Media Pool / Ingest | `domains/media_pool_ingest/` | import, multicam, relink, card verification |
| Auto Edit (brief → render) | `domains/auto_edit/` | autonomous talking-head + montage editing |
| Media Analysis | `domains/media_analysis/` | technical/visual/transcription analysis of source media |
| Orchestrate (ingest → deliver) | `domains/orchestration/` | resumable multi-domain conductor |
| Extension Authoring | `domains/extension_authoring/` | Fuse/DCTL/script-plugin authoring + install |
| Project / Database / Archive | `domains/project_lifecycle/` | project/db/archive lifecycle, cloud projects |
| Review Annotation | `domains/review_annotation/` | markers/flags/clip colors on timeline or media pool |

Each domain's `CONTEXT.md` has the real routing table (task → file → gotcha).
Cross-domain shared infra (governance, transport, brain DB, process/platform,
advanced-bridge) is `core/` — see `core/CONTEXT.md`.

## Key files

- `server.py` — compound server (preferred); `resolve_mcp_server.py` — granular entrypoint.
- `granular/` — one module per Resolve-API object; untouched, separate taxonomy from the
  domains above (`docs/decisions/0001-domain-taxonomy.md`).
- `core/api_truth.py` — API-gap source of truth; regenerates `docs/reference/api-limitations.md`.

## Conventions & gotchas

- Prefer the compound server unless a task specifically needs granular tools.
- Source-media safety in root `AGENTS.md` is non-negotiable, every tool here.
- "No tools of its own" (audio_fairlight, timeline_conform_interchange) means dispatched
  through another domain's compound tool — its `CONTEXT.md` says which.

> Upkeep: when a domain is added/removed/renamed, fix the index above in the same session,
> then run `python3 .icm/drift-check.py --update` from the root. Task-level routing changes
> belong in that domain's own `CONTEXT.md`, not here. Keep this file ≲40 lines.
