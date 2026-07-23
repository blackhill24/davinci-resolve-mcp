---
name: resolve-media-analysis
description: Media intelligence layer for the DaVinci Resolve MCP. Uses FFprobe, FFmpeg, and optionally Whisper to READ and ANALYZE source media — never modify, transcode, convert, or create derivatives. Provides the MCP with full context of what footage actually is so it can take informed actions within Resolve.
---

# Resolve Media Analysis — Claude Code Skill

Bridges media-intelligence *craft* to this repo's *tools*. The canonical guide
is authoritative — this skill is Claude Code-specific integration on top of it,
not a copy.

- **Full reference** — `docs/guides/media-analysis-guide.md` (commands, output
  format, examples, proactive warnings, the three setup questions). Read and
  follow it; do not duplicate its content here.
- **Live tool mechanics** — no dedicated kernel doc for this domain; see
  `docs/kernels/README.md` for where it sits relative to the others.

## The First Rule (non-negotiable, AGENTS.md)

**Never touch the source.** Your relationship to source media is READ-ONLY —
FFprobe/FFmpeg/Whisper read and report, they never transcode, convert, or
create derivatives of camera originals. See the guide's "The First Rule"
section for the full rationale.

## Two servers

| Job | Server | Tools |
|---|---|---|
| Plan/run an analysis pass and commit results **into a running** Resolve project | `davinci-resolve` (Python, live) | `media_analysis` (`capabilities`, `plan`, `analyze_file`/`clip`/`bin`/`project`, `commit_vision`, `publish_clip_metadata`, `get_report`) |
| Verify/inventory media with **no Resolve open** | `davinci-resolve-advanced` (Node) | `media` (`ingest_verify`, `media_inventory` — needs ffmpeg/ffprobe on PATH) |

## Workflow: analyze before acting

1. **Identify the media** — `media_pool_item.get_clip_property` (file path from
   a clip), `timeline_item.get_media_pool_item` (from a timeline item), or
   `media_storage.get_files` (from a directory).
2. **Check for existing analysis** — look for sidecar JSON before re-running.
3. **Analyze** — `media_analysis(action="capabilities")` first (tool
   detection: FFprobe required, FFmpeg/Whisper optional), then
   `analyze_file`/`clip`/`bin`/`project` at the requested depth
   (quick/standard/deep — ask if unset).
4. **Commit + act** — `commit_vision` writes the result into the registry;
   `publish_clip_metadata` surfaces it onto the Resolve clip; `get_report`
   reads it back. Only then act on the clip with full context.

## Granular (`--full`) equivalents

The `media_analysis` pipeline itself is kernel-only (`src/domains/media_analysis/actions.py`)
— no one-per-method granular twin. Adjacent granular building blocks:
`src/granular/media_pool_item.py` (`get_clip_metadata`/`set_clip_metadata`,
`transcribe_audio`, third-party metadata) for the metadata half of
`publish_clip_metadata`.

**Prompts** — `analyze_media` (file/clip/bin/sequence/project), plus
`analyze_and_propose_grade`, `verify_timeline_coverage`,
`open_and_analyze_selection` (all `src/server.py`) chain analysis into
grading/coverage workflows. **Resources** — `analysis://recent_reports`,
`capabilities://installed_tools` (FFprobe/FFmpeg/Whisper detection).

## Boundaries

Never create intermediate files that enter the media pipeline; analysis
artifacts are scratch-only, never committed alongside source media.
