# ADR 0001 — Domain Taxonomy for the Repo Restructure

Status: Accepted (2026-07-23). Part of the repo-restructure epic (#52), Phase 0 (#43).

## Context

Before any file moves, the restructure needs one canonical domain list — the
folder shape for `src/domains/<domain>/` depends on it. Two problems existed:

1. **10 routed domains vs 13 kernel files.** `AGENTS.md`'s generated routing
   table (`scripts/agent-rules/generate.mjs` `DOMAINS`) listed 10 domains, but
   `docs/kernels/` had 13 kernel files — `extension-authoring-kernel.md`,
   `project-lifecycle-kernel.md`, and `review-annotation-kernel.md` had no
   routed skill or slash-prompt entry, despite being live-validated,
   guarded, kernel-depth workflow layers of the same caliber as the routed 10
   (14, 41, and 44 supported actions respectively — see their kernel docs).
2. **Inconsistent naming.** The manifest's `id` field used short, sometimes
   arbitrary words that didn't match their own kernel filename — e.g.
   `id: 'delivery'` for `render-deliver-kernel.md`, `id: 'orchestrate'` for
   `orchestration-kernel.md`, `id: 'media-pool'` (hyphenated) for
   `media-pool-ingest-kernel.md`. `id` is presently inert outside the
   manifest (no other script consumes it), but Phase 1+ will use it as the
   `src/domains/<id>/` folder name, so it needs to be right now.

## Decision

**1. Promote all 3 orphan kernels to full routed domains.** Extension
authoring, project lifecycle, and review annotation each get a `DOMAINS`
entry, an `@mcp.prompt` slash workflow in `src/server.py`, and a
`.claude/skills/*.md` file, mirroring the existing 10. Rationale: they clear
the same bar as the routed domains (dedicated compound MCP tool, guarded
kernel actions, a live boundary probe) — there is no principled reason to
keep them second-class, and this restructure is meant to be the last one.

**2. Canonical domain name = the kernel-filename stem, underscored.** Where
the manifest `id` was a short abbreviation that diverged from its kernel's
descriptive filename, the `id` (and, for the same reason, the Claude Code
skill name) was widened to match the kernel filename exactly. Where `id` and
kernel filename already agreed (`auto-edit`, `media-analysis`, and the 3
promoted domains), only the hyphen→underscore folder-safety conversion
applied. This makes the kernel filename the single source of truth for a
domain's name everywhere except the MCP tool name and slash-prompt name
(`prompt` and `live`/MCP-tool-name fields), which stay as already-shipped,
short, user-facing identifiers — renaming those would break muscle memory
and external references (e.g. `/delivery_workflow`, the `orchestrate` tool)
for no naming-purity gain, since prompt names were never part of the
mismatch this ADR fixes.

**3. Folder names use underscores** (Python package constraint) — confirmed
literally, not just as a hyphen→underscore mechanical swap: `media-pool`
became `media_pool_ingest`, not `media_pool`.

## Final domain list (13)

| id (= folder name) | Skill | Kernel | Prompt | Renamed? |
|---|---|---|---|---|
| `color_grade` | `resolve-color-grade` | `color-grade-kernel.md` | `/color_grade_workflow` | id+skill (was `color`/`resolve-color`) |
| `timeline_edit` | `resolve-timeline-edit` | `timeline-edit-kernel.md` | `/timeline_edit_workflow` | id+skill (was `edit`/`resolve-edit`) |
| `timeline_conform_interchange` | `resolve-timeline-conform-interchange` | `timeline-conform-interchange-kernel.md` | `/conform_workflow` | id+skill (was `conform`/`resolve-conform`) |
| `render_deliver` | `resolve-render-deliver` | `render-deliver-kernel.md` | `/delivery_workflow` | id+skill (was `delivery`/`resolve-delivery`) |
| `fusion_composition` | `resolve-fusion-composition` | `fusion-composition-kernel.md` | `/fusion_workflow` | id+skill (was `fusion`/`resolve-fusion`) |
| `audio_fairlight` | `resolve-audio-fairlight` | `audio-fairlight-kernel.md` | `/audio_workflow` | id+skill (was `audio`/`resolve-audio`) |
| `media_pool_ingest` | `resolve-media-pool-ingest` | `media-pool-ingest-kernel.md` | `/media_pool_workflow` | id+skill (was `media-pool`/`resolve-media-pool`) |
| `auto_edit` | `resolve-auto-edit` | `auto-edit-kernel.md` | `/auto_edit_workflow` | id only (hyphen→underscore) |
| `media_analysis` | `resolve-media-analysis` | `README.md` (documented exception — no dedicated kernel file) | `/analyze_media` | id only (hyphen→underscore) |
| `orchestration` | `resolve-orchestration` | `orchestration-kernel.md` | `/orchestrate_workflow` | id+skill (was `orchestrate`/`resolve-orchestrate`) |
| `extension_authoring` | `resolve-extension-authoring` | `extension-authoring-kernel.md` | `/extension_authoring_workflow` | **new** (promoted orphan) |
| `project_lifecycle` | `resolve-project-lifecycle` | `project-lifecycle-kernel.md` | `/project_lifecycle_workflow` | **new** (promoted orphan) |
| `review_annotation` | `resolve-review-annotation` | `review-annotation-kernel.md` | `/review_annotation_workflow` | **new** (promoted orphan) |

`src/granular/` (Resolve-API-object taxonomy) stays untouched and separate —
it is 1:1 API coverage, not workflow-grouped, per the epic's locked-in
decisions.

## Growth criterion (for domains added after this restructure)

A workflow layer earns its own `src/domains/<name>/` folder, skill, and slash
prompt when **all** of:

1. It has a dedicated compound MCP tool (or a clearly-scoped slice of one)
   exposing guarded, kernel-level actions — not just raw API passthroughs.
2. It has a kernel doc under `docs/kernels/` with a live-validated boundary
   probe (a `tests/live_*_validation.py` harness with recorded
   supported/unsupported counts).
3. Its `when`-to-use description doesn't already fully overlap an existing
   domain's (if it does, the actions fold into that domain instead of
   forking a new one).

Anything short of that (a helper function, a single guarded action, a
one-off script) stays inside an existing domain's `utils/` rather than
forking a new domain — this is what keeps the folder shape from needing
another repo-wide reorg.

## Consequences

- `scripts/agent-rules/generate.mjs` `DOMAINS` is now 13 entries and remains
  the single source of truth; `AGENTS.md` and all per-platform rule files
  regenerate from it.
- 3 new `.claude/skills/*.md` files and 3 new `@mcp.prompt` slash workflows
  exist in `src/server.py` (`extension_authoring_workflow`,
  `project_lifecycle_workflow`, `review_annotation_workflow`).
- `src/domains/<id>/` exists for all 13 as empty skeleton directories
  (`.gitkeep` only) — no source files moved yet. Later phases (#44–#51)
  populate them.
- `tests/` gate: offline `pytest tests/` and
  `node scripts/agent-rules/generate.mjs --check` both green — this phase
  moved no runtime code, only manifest/doc/skill content and empty dirs.
