---
name: resolve-mcp
description: Orientation and index for DaVinci Resolve MCP work — grading, editing, conforming, delivery, media analysis, project lifecycle, review annotation, extension authoring, and .drp/.drt/.drx file work, live in a running Resolve or offline with none open. Load this for a map of the domain skills, the live-vs-offline servers, and the cross-cutting safety rules. The per-domain skills (resolve-color-grade / resolve-timeline-edit / resolve-timeline-conform-interchange / resolve-render-deliver / resolve-media-analysis / ...) carry the depth and self-trigger on their own descriptions; use this as the map, or when a task spans several domains.
---

# DaVinci Resolve MCP — Index

Orientation for any Resolve MCP task. This is the **map, not the depth** — each
domain skill below carries its own routing and triggers on its own `description`.
Open the one that matches, or use this when a task spans several domains. This
skill does not auto-load the others; it points at them.

## Two servers — compute offline, apply live

- **Live** — `davinci-resolve` (Python): drives a *running* Resolve via the
  scripting API.
- **Advanced / offline** — `davinci-resolve-advanced` (Node): authors
  `.drp`/`.drt`/`.drx` files and patches the project DB with **no Resolve open**.

Rule of thumb: compute grades, QC, and conform math offline; apply the result
live. Node never drives Resolve.

## Domains

| Task | Skill | Kernel | Any-MCP-client prompt |
|---|---|---|---|
| Grading, looks, shot match, LUT/CDL/DRX | `resolve-color-grade` | `color-grade-kernel.md` | `/color_grade_workflow` |
| Cutting, trimming, ranges, variants, changelist | `resolve-timeline-edit` | `timeline-edit-kernel.md` | `/timeline_edit_workflow` |
| Conform, relink, finishing QC, grade tracing | `resolve-timeline-conform-interchange` | `timeline-conform-interchange-kernel.md` | `/conform_workflow` |
| Render, deliverable QC, media/provenance | `resolve-render-deliver` | `render-deliver-kernel.md` | `/delivery_workflow` |
| Fusion comps (titles, MG, VFX) | `resolve-fusion-composition` | `fusion-composition-kernel.md` | `/fusion_workflow` |
| Audio / Fairlight (tracks, buses, loudness) | `resolve-audio-fairlight` | `audio-fairlight-kernel.md` | `/audio_workflow` |
| Media pool ingest / organize / multicam | `resolve-media-pool-ingest` | `media-pool-ingest-kernel.md` | `/media_pool_workflow` |
| Reading/analyzing source media | `resolve-media-analysis` | `media-analysis-guide.md` | `/analyze_media` |
| Brief-to-rendered-video pipeline (talking-head, montage) | `resolve-auto-edit` | `auto-edit-kernel.md` | `/auto_edit_workflow` |
| Multi-domain, resumable ingest-to-deliver conductor | `resolve-orchestration` | `orchestration-kernel.md` | `/orchestrate_workflow` |
| Project/database/archive lifecycle, presets | `resolve-project-lifecycle` | `project-lifecycle-kernel.md` | `/project_lifecycle_workflow` |
| Markers, flags, clip color, review reports | `resolve-review-annotation` | `review-annotation-kernel.md` | `/review_annotation_workflow` |
| Fuse/DCTL/script extension authoring | `resolve-extension-authoring` | `extension-authoring-kernel.md` | `/extension_authoring_workflow` |

## Less-common domains (no dedicated skill — go straight to the kernel/tool)

This has real coverage but low enough traffic that it routes through this
index rather than its own skill:

- **Pipeline (DB-as-truth)** — YAML-authored canonical project DB, staged runs
  with gates + provenance + drift: advanced `pipeline` tool →
  `resolve-advanced/README.md`.

## Cross-cutting rules (always)

- **Source media is sacred** (AGENTS.md): never modify, transcode, convert, proxy,
  relink, or derive source media unless explicitly asked. Outputs go to sidecars,
  scratch, or the analysis project root.
- **Frame-first color**: inspect Resolve-rendered frames before applying any
  grade/look/LUT/CDL/DRX, and preserve a recoverable grade version.
- **Guards refuse rather than fabricate** on the advanced server — read a
  "refused" message before retrying (usually wrong value space, log-encoded
  frames, missing media, or a missing optional dep; call the advanced
  `capabilities` tool).

## Deeper references

- `AGENTS.md` — canonical brief + the `## Domain Routing` index (all platforms).
- `docs/SKILL.md` — operating reference for both servers.
- `docs/kernels/` — per-action depth. `resolve-advanced/README.md` — offline catalog.
