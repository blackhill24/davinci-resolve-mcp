# live-server — Context (ICM Layer 2)

Python MCP that drives a **running** DaVinci Resolve via its scripting API.

## Routing table

<!-- Rows = tasks that actually recur here. Read/Skip = paths + purposes, not summaries. -->

| Task | Read | Skip | Skills / MCP |
|------|------|------|--------------|
| Add/change a compound tool | `server.py`, relevant `utils/<feature>.py` | `granular/` | domain skill in `.claude/skills/` |
| Add/change a granular tool | `resolve_mcp_server.py`, `granular/<domain>.py` | `server.py` | — |
| Media-analysis / vision work | `utils/media_analysis*.py`, `utils/deep_vision.py` | `granular/` | `docs/guides/media-analysis-guide.md` |
| Check what a live probe supports | `utils/*_live_probe.py` for the domain | codecs (advanced-server) | matching `docs/kernels/*-kernel.md` |
| Document a Resolve API limitation | `utils/api_truth.py` | — | run `scripts/gen_api_limitations.py` |
| Auto-edit pipeline (brief→render) | `utils/auto_edit.py`, `utils/cut_ir.py`, `utils/music_analysis.py` (ducking-mode ladder), `server.py` (auto_edit tool) | `granular/` | `.claude/skills/auto-edit.md` |
| Reverse-engineer a drt/drp encoding | `utils/drt_diff.py` (raw export-diff for ground-truth), `tests/live_auto_edit_ducking_probe.py` | Node `vendor/drp-format/diff.js` (semantic) | issue #14 |
| Invoke resolve-advanced (Node) ops | `utils/advanced_bridge.py` (drt/drp surgery in scratch; honest refuse w/o node) | `granular/` | `resolve-advanced/scripts/drp-bridge.mjs` |
| Safe temp/export paths | `utils/lut_paths.py`, `utils/safe path helpers` | — | — |
| Subtitle cue authoring / SRT import | `utils/subtitle_codec.py` (oracle-validated blob codec + styling), `server.py` (`import_srt`) | `granular/` | probes in `tests/` (#30) |

## Key files (only where the name doesn't say enough)

- `server.py` — compound server (preferred); `resolve_mcp_server.py` — granular entrypoint.
- `utils/api_truth.py` — source of truth for API gaps; `submit`-tagged entries regenerate
  `docs/reference/api-limitations.md` (a drift guard enforces regeneration).
- `utils/contracts.py` — shared action-dispatch envelope; reuse before adding abstractions.

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
