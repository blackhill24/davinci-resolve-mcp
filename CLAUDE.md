# davinci-resolve-mcp — Router (ICM Layer 1)

> Map, not manual: read this, open exactly ONE workspace's CONTEXT.md, follow its routing
> table. Don't pre-read the repo. (Methodology: METHODOLOGY.md.)
> Docs reflect the repo at last sync; a session-start `[icm]` warning means code moved since
> then — trust structure, re-verify contents for load-bearing work.

**Rules live in `AGENTS.md`** (source-media safety, media-analysis defaults, frame-referenced
color work, domain routing, commands) — that stays canonical and is read by every agent.
This router only answers *where does the task live*. Also: `docs/SKILL.md` (operating guide),
`docs/process/release-process.md` (releases).

## What this project is

One npm package, two MCP servers giving AI assistants full DaVinci Resolve control:
- **live** (`src/`, Python) — drives a *running* Resolve via the official Scripting API.
  Full API coverage + guarded workflow helpers across 13 domains (see `src/CONTEXT.md`'s
  domain index): color/grade, timeline edit, conform/interchange, delivery QC, Fusion,
  audio/Fairlight, media pool/ingest, auto edit, media analysis, orchestration, extension
  authoring, project lifecycle, review annotation. Ships a local browser control panel.
- **advanced** (`resolve-advanced/`, Node) — authors `.drp`/`.drt`/`.drx` files + applies
  DB/XML edits (Fairlight routing, conform, offline-ref, group-grade read) with **no Resolve running**.

Two live surfaces: compound (grouped actions, default) vs full/granular (one tool per API
method). Per-domain depth: `AGENTS.md` → Domain Routing + `docs/kernels/`. Live tool counts
& stats live in `README.md` — they drift, so they are not tracked here.

## Workspaces

| Workspace | Path | Purpose | Open when |
|-----------|------|---------|-----------|
| live-server | `src/` | Python live MCP (compound + granular) driving Resolve | Editing Resolve-facing tools, utils, servers |
| dashboard | `src/dashboard/` | Local browser control-panel UI + backend (no Resolve required to serve it) | Editing the control panel's HTTP handler, panel UI, or its endpoints |
| advanced-server | `resolve-advanced/` | Node beyond-API file/DB authoring | Editing `.drp/.drt/.drx` codecs or offline tools |
| docs | `docs/` | Kernels, guides, reference, process | Writing/finding domain depth or process docs |
| tests | `tests/` | ~220 Python tests, mirrors `src/domains`+`core`+`dashboard` (offline + live) | Adding/running validation |
| tooling | `scripts/` (+ `install.py`) | Installer, audits, doc/rule generators | Running audits or regenerating generated docs |

## Folder map (top level only — each workspace maps its own depth)

```
src/  resolve-advanced/  docs/  tests/  scripts/  bin/  examples/
install.py  package.json  requirements.txt  AGENTS.md  README.md  CHANGELOG.md
.claude/skills/   (Claude Code domain skills; mirrors in .cursor/ .roo/ etc.)
```

## Naming conventions

<!-- A good name is a doc line that can never go stale. Prefer renaming to describing. -->
- `src/granular/<domain>.py` — one module per Resolve domain (timeline, media_pool, …)
- `src/domains/<domain>/utils/<feature>.py` (domain code); `src/core/<feature>.py` (cross-domain); `*_live_probe.py` = live-Resolve probes
- `tests/test_*.py` = offline unit; `tests/live_*.py` = require a running Resolve
- `resolve-advanced/server/*.mjs` + `server/tools/<domain>.mjs` = authoring tools; `vendor/<domain>/` = codecs
- `docs/kernels/<domain>-kernel.md` = per-domain action depth

## Standing rules (in force until the user says otherwise)

- Load only what a routing table points to; stay inside the chosen workspace.
- Keep names identical across router, contexts, and folders — routing depends on it.
- Generated files carry `BEGIN GENERATED` markers (e.g. AGENTS.md domain-routing,
  `docs/reference/api-limitations.md`) — edit the generator in `scripts/`, not the output.
- **Upkeep:** if you add, remove, or rename files, update that workspace's CONTEXT.md (and
  this router if the top-level layout changed) in the same session, then run from the root:
  `python3 .icm/drift-check.py --update`
- Keep this file ≲60 lines and each CONTEXT.md ≲40. Overflow → push detail down a layer.
