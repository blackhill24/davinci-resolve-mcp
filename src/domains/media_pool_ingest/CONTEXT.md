# Media Pool / Ingest — Context (ICM Layer 3)

Importing media, building multicam timelines, organizing/relinking clips,
normalizing metadata, or verifying/inventorying a card before ingest. Prompt:
`/media_pool_workflow`.

## Routing table

| Task | Read | Skip | Notes |
|------|------|------|-------|
| Media pool clip/folder ops, import, relink | `actions.py` (`media_pool`, `media_pool_item`, `folder`) | `granular/` | |
| Storage/mount inventory | `actions.py` (`media_storage`) | — | |
| Multicam timeline setup | `utils/multicam.py` | — | `build_multicam_setup_plan`; live-only — offline authoring of the UI-authored MULTICAM clip type was investigated and found not implementable (own `Sm2SequenceContainer`, issue #30 3.1.7) |
| Change-tracking / diff for media pool state | `utils/media_pool_changes.py` | — | |
| Live capability probe | `utils/media_pool_ingest_live_probe.py` | — | regenerates kernel counts |

## Key files (only where the name doesn't say enough)

- `actions.py` — 4 `@mcp.tool()`s: `media_storage`, `media_pool`, `folder`,
  `media_pool_item`. Also owns `_setup_multicam_timeline` (relocated here from
  `tool_kernel` during Phase 3 — multicam setup is media-pool-shaped, not a
  generic timeline helper).

## Conventions & gotchas

- Card/ingest verification and multicam are the two riskiest write paths here —
  follow source-media safety rules in root `AGENTS.md` before any mutation.
- Live: `media_pool`, `media_pool_item`, `folder`, `media_storage`.
  Offline (resolve-advanced): `media`.

> Upkeep: when files here change (add/remove/rename), fix the table above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root.
> Kernel: `docs/kernels/media-pool-ingest-kernel.md` + `docs/guides/multicam-setup-guide.md`.
> Skill: `.claude/skills/media-pool-ingest.md`. Keep this file ≲40 lines.
