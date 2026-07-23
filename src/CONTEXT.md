# live-server — Context (ICM Layer 2)

Python MCP that drives a **running** DaVinci Resolve via its scripting API.
`utils/` (flat) is gone (restructure epic #52) — domain-specific code now lives
under `domains/<domain>/utils/`, cross-domain shared infra under `core/`.

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Add/change a compound tool | `server.py`, relevant `domains/<domain>/utils/<feature>.py` | `granular/` | domain skill in `.claude/skills/` |
| Add/change a granular tool | `resolve_mcp_server.py`, `granular/<domain>.py` | `server.py` | — |
| Find a domain's code / live probe | `domains/<domain>/CONTEXT.md` (stub until Phase 7 / #49), `domains/<domain>/utils/<domain>_live_probe.py` | other domains | matching `.claude/skills/`, `docs/kernels/*-kernel.md` |
| Cross-domain shared infra (governance, transport, brain DB, ledgers, process/platform helpers, background jobs, advanced-bridge) | `core/CONTEXT.md` (routing), `core/*.py` | domain `utils/`, `granular/` | — |
| Media-analysis / vision work | `domains/media_analysis/utils/media_analysis*.py`, `deep_vision.py` | `granular/` | `docs/guides/media-analysis-guide.md` |
| Document a Resolve API limitation | `core/api_truth.py` | — | run `scripts/gen_api_limitations.py` |
| Auto-edit pipeline (brief→render, all genres) | `domains/auto_edit/utils/auto_edit.py` (talking-head), `montage_edit.py` (montage — sibling decision layer, same CutList IR), `cut_ir.py`, `music_analysis.py` (ducking ladder + beat detection), `server.py` (auto_edit tool) | `granular/` | `.claude/skills/auto-edit.md` |
| Orchestrate conductor (ingest→deliver, resumable) | `domains/orchestration/utils/orchestrate.py`, `server.py` (orchestrate tool + `_orchestrate_*` helpers) | `granular/` | `.claude/skills/orchestration.md`, `docs/kernels/orchestration-kernel.md` |
| Reverse-engineer a drt/drp encoding | `domains/timeline_conform_interchange/utils/drt_diff.py` (raw export-diff for ground-truth), `tests/live_auto_edit_ducking_probe.py` | Node `vendor/drp-format/diff.js` (semantic) | issue #14 |
| Invoke resolve-advanced (Node) ops | `core/advanced_bridge.py` (drt/drp surgery in scratch; honest refuse w/o node — used by 4+ domains, lives in core despite the Phase 2 per-domain rule) | `granular/` | `resolve-advanced/scripts/drp-bridge.mjs` |
| Safe temp/export paths | `domains/color_grade/utils/lut_paths.py`, safe path helpers | — | — |
| Subtitle cue authoring / SRT import | `domains/audio_fairlight/utils/subtitle_codec.py` (oracle-validated blob codec + styling), `server.py` (`import_srt`) | `granular/` | probes in `tests/` (#30) |

## Key files (only where the name doesn't say enough)

- `server.py` — compound server (preferred); `resolve_mcp_server.py` — granular entrypoint.
- `core/api_truth.py` — source of truth for API gaps; `submit`-tagged entries regenerate
  `docs/reference/api-limitations.md` (a drift guard enforces regeneration).
- `core/contracts.py` — shared action-dispatch envelope; reuse before adding abstractions.

## Conventions & gotchas

- Prefer the compound server unless a task specifically needs granular tools.
- Follow existing action-dispatch + helper patterns; never invent ad hoc temp paths for
  files Resolve writes — use the repo's safe path helpers.
- Source-media safety in `AGENTS.md` is non-negotiable and applies to every tool here.
- Resolve's render queue REFUSES the system temp dir (`AddRenderJob` silently returns
  falsy) — render to a real media dir (e.g. `~/Videos`). `render build_proxies` defaults
  `require_temp_target=False` for this reason. ExportAudio=False dodges the headless stall.

> Upkeep: when files here change (add/remove/rename), fix the table + key files above in the
> same session, then run `python3 .icm/drift-check.py --update` from the root. Content-only
> edits usually need no doc change. Keep this file ≲40 lines.
