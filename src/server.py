#!/usr/bin/env python3
"""
DaVinci Resolve MCP Server (Compound Tools)

36 compound tools covering 100% of the DaVinci Resolve Scripting API (336 methods)
plus Fusion Fuse, DCTL, and Resolve-page Script authoring tools.
Each tool groups related operations via an 'action' parameter.

Usage:
    python src/server.py              # Start the MCP server
    python src/server.py --full       # Start the 341-tool granular server instead
"""

VERSION = "2.62.3"

import base64
import os
import sys
import json
import logging
import math
import platform
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zipfile
import zlib
from typing import Dict, Any, Optional, List, Tuple

if __name__ == "__main__":
    # Domain action modules below do `from src.server import mcp`. Run as a
    # script (as the MCP client launches it), this module's __name__ is
    # "__main__", not "src.server" — that self-import wouldn't find this
    # already-executing module and would re-exec this file from scratch,
    # circularly re-entering a domain module still mid-import. Registering
    # under the dotted name up front makes the later self-import resolve to
    # this same (progressively populated) module object instead.
    sys.modules.setdefault("src.server", sys.modules[__name__])

# ─── Path Setup ───────────────────────────────────────────────────────────────

current_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(current_dir)

# Add src and project to path
for p in [current_dir, project_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Platform-specific Resolve paths
from src.domains.color_grade.utils.cdl import normalize_cdl_payload
from src.core.mcp_stdio import run_fastmcp_stdio
from src.core.api_truth import lookup_api_truth, VERIFIED_ON as _API_TRUTH_VERIFIED_ON
from src.core.contracts import validate as _validate_params
from src.domains.project_lifecycle.utils.cloud_operations import cloud_sync_status_label
from src.domains.auto_edit.utils.cut_ir import build_cut_list as _build_cut_list
from src.core import launch_shim as _launch_shim
from src.core.page_lock import open_page_serialized as _open_page_serialized
from src.core.proc import preload_audit, resolve_spawn_env, safe_run, sanitized_spawn_env
from src.core.readback import verify_by_readback, verification_stats as _verification_stats
from src.core.update_check import (
    check_for_updates,
    clear_update_prompt_preferences,
    get_cached_update_status,
    get_update_channel,
    get_update_mode,
    ignore_update_version,
    set_update_mode,
    snooze_update_prompt,
    start_background_update_check,
    update_prompt_decision,
    update_state_path,
)
from src.domains.media_analysis.utils.caps_gating import (
    HOST_CHAT_PATHS_PROVIDER,
    HOST_CHAT_VISION_PROVIDERS,
    DEFAULT_VISION_ANALYSIS_PROMPT,
    VISION_SCHEMA_REFERENCE,
)
from src.domains.media_analysis.utils.clip_identity_registry import (
    resolve_output_root as resolve_media_analysis_output_root,
    short_hash,
    slugify,
)
from src.domains.media_analysis.utils.capabilities_and_planning import (
    build_plan as build_media_analysis_plan,
    detect_capabilities as detect_media_analysis_capabilities,
    install_guidance as media_analysis_install_guidance,
)
from src.domains.media_analysis.utils.execute_engine import (
    execute_plan_async as execute_media_analysis_plan_async,
    plan_requires_capabilities as media_analysis_plan_requires_capabilities,
)
from src.domains.media_analysis.utils.reports import (
    build_coverage_report as build_media_analysis_coverage_report,
    cleanup_artifacts as cleanup_media_analysis_artifacts,
    commit_visual_analysis,
    load_report as load_media_analysis_report,
    summarize_reports as summarize_media_analysis_reports,
)
from src.domains.media_analysis.utils.analysis_index_build import build_analysis_index
from src.domains.media_analysis.utils.analysis_index_query import (
    analysis_index_status,
    query_analysis_index,
)
from src.domains.media_analysis.utils.sync_detection import detect_sync_events_for_records as detect_media_sync_events
from src.core import actor_identity, background_jobs, resolve_busy
from src.core.resolve_busy import long_resolve_op
from src.domains.media_analysis.utils.media_analysis_jobs import (
    MEDIA_EXTENSIONS,
    batch_job_status as media_analysis_batch_job_status,
    cancel_batch_job as cancel_media_analysis_batch_job,
    create_batch_job as create_media_analysis_batch_job,
    list_batch_jobs as list_media_analysis_batch_jobs,
    resume_batch_job as resume_media_analysis_batch_job,
    run_batch_job_slice as run_media_analysis_batch_job_slice,
)
from src.core.platform import get_resolve_paths, get_resolve_plugin_paths
from src.domains.color_grade.utils.lut_paths import master_lut_dir, ensure_lut_in_master
from src.domains.extension_authoring.utils import fuse_templates, dctl_templates, script_templates
from src.domains.timeline_edit.utils.timeline_title_text import (
    candidate_title_property_keys as _candidate_title_property_keys,
    plain_to_minimal_styled_xml as _plain_to_minimal_styled_xml,
    timeline_item_get_property_map as _timeline_item_get_property_map,
)
from src.domains.media_pool_ingest.utils.multicam import build_multicam_setup_plan
from src.domains.timeline_conform_interchange.utils.timeline_xml import analyze_timeline_xml, sanitize_timeline_xml
from src.domains.fusion_composition.utils.fusion_group_settings import (
    FUSION_COMMIT_CHECKLIST,
    FUSION_GROUP_GUARDRAILS,
    default_backup_path,
    parse_setting_file,
    splice_inputs_block,
)
from src.core import analysis_runs as _analysis_runs
from src.core import brain_edits as _brain_edits
from src.domains.auto_edit.utils import edit_engine as _edit_engine_mod
from src.domains.auto_edit.utils import auto_edit as _auto_edit_mod
from src.domains.auto_edit.utils import montage_edit as _montage_edit_mod
from src.domains.orchestration.utils import orchestrate as _orchestrate_mod
from src.core import advanced_bridge as _advanced_bridge
from src.domains.auto_edit.utils import music_analysis as _music_analysis_mod
from src.domains.media_pool_ingest.utils import media_pool_changes as _media_pool_changes
from src.core import timeline_versioning as _timeline_versioning
from src.domains.project_lifecycle.utils import project_spec as _project_spec
from src.domains.project_lifecycle.utils import project_lint as _project_lint
from src.domains.timeline_edit.utils import clip_query as _clip_query
from src.core import destructive_hook as _destructive_hook
from src.core.destructive_hook import destructive_op as _destructive_op

# Extracted to src/core/tool_kernel.py + src/core/timeline_lookup.py (restructure epic #52, Phase 3 / #46).
from src.core.tool_kernel import (
    ERROR_CATEGORIES,
    ConfigParseError,
    _ACTION_HELP,
    _AI_GOVERNANCE_MODES,
    _AI_LEDGER_SESSION_ID,
    _CATEGORY_RETRYABLE_DEFAULT,
    _MEDIA_ANALYSIS_DEFAULT_PREFS,
    _MEDIA_ANALYSIS_MARKER_TYPE_ALIASES,
    _MEDIA_ANALYSIS_PREFS_ENV,
    _media_analysis_as_list,
    _media_analysis_bool,
    _normalize_analysis_persistence,
    _normalize_marker_colors,
    _normalize_metadata_overwrite_policy,
    _RETRYABLE_UNSET,
    _SETUP_CHOICE_CLEAR_VALUES,
    _TIMELINE_ACTIONS,
    _TINY_FONT,
    _media_analysis_effective_preferences,
    _read_media_analysis_preferences,
    _read_media_analysis_preferences_strict,
    _TOKEN_GATED_DESTRUCTIVE_ACTIONS,
    _TOOL_ACTIONS,
    _media_analysis_preferences_path,
    _resolve_audio_constant,
    _action_help,
    _activate_resolve_window,
    _ai_governance_check,
    _ai_governance_gate,
    _ai_governance_mode,
    _ai_governance_overrides,
    _ai_governance_preset,
    _ai_ledger_root,
    _ai_ledger_timed,
    _confirm_token_fingerprint,
    _confirm_token_gc,
    _confirm_token_required,
    _consume_confirm_token,
    _contact_sheet_png_bytes,
    _contact_sheet_sample_label,
    _draw_rect_rgb,
    _draw_tiny_text_rgb,
    _err,
    _filter_to_keys,
    _find_timeline_item_by_id,
    _first_param,
    _has_any_param,
    _is_truncated,
    _issue_confirm_token,
    _normalize_sampling_mode_default,
    _normalize_setup_choice,
    _normalize_setup_list,
    _normalize_timed_marker_choice,
    _normalize_vision_default,
    _normalize_yes_no_ask,
    _ok,
    _opt_number,
    _path_error,
    _png_chunk,
    _read_json_strict,
    _record_action_outcome,
    _resolve_enum_settings,
    _rgb_to_png_bytes,
    _run_maybe_background,
    _safe_int,
    _sampling_mode_choice_from_params,
    _send_resolve_keystroke_go_to_mark_in,
    _ser,
    _settings_diff,
    _setup_marker_limit,
    _setup_positive_int,
    _setup_text_key,
    _string_list_param,
    _thumbnail_data_to_png_bytes,
    _thumbnail_raw_rgb,
    _timed_marker_choice_from_params,
    _timeline_items_by_ids,
    _timeline_items_by_ids_report,
    _timeline_resolve_item_optional,
    _unknown,
    _write_media_analysis_preferences,
)
from src.core.envelope import (
    _callable_method_names,
    _check,
    _has_method,
    _requires_method,
    _safe_clip_call,
    _safe_get_property,
)
from src.core.timeline_lookup import (
    _clip_file_size,
    _clip_name,
    _clip_summaries,
    _clips_from_params,
    _coerce_item_list,
    _collect_timeline_items_in_range,
    _current_timeline_frame_id,
    _range_frames_from_params,
    _range_track_indices,
    _range_track_types,
    _find_clip,
    _find_clip_with_parent,
    _find_timeline_by_id,
    _find_timeline_by_name,
    _frame_id_to_timecode,
    _frame_int,
    _get_item,
    _get_mp,
    _get_selected_timeline_items,
    _get_tl,
    _metadata_write_field_for_field,
    _project_name_and_id,
    _project_summary,
    _safe_timeline_item_id,
    _safe_timeline_item_name,
    _timecode_to_frame_id,
    _timeline_by_selector,
    _timeline_fps,
    _timeline_frame_id_to_timecode,
    _timeline_item_duration,
    _timeline_item_ids,
    _timeline_item_media_pool_item,
    _timeline_item_probe,
    _timeline_item_source_start,
    _timeline_item_summary,
    _timeline_item_track_info,
    _timeline_start_frame,
    _timeline_timecode_to_frame_id,
    _timeline_track_count,
    _track_items_sorted,
    _track_selector,
)

paths = get_resolve_paths()
RESOLVE_API_PATH = paths["api_path"]
RESOLVE_LIB_PATH = paths["lib_path"]
RESOLVE_MODULES_PATH = paths["modules_path"]

os.environ["RESOLVE_SCRIPT_API"] = RESOLVE_API_PATH
os.environ["RESOLVE_SCRIPT_LIB"] = RESOLVE_LIB_PATH

if RESOLVE_MODULES_PATH not in sys.path:
    sys.path.append(RESOLVE_MODULES_PATH)

# ─── Logging ──────────────────────────────────────────────────────────────────

log_dir = os.path.join(project_dir, "logs")
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(os.path.join(log_dir, "server.log"))]
)
logger = logging.getLogger("resolve-mcp")

# ─── MCP Server ───────────────────────────────────────────────────────────────

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp import types as mcp_types
mcp = FastMCP(
    "DaVinciResolveMCP",
    instructions=(
        "DaVinci Resolve MCP Server — controls Resolve via its Scripting API. "
        "Tools automatically launch Resolve if it is not running (may take up to 60s on first call). "
        "If a tool returns a connection error, Resolve Studio may not be installed or external scripting is disabled."
    ),
)

READ_ONLY_TOOL = mcp_types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_TOOL = mcp_types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
IDEMPOTENT_WRITE_TOOL = mcp_types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
DESTRUCTIVE_TOOL = mcp_types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
EXTERNAL_READ_TOOL = mcp_types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
EXTERNAL_WRITE_TOOL = mcp_types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
EXTERNAL_DESTRUCTIVE_TOOL = mcp_types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


def _annotations_for_tool_name(tool_name: str) -> mcp_types.ToolAnnotations:
    """Infer conservative MCP client-safety hints for compound action tools."""
    name = (tool_name or "").lower()
    external_tools = (
        "layout_presets",
        "render_presets",
        "render",
        "media_storage",
        "media_pool",
        "folder",
        "media_pool_item",
        "gallery_stills",
        "fuse_plugin",
        "dctl",
        "script_plugin",
    )
    destructive_tools = (
        "resolve_control",
        "project_manager",
        "project_manager_folders",
        "project_manager_cloud",
        "project_manager_database",
        "project_settings",
        "timeline",
        "timeline_markers",
        "timeline_ai",
        "timeline_item",
        "timeline_item_markers",
        "timeline_item_fusion",
        "timeline_item_color",
        "timeline_item_takes",
        "timeline_versioning",
        "gallery",
        "graph",
        "color_group",
        "fusion_comp",
    )
    if name == "media_analysis":
        return EXTERNAL_WRITE_TOOL
    if name in external_tools:
        return EXTERNAL_DESTRUCTIVE_TOOL
    if name in destructive_tools:
        return DESTRUCTIVE_TOOL
    return WRITE_TOOL


_original_mcp_tool = mcp.tool


def _tool_with_default_annotations(
    name=None,
    title=None,
    description=None,
    annotations=None,
    icons=None,
    meta=None,
    structured_output=None,
):
    """Default unannotated compound tools to explicit MCP safety hints."""

    def decorator(func):
        tool_name = name or getattr(func, "__name__", "")
        return _original_mcp_tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations or _annotations_for_tool_name(tool_name),
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )(func)

    return decorator


mcp.tool = _tool_with_default_annotations


@mcp.prompt()
def davinci_resolve_workflow() -> str:
    """Recommended agent workflow for this DaVinci Resolve MCP server."""
    return """Use this DaVinci Resolve MCP server as a guarded post-production control surface.

Core pattern:
- Prefer the 32 compound tools and their action names over raw scripting.
- Start by probing state: resolve_control.get_version/get_page, project_manager.get_current, timeline.get_current, and media_pool.probe_media_pool.
- Before mutating timelines, media pools, render settings, grades, projects, databases, or extensions, prefer the matching probe, capabilities, boundary_report, safe_*, or dry_run action when one exists.
- Preserve source media integrity. Never transcode, proxy, rewrite, move, rename, or create derivatives of source media unless the user explicitly asks. Analysis output belongs in sidecars or analysis directories.
- Do not silently downgrade media analysis. Source-safe does not mean no visuals, no transcription, no persistence, no metadata, or no markers. For Resolve-target media analysis, keep visual analysis, transcription, persisted artifacts, metadata writeback, and Media Pool marker writeback enabled unless the user explicitly opts out. Vision uses host_chat_paths by default: analyze actions return absolute frame_paths in a deferred payload; you must read those frames as images and call media_analysis(action="commit_vision", ...) to finalize. Not completing commit_vision leaves the analysis in pending_host_vision_analysis — that is a failure mode, not a success.

Visual feedback:
- For the current Color-page frame, use timeline_markers(action="get_thumbnail_image") when the client can display MCP images.
- Use timeline_markers(action="get_thumbnail") when raw Resolve thumbnail data is needed for tooling.
- Use project_settings(action="export_frame_as_still") only when a file export is explicitly useful, and write to a temp/stills location rather than near source media.

High-value workflows:
- Media analysis: use the analyze_media prompt or media_analysis.capabilities/install_guidance, then analyze file/clip/bin/project targets directly with persisted artifacts, host_chat_paths visual analysis (finish each clip with media_analysis(action="commit_vision", ...)), transcription, metadata writeback, and Media Pool marker writeback enabled by default unless the user opts out.
- Timeline editing: use timeline.probe_edit_kernel_item, timeline.title_property_scan / timeline.set_title_text for Edit-page Text+ keys, duplicate_clips/copy_clips/move_clips, copy_range/overwrite_range/lift_range, and detect_gaps_overlaps.
- Media ingest: use media_pool.ingest_capabilities, safe_import_media/safe_import_sequence, organize_clips, normalize_metadata, and relink planning actions.
- Color: use timeline_item_color.grade_boundary_report, probe_node_graph, safe_set_cdl, safe_apply_drx, grade_version_snapshot/restore, and gallery/color-group capability actions.
- Fusion: use fusion_comp.fusion_boundary_report, probe_fusion_comp, safe_add_tool, safe_set_inputs, and safe_connect_tools.
- Audio/Fairlight: use timeline.fairlight_boundary_report, probe_audio_track/item, voice_isolation_capabilities, safe_auto_sync_audio, and subtitle_generation_probe.
- Render/deliver: use render.export_render_boundary_report, validate_render_settings, safe_set_render_settings, prepare_render_job, and safe_quick_export.
- Project lifecycle: use project_manager.project_boundary_report and safe project/database/archive actions. Keep destructive work scoped to disposable _mcp_ projects unless the user explicitly approves otherwise.
- Extension authoring: use script_plugin.extension_boundary_report and safe_install_extension/safe_remove_extension. Respect refresh/restart requirements.

Editorial improvements + versioning (C6 — always on for destructive timeline ops):
- Every destructive timeline op (compound, captions, ripple delete, gap close, retime, marker batch, take swap, color grade, etc.) auto-archives the working timeline to the `Archive` bin BEFORE the mutation runs. You don't need to call archive yourself.
- For multi-step editorial operations, call `timeline_versioning(action="begin_run", label="<short description>", initiator="brain.chat")` first. Every subsequent destructive call within that run will reuse the same `analysis_run_id` and produce ONE archived predecessor, not N. Pair with `timeline_versioning(action="end_run")` to write a cumulative per-metric summary.
- When you're making a deliberate edit you can measure, pass `metric`, `direction`, and `rationale` in the action params. The hook captures the live `before_value` and `after_value` from the timeline and writes a `brain_edits` row with the delta — that's the measurement substrate for tuning. Supported metrics: `duration_seconds`, `avg_performance_score`, `clip_count`, `gap_count`, `total_gap_seconds`, `redundancy_score`. `direction` is `increase` | `decrease` | `target_value`.
- For catastrophic ops (`timeline.delete_timelines`, `timeline.delete_track`, `timeline.delete_clips(ripple=True)`), strict mode is on by default — the call REFUSES to run if the pre-mutation archive can't be created. Pass `strict=true` on any destructive op to opt in to the same protection.
- Read-only inspection (list, get_current, get_property, etc.) bypasses versioning entirely — no setup needed.
- Inspect history via `timeline_versioning(action="get_history", timeline_name=…)`, `list_versions`, `diff_versions(from_version, to_version)`, or `list_runs`. Roll back via `timeline_versioning(action="rollback", timeline_name=…, version=…)`.

For one-off scripting:
- Prefer script_plugin(action="run_inline") over arbitrary persistent code changes. Use it to inspect Resolve state, then move durable behavior into guarded compound actions when it proves valuable.
"""


@mcp.prompt(
    name="analyze_media",
    title="Analyze Media",
    description="Run a source-safe DaVinci Resolve media-analysis workflow for a file, selected clip, bin, sequence/timeline, or whole project.",
)
def analyze_media(
    target: str = "project",
    depth: str = "standard",
    finished_video: bool = False,
    include_visuals: bool = True,
    include_transcription: bool = True,
    persist: bool = True,
) -> str:
    """Slash-command style prompt for guided media analysis."""
    return f"""Analyze Resolve media with the DaVinci Resolve MCP attached.

Requested shape:
- target: {target}
- depth: {depth}
- finished_video: {finished_video}
- include_visuals: {include_visuals}
- include_transcription: {include_transcription}
- persist: {persist}

Workflow:
1. Confirm the MCP is live with resolve_control(action="get_version") and project_manager(action="get_current").
2. Call media_analysis(action="capabilities") and media_analysis(action="install_guidance"). Do not install anything automatically.
3. Resolve the target:
   - "project": use media_analysis(action="analyze_project").
   - "selected" or "selected clip": use media_analysis(action="analyze_clip", params={{"selected": true}}).
   - "timeline" or "sequence": use media_analysis(action="analyze_sequence", params={{"track_types": ["video", "audio"]}}).
   - "bin:<path>": use media_analysis(action="analyze_bin", params={{"path": "<path>", "recursive": true}}).
   - An absolute file path: use media_analysis(action="analyze_file", params={{"path": "<path>"}}).
4. Do not silently downgrade media analysis. Do not add include_visuals=false, include_transcription=false, publish_metadata=false, timed_markers="no", session_only=true, or dry_run=true unless the user explicitly asks for that opt-out or the target is a raw file path that cannot receive Resolve project writeback.
5. Do a quick memory check, then keep moving:
   - media_analysis(action="summarize") to find existing reports for the active project.
   - media_analysis(action="get_report") when a manifest/report already exists.
   - timeline(action="list"), timeline(action="get_current"), timeline(action="probe_timeline_structure"), and timeline_markers(action="get_all") when an edit already exists.
   The planner also reuses Resolve-published `davinci_resolve_mcp.analysis_report_path` provenance, the global analysis registry, and bounded related project-version roots by default; do not disable that unless the user asks for an isolated fresh run.
   If execution returns status="reuse_blocked", do not rerun analysis silently; restore the referenced report or use force_refresh=true only when the user explicitly wants a fresh read.
   Reuse existing evidence only when it already satisfies the requested technical, visual, transcription, and marker/writeback needs; otherwise run fresh analysis.
6. Execute analysis by default. Use dry_run=true only when the user asks for a preview or when you are intentionally staging a very large batch before a slice-based job.
7. Persist inspectable reports and artifacts by default under davinci-resolve-mcp-analysis. Use session_only=true only when the user explicitly asks for scratch results.
8. Visual analysis and transcription default on. If include_visuals is true, request vision={{"enabled": true, "provider": "host_chat_paths"}}. The analyze tool will respond with a deferred payload containing absolute frame_paths and a JSON schema; read each frame as a local image, produce JSON per the schema, then call media_analysis(action="commit_vision", params={{"clip_id": "...", "visual": <your JSON>, "vision_token": "..."}}) per clip. Metadata writeback and Media Pool clip markers publish automatically when commit_vision finalizes each clip. If include_transcription is true, request transcription={{"enabled": true, "allow_model_download": true}} so the configured local transcription backend can run.
9. If you cannot read the frame_paths as images, the visual layer remains pending_host_vision_analysis — surface that to the user; do not call the analysis complete unless they explicitly opt out with include_visuals=false.
10. Executed Resolve-target analysis writes metadata and source-time Media Pool clip markers by default after commit_vision finalizes. Pass publish_metadata=false, timed_markers="no", or dry_run=true only when the user asks to avoid Resolve project writeback.
11. If the task is about an existing edit, markers, or a finished video, call media_analysis(action="review_timeline_markers", params={{"vision": {{"enabled": {str(include_visuals).lower()}, "provider": "host_chat_paths"}}}}) when marker/frame alignment affects the decision. The response will include image_path; read it and answer inline (no commit step).
12. Timeline creation supports if_exists: use "reuse" for idempotent reruns, "version" for alternate cuts, and "fail" when duplicates indicate a workflow problem.

Recommended execution params:
{{
  "dry_run": false,
  "depth": "{depth}",
  "session_only": false,
  "persist": {str(persist).lower()},
  "publish_metadata": true,
  "timed_markers": "yes",
  "reuse_existing": true,
  "force_refresh": false,
  "reuse_policy": "compatible",
  "search_related_project_roots": true,
  "max_analysis_frames": 8,
  "vision": {{"enabled": {str(include_visuals).lower()}, "provider": "host_chat_paths"}},
  "transcription": {{"enabled": {str(include_transcription).lower()}, "allow_model_download": true}}
}}

Interpretation rules learned from live Resolve sessions:
- Preserve source media integrity. Do not modify, transcode, proxy, rename, move, or write beside source media.
- Users can opt out of in-chat visual analysis by setting include_visuals=false. When opted out, do not read frame_paths and do not call commit_vision.
- Users can opt out of transcription with include_transcription=false and can opt out of Resolve project writeback with publish_metadata=false or timed_markers="no".
- Use the project-owned editorial craft reference in docs/guides/editorial-decision-guide.md; do not rely on personal or external editor skills.
- When the user asks for cutting, pacing, story structure, suspense, comedy, or tonal reframing, use the editor craft lens: emotion and story outrank coverage; sound leads picture; find blink points and decisive frames; cut on reaction when meaning matters.
- Treat scene/cut detection as guardrails, not story. If the source is a finished video, use black/flash ranges and likely cut points to avoid bad edit regions, but let transcript, sound, and complete thoughts drive editorial decisions.
- For short-form edit recommendations, build an audio-first spine: premise, setup, turn, and button. Sacrifice visual variety when clarity or the joke needs it.
- After a rough variant is assembled, verify it frame-by-frame: probe gaps/overlaps, inspect thumbnails at markers and cut points, compare marker intent against what Resolve actually shows, then revise marker names/source ranges if the image contradicts the plan.
- Watch for Resolve timeline start-frame offsets. Positioned appends should anchor record_frame to the timeline start frame, often 108000 for 01:00:00:00.
- Summarize results as editor-usable intelligence: technical state, warnings, motion/variance, visual content, transcript/sound notes, avoid ranges, best moments, and concrete next actions.

When finished, report exactly which media_analysis call was made, whether artifacts were persisted, whether commit_vision was called for each clip with pending vision, whether metadata/markers were written back, and whether transcription succeeded."""


# ─── F2 — MCP Prompts for common multi-step workflows ──────────────────────────
# (agentic-flow improvement F2: structured confirm-token envelope.)
# These surface as slash commands in the host UI (e.g. /davinci-resolve:match-bin-to-hero).


@mcp.prompt(
    name="analyze_and_propose_grade",
    title="Analyze + Propose Grade",
    description="Analyze the hero clip, build a grade_evidence_base, and stage a propose_grade artifact.",
)
def analyze_and_propose_grade(hero_clip_id: str) -> str:
    return f"""Run the hero-clip color pipeline end-to-end.

1. Call media_analysis(action="analyze_clip", params={{"clip_id": "{hero_clip_id}"}}).
2. Read the resulting `analysis_signature` and confirm the clip has a current vision report.
3. Call timeline_item_color(action="grade_evidence_base", params={{"min_source_trust": "high"}}).
4. Lead your reply with the returned `evidence_base` line.
5. Call timeline_item_color(action="propose_grade", params={{
     "target_id": "<hero timeline item id>",
     "evidence_base": "<evidence_base line>",
     "frame_paths": [<frames from the analysis>],
     "operation_class": "direct",
     "cdl_delta_or_artifact": {{"cdl": {{...}}}},
     "execute": false
   }}).
6. Show the user the returned plan_id + preview_path. Wait for explicit confirmation
   before re-calling with execute=true. Never auto-execute the proposal."""


@mcp.prompt(
    name="match_bin_to_hero",
    title="Match Bin to Hero",
    description="Use bulk_match_to_hero to stage a per-target grade across a bin, dry-run first.",
)
def match_bin_to_hero(hero_clip_id: str, method: str = "copy_grade") -> str:
    return f"""Match a bin's clips to a hero shot using bulk_match_to_hero.

1. Run media_analysis(action="analyze_bin", params={{"recursive": true}}) on the
   current bin so each target has a current vision report.
2. Call timeline_item_color(action="grade_evidence_base", params={{
     "target": {{"target_id": "{hero_clip_id}"}}, "min_source_trust": "high"
   }}).
3. Lead your reply with the returned `evidence_base` line.
4. Call timeline_item_color(action="bulk_match_to_hero", params={{
     "hero_id": "{hero_clip_id}",
     "target_ids": [<bin clips you analyzed>],
     "method": "{method}",
     "min_source_trust": "high",
     "dry_run": true
   }}).
5. Show the user the per-target proposals and any `blocked` entries.
6. On confirmation, re-call with dry_run=false and the issued confirm_token."""


@mcp.prompt(
    name="verify_timeline_coverage",
    title="Verify Timeline Coverage",
    description="Run analyze_sequence on the current timeline and summarize coverage gaps.",
)
def verify_timeline_coverage() -> str:
    return """Verify the current timeline has full analysis coverage.

1. Confirm with timeline(action="get_current") that a timeline is open.
2. Call media_analysis(action="analyze_sequence", params={"track_types": ["video"]}).
3. Inspect the returned manifest: clip_count vs successful_clip_count vs failed_clip_count.
4. If partial_success is true, surface failed_clip_ids and recommend retry-only-failed.
5. Call media_analysis(action="summarize") and read the `provenance.source_reports`
   list to verify every contributing clip has a current analysis_signature.
6. Report any clips in `provenance.missing_reports` as coverage gaps."""


@mcp.prompt(
    name="open_and_analyze_selection",
    title="Open Panel + Analyze Selection",
    description="Launch the control panel and analyze the current Resolve clip selection.",
)
def open_and_analyze_selection() -> str:
    return """Open the analysis control panel and analyze the selected clips.

1. Call resolve_control(action="open_control_panel"). Surface the returned URL.
2. Call media_analysis(action="analyze_clip", params={"selected": true}).
3. Report manifest.successful_clip_count and any vision_pending count.
4. If vision_pending > 0, walk each pending clip's frame_paths and call
   media_analysis(action="commit_vision") per the host_chat_paths protocol.
5. Direct the user to the control panel URL for inline review of results."""


@mcp.prompt(
    name="prep_color_handoff",
    title="Prep Color Handoff",
    description="Generate a coverage + provenance + render-presets handoff packet for online/color.",
)
def prep_color_handoff(output_dir: str = "") -> str:
    target_dir = output_dir or "~/Documents/davinci-resolve-mcp-analysis/handoff"
    return f"""Prepare a color/online handoff packet.

1. Call media_analysis(action="summarize") and capture provenance.source_reports.
2. Call render(action="list_render_presets") and timeline(action="probe_timeline_structure").
3. Call timeline_versioning(action="list_versions") for the current timeline.
4. Write a handoff manifest to: {target_dir}/handoff_<timestamp>.json containing:
   - provenance source_reports list (clip signatures + paths)
   - render preset names
   - timeline version list
   - any caps usage at the time of handoff (media_analysis.get_caps)
5. Surface the manifest path back to the user. Do not write beside source media."""


# ─── Per-domain workflow routers ───────────────────────────────────────────────
# Cross-platform depth: these surface as slash commands in EVERY MCP client
# (Codex, Cursor, Copilot, Continue, Claude Desktop, …), so per-domain routing is
# not limited to Claude Code's .claude/skills/. They mirror the repo skills of the
# same name; keep them in sync with docs/kernels/*.


@mcp.prompt(
    name="color_grade_workflow",
    title="Color / Grade Workflow",
    description="Route a grading/look/shot-match task across the live color tools and the offline advanced grading/QC catalog.",
)
def color_grade_workflow() -> str:
    return """Color / grade work spans two servers: the live Python server drives a
running Resolve; the advanced (offline) server computes grades from frames and
reads/writes .drx/.drp with no Resolve open. Compute offline, apply live.

Frame-first rule (non-negotiable): before applying any grade, look, shot match,
LUT, CDL, DRX, or copied grade, inspect representative Resolve-rendered frames
(thumbnails/contact sheet/Gallery stills/marker frames) and compare
bypass/current/after at matched timecodes; restore prior version/node state after
a temporary bypass. Never grade from metadata or a style label alone. Preserve a
recoverable grade version.

- Live: timeline_item_color.grade_boundary_report / probe_node_graph /
  safe_set_cdl / safe_apply_drx / grade_version_snapshot|restore; graph;
  gallery_stills; color_group.
- Offline (advanced server `drx` actions): match_to_reference, level_clips,
  skin_match, shot_match, white_balance_match, contrast_normalize,
  saturation_match, black_balance, cdl_io, grade_transfer, lut_apply,
  author_look|carry_look, scope_read|intent_tags|gamut_legal, verify_grade.
- Value space: drx generate/merge default to space='ui' (panel units, saturation
  0-100 neutral 50); a `warnings` array means a hue curve rendered FLAT — surface
  it. safe_apply_drx defaults to V1/item0 — always pass explicit track/item indices
  and back up a still first; ApplyGradeFromDRX replaces the graph.
- Relayout ("Cleanup Node Graph", no UI API): grab still -> drx relayout ->
  graph.reset_all_grades -> safe_apply_drx (a same-structure apply keeps the old
  layout). Whole project: project_db.relayout_node_graphs.

Depth: docs/kernels/color-grade-kernel.md, docs/guides/color-decision-guide.md.
The advanced grading catalog needs `sharp`; call the advanced `capabilities` tool.
Never modify/transcode/derive source media (see AGENTS.md)."""


@mcp.prompt(
    name="timeline_edit_workflow",
    title="Timeline Edit Workflow",
    description="Route a cutting/trimming/restructuring/changelist task across the live edit tools and the offline editorial tools.",
)
def timeline_edit_workflow() -> str:
    return """Editing spans two servers: the live Python server restructures a
running timeline; the advanced (offline) server authors/diffs .drt files and
reasons over editorial interchange with no Resolve open.

- Live: timeline duplicate_clips/copy_clips/move_clips (include_linked carries
  linked audio); copy_range/duplicate_range/overwrite_range/lift_range (no public
  razor/split — partial overlaps blocked unless allow_partial_item_delete);
  copy_properties (scope with a group list); edit_engine (selects/tighten/swap,
  plan -> confirm -> execute; tighten can carry audio via keep_ranges/include_audio).
- Offline (advanced server `editorial` actions): parse_interchange (EDL/OTIO/XMEML;
  AAF = honest refuse), turnover_changelist (moved/retimed/replaced/new/gone with
  timing guards), conform_manifest, marker_roundtrip; `drt` for timeline files.

Use the offline tools to answer "what changed between v3 and v4" without opening
either cut. Edits reference existing Media Pool items — never transcode/proxy/derive
source media (see AGENTS.md).

Depth: docs/kernels/timeline-edit-kernel.md, docs/guides/editorial-decision-guide.md."""


@mcp.prompt(
    name="auto_edit_workflow",
    title="Auto Edit Workflow",
    description="Route a brief-to-rendered-video task (talking head / interview) through the autonomous auto_edit pipeline with its single approval checkpoint.",
)
def auto_edit_workflow() -> str:
    return """The auto_edit pipeline turns a brief (source files, optional music,
target length, title) into a rendered video with ONE human checkpoint.

- start_brief(files, music?, target_duration_seconds?, title_text?) — validates
  media (exist + ffprobe), scaffolds Footage/Music bins, kicks the analysis
  batch. Poll brief_status; complete commit_vision handoffs while it runs.
- plan_cut(brief_id) — word-level Pass-1 (fillers/false starts; cue fallback),
  dead-air windows, duration fit, jump-cut smoothing (b-roll via similarity,
  else punch-in), music gain from ebur128 loudness. Returns a markdown summary.
- Show the summary verbatim; iterate with revise_cut (reorder/drop/keep/title).
- approve_cut(plan_id, music_bed_consent?) — THE checkpoint (confirm-token
  gated). The preview carries the summary + the music-bed-render consent line;
  a ducked bed is a DERIVATIVE and renders only with explicit consent.
- build_timeline(plan_id) — append-rebuild (V1 speech + mirrored audio, V2
  b-roll, punch-in zoom, A2 music). Revisions rebuild; never hand-patch.
- finish(plan_id, grade?, subtitles?, render?) — grade/subtitles/validated
  render; verify the reported output_path exists.

Depth: docs/kernels/auto-edit-kernel.md, docs/guides/editorial-decision-guide.md
(“Auto-Edit Heuristics”). Source media is READ-ONLY (see AGENTS.md)."""


@mcp.prompt(
    name="orchestrate_workflow",
    title="Orchestrate Workflow",
    description="Drive a resumable ingest-to-deliver job through the orchestrate conductor — sequences the domain tools across ten stages and survives a context reset mid-job.",
)
def orchestrate_workflow() -> str:
    return """orchestrate sequences the domain tools (media_pool, media_analysis,
auto_edit, timeline, timeline_item_color, render, timeline_markers) across a
durable job record. Its only reason to exist as a tool rather than replaying
the same calls from prose is surviving context death mid-job.

- start_job(files, music?, target_duration_seconds?, genre?, deliverable?,
  options?, stages?, include_fusion?, gates?) — infers the stage manifest,
  marks intake done, persists, acquires the lease.
- job_status(job_id) — cursor + manifest; read-only, safe anytime.
- run_stage(job_id) (defaults to cursor) delegates one stage and reports
  waiting_on rather than erroring when it needs you: talking-head edit
  kicks/polls the brief+plan (approve_gate(gate="G1") ADOPTS
  auto_edit.approve_cut verbatim); any other genre pauses for a
  bring-your-own-timeline cut (byo_ready=true to confirm); grade/audio
  no-op unless options.grade/options.audio are given; deliver requires G3
  approved, then a validated render (special-cased — no rollback, leans on
  Resolve's own render-queue resume on failure).
- approve_gate(job_id, gate="G2", vision_assessment, preview_frame_path) —
  the post-grade checkpoint. ALWAYS a real look at the rendered frame,
  never blind, never fabricated.
- A failed reversible stage does not auto-rollback: rollback_stage(job_id,
  stage), then run_stage again.
- After a gap: check_resume(job_id) before trusting cursor; on
  drifted=true, force_replan_stage(job_id, stage).
- finish_job(job_id) once every stage is done — verifies output_path,
  purges namespaced snapshots.

Depth: docs/kernels/orchestration-kernel.md. Zero decision logic belongs in
prose — sequencing, drift checks, and gates live in the tool. Source media
is READ-ONLY (see AGENTS.md)."""


@mcp.prompt(
    name="conform_workflow",
    title="Conform / Interchange Workflow",
    description="Route a conform/relink/finishing-QC/grade-trace task across the live conform tools and the offline conform QC engine.",
)
def conform_workflow() -> str:
    return """Conforming spans two servers: the live Python server imports/relinks/
compares a running conform; the advanced (offline) server does frame-oracle QC,
reverse-subclip repair, lineage, and grade tracing with no Resolve open.

- Live: timeline probe_timeline_structure / detect_gaps_overlaps /
  source_range_report / conform_boundary_report; export_timeline_checked /
  import_timeline_checked (drt is the only lossless project-native round-trip;
  EDL/FCPXML drop relationships); detect_missing_media -> build_relink_plan
  (read-only, bounded) -> media_pool.safe_relink with approved paths only.
- XML import via the scripting API goes OFFLINE (missing-media/generators abort);
  use import_timeline_checked with media sanitize (FCP7/FCPXML), then exact-path
  relink; restart a running MCP server to pick up the sanitize fix.
- Offline (advanced server): conform (frame-oracle QC — catches wrong-but-similar
  relinks; reversed source_start = masterFrames-1-endoffset; lineage store+diff;
  per-cut frame QC vs reference render), color_trace (carry grades across a
  re-conform), offline_ref (<OfflineClip> patch — no scripting API), editorial,
  drt, project_db.
- .drt for Resolve 19.1.3: set DbPrjVer 17 -> 16. project_db patches need the
  project CLOSED + iConfirmProjectClosed:true, auto-backup + read-back verify, and
  a full QUIT+relaunch of Resolve before the change is visible.
- Inside an `orchestrate` job, `conform.fix_reverse_clip` / `offline_ref`'s
  LIVE DB link/unlink need this same closed-project dance — call
  `orchestrate request_offline_op` to park the stage instead of hand-rolling
  the quit/relaunch sequence; `resolve_offline_op` resumes it afterward.

Depth: docs/kernels/timeline-conform-interchange-kernel.md. better-sqlite3 gates
lineage/reverse/DB, sharp/ffmpeg gate frame compare — call advanced `capabilities`.
Relink plans are read-only until executed; never derive source media (see AGENTS.md)."""


@mcp.prompt(
    name="delivery_workflow",
    title="Delivery / Deliverable QC Workflow",
    description="Route a render/deliverable-QC/media-provenance task across the live render tools and the offline deliverable/media/provenance tools.",
)
def delivery_workflow() -> str:
    return """Delivery spans two servers: the live Python server plans/validates/runs
renders in a running Resolve; the advanced (offline) server QCs the FINISHED render
and manages media/provenance with no Resolve open. Deliverable QC is report-only:
gate=review, never auto-pass-clear.

- Live: render probe_render_matrix -> validate_render_settings ->
  safe_set_render_settings (dry-run) -> prepare_render_job (adds, does not start;
  temp output dirs by default). safe_quick_export forces EnableUpload=False and
  needs allow_render=True. GetRenderSettings readback is version/page dependent.
- Offline `deliverable` actions: deliverable_qc (ffprobe vs spec, pass/fail per
  field), loudness_qc (ebur128 LUFS/true-peak/LRA), reframe_blanking_check,
  conform_completeness, re_delivery_diff, render_manifest, expand_deliverable
  (texted/textless/stems/slate/leader).
- Offline `media`: ingest_verify (hash seal/verify/dupes), media_inventory,
  sync (picture<->sound TC + drift/MOS), relink_manifest, rename_plan (refuses
  camera originals) / reel_normalize, turnover_package, project_hygiene.
- Offline `provenance`: grade_provenance, gallery_lineage, cdl_export/cdl_diff
  (round-trip asserted), revision_tracking, episode_report.

QC refuses rather than fabricates; gates never auto-clear — surface per-field
verdicts to a human. deliverable/media QC needs ffmpeg+ffprobe on PATH (GPL, not
bundled) — call advanced `capabilities`. Render probes touch only synthetic
fixtures, never user source media (see AGENTS.md).

Depth: docs/kernels/render-deliver-kernel.md."""


@mcp.prompt(
    name="fusion_workflow",
    title="Fusion Composition Workflow",
    description="Route a Fusion comp task across the live fusion_comp tools and the offline .comp authoring tool.",
)
def fusion_workflow() -> str:
    return """Fusion work spans two servers: the live Python server builds a comp on a
running timeline item; the advanced (offline) server authors a .comp from a spec
or template with no Resolve open. Author offline, apply live.

- Live: fusion_comp probe_fusion_comp / probe_fusion_tool / safe_add_tool /
  safe_set_inputs / safe_connect_tools / fusion_boundary_report.
- Offline (advanced `fusion` tool): generate / generate_from_template /
  list_templates / to_api_calls.

Flow: fusion generate|to_api_calls offline -> apply live via safe_add_tool ->
safe_set_inputs -> safe_connect_tools (to_api_calls maps directly onto those).
Probe first — tool availability and input readability vary by Resolve/Fusion
build; some inputs coerce or are write-only; bulk mutation needs timeline scope,
not the active Fusion page.

Depth: docs/kernels/fusion-composition-kernel.md. Never derive source media (AGENTS.md)."""


@mcp.prompt(
    name="audio_workflow",
    title="Audio / Fairlight Workflow",
    description="Route an audio/Fairlight task across the live audio tools and the offline planning/bus-routing tools.",
)
def audio_workflow() -> str:
    return """Audio work spans two servers: the live Python server drives audio on a
running Resolve; the advanced (offline) server plans tracks, routes buses, and
edits audio files with no Resolve open. Plan/measure offline, apply live.

- Live: timeline probe_audio_item|track / safe_set_audio_properties /
  safe_auto_sync_audio / voice_isolation_capabilities / subtitle_generation_probe
  / fairlight_boundary_report.
- Offline `audio_plan` (pure Node): list_templates, select_template, track_plan,
  analyze_coverage, check_loudness (R128 -23 / ATSC -24 / streaming -14).
- Offline `fairlight`: bus routing has NO scripting API — patches the
  FLStudioModelBA blob. read_buses_from_blob (offline); read_buses_from_db,
  expand_buses, export_template/import_template, backup, restore (DB path; needs
  better-sqlite3; project CLOSED + quit/relaunch like other DB patches — inside
  an `orchestrate` job, park the audio stage with `request_offline_op` instead
  of hand-rolling the sequence).
- Offline `audio`: split (silence/TC/intervals) / trim / convert (needs ffmpeg on
  PATH — GPL, not bundled). Align/loudness-measure not yet vendored.

Timeline audio SetProperty (e.g. Volume) can return false for some item types;
the public API exposes no Fairlight automation curves — use fairlight for bus
structure only. The offline audio ops write NEW files to scratch, never over
source (AGENTS.md).

Depth: docs/kernels/audio-fairlight-kernel.md."""


@mcp.prompt(
    name="media_pool_workflow",
    title="Media Pool / Ingest Workflow",
    description="Route a media-pool ingest/organization task across the live media_pool tools and the offline media front-end tool.",
)
def media_pool_workflow() -> str:
    return """Media pool work spans two servers: the live Python server imports and
organizes media in a running Resolve; the advanced (offline) server verifies and
inventories a card with no Resolve open. Verify offline, import live.

- Live: media_pool safe_import_media|sequence|folder / organize_clips /
  normalize_metadata / safe_relink|unlink / link_proxy_checked / set_clip_marks /
  setup_multicam_timeline.
- Offline `media` (needs ffmpeg + ffprobe on PATH): ingest_verify (hash
  seal/verify/dupes), media_inventory (fps/codec/colorspace/TC + card gaps), sync
  (picture<->sound TC + drift/MOS), relink_manifest, rename_plan (refuses camera
  originals) / reel_normalize, turnover_package, project_hygiene.

Rule of thumb: verify + inventory the card offline BEFORE importing, then import
and organize live. Non-media files are not imported; the kernel never proxies,
transcodes, or derives source media. Native multicam clip creation isn't in the
public API — the setup helper preps a stacked timeline you convert in Resolve's
UI. Never rename/derive camera originals without explicit approval (AGENTS.md).
For reading/analyzing footage content use the analyze_media prompt; for
deliverable-side media QC use delivery_workflow.

Depth: docs/kernels/media-pool-ingest-kernel.md, docs/guides/multicam-setup-guide.md."""


@mcp.prompt(
    name="extension_authoring_workflow",
    title="Extension Authoring Workflow",
    description="Route a Fuse/DCTL/Resolve-page-script authoring task across the live extension lifecycle tools.",
)
def extension_authoring_workflow() -> str:
    return """Extension authoring is a live-only domain: generate, validate, install, and
remove Fuse tools, DCTL/ACES LUTs, and Resolve-page scripts through one
lifecycle-aware tool. There is no offline authoring path yet.

- Live: script_plugin extension_capabilities / probe_fuse_lifecycle /
  probe_dctl_lifecycle / probe_script_lifecycle / safe_install_extension /
  safe_remove_extension / refresh_or_restart_required / extension_boundary_report.
  Raw fuse_plugin and dctl tools remain available for direct file operations.

Rule of thumb: probe the lifecycle for the extension kind first, install with the
`_mcp_` marker guard on, then check refresh_or_restart_required before assuming
Resolve has picked up the change. New Fuses and ACES IDT/ODT DCTLs need a
restart; regular LUT-category DCTLs pick up via project_settings.refresh_luts.
Installed Lua execution through fusion.RunScript is unreliable — use
script_plugin.run_inline for captured output.

Depth: docs/kernels/extension-authoring-kernel.md."""


@mcp.prompt(
    name="project_lifecycle_workflow",
    title="Project / Database / Archive Workflow",
    description="Route a project/database/archive/preset task across the live project_manager tools and the offline project_db patcher.",
)
def project_lifecycle_workflow() -> str:
    return """Project lifecycle spans two servers: the live Python server creates, exports,
imports, archives, restores, and configures projects/databases/presets in a
running Resolve; the advanced (offline) server patches the project DB with no
Resolve open.

- Live: project_manager project_capabilities / probe_project_lifecycle /
  probe_project_settings / safe_project_create|export|import|archive|restore|delete /
  safe_set_project_settings / project_settings_snapshot / database_capabilities /
  safe_set_current_database / preset_lifecycle_probe / project_boundary_report,
  plus project_manager_folders and project_manager_database.
- Offline `project_db` (needs the project CLOSED + a full quit/relaunch).

Rule of thumb: safe project create/import/restore/delete require `_mcp_`-prefixed
names unless allow_non_mcp_name is set; safe export/import/archive/restore paths
must sit under the system temp dir unless require_temp_path=False. Safe archive
defaults every media/cache/proxy flag to false. Database switching is a dry-run
unless both allow_switch=True and dry_run=False are given — a real switch closes
open projects.

Depth: docs/kernels/project-lifecycle-kernel.md."""


@mcp.prompt(
    name="review_annotation_workflow",
    title="Review Annotation Workflow",
    description="Route a marker/flag/clip-color annotation task across the live timeline_markers annotation layer.",
)
def review_annotation_workflow() -> str:
    return """Review annotation is a live-only domain: add, read, copy, move, and clear
markers, flags, and clip color across timeline, timeline item, and media pool
item scopes, and produce read-only review reports.

- Live: timeline_markers annotation_capabilities / probe_annotations /
  normalize_marker_payload / copy_annotations / move_annotations /
  sync_marker_custom_data / clear_annotations_by_scope / export_review_report /
  annotation_boundary_report.

Rule of thumb: timeline, timeline item, and media pool item frame spaces are NOT
interchangeable — copy_annotations/move_annotations use direct frame numbers, so
map frames explicitly when moving between scopes. Flags and clip color copy only
when both source and target expose compatible methods; invalid marker colors are
rejected before calling Resolve.

Depth: docs/kernels/review-annotation-kernel.md."""


@mcp.prompt(
    name="server_ops_workflow",
    title="Server Ops Workflow",
    description="Route a connect/verify, surface-selection, or diagnostics task across setup, resolve_control, timeline_versioning, the control panel, and the MCP resources.",
)
def server_ops_workflow() -> str:
    return """Server ops is the connector's own "is it alive and configured correctly" layer —
live-only, cross-cutting infrastructure, not a Resolve-content domain.

- Live: setup (schema/get_defaults/set_defaults/clear_defaults — conversational
  defaults), resolve_control (launch/get_version/get_page/open_page, the control
  panel lifecycle, api_truth/verification_stats/env_audit, MCP update policy),
  timeline_versioning (begin_run/end_run/list_versions/diff_versions/rollback —
  snapshot + rollback safety net used by other domains' guarded actions).
- Surface toggle: compound (default, one tool per domain) vs granular (`--full`,
  one tool per underlying API method) — see docs/SKILL.md for the tool-count table.
- Diagnostics: `scripts/doctor.py` (environment/dependency check), the advanced
  server's `capabilities` tool (native-dep detection + install hints),
  `capabilities://install_guidance` resource, `src/batch_cli.py` (headless batch
  runner), `src/control_panel.py` + `src/dashboard/` (local browser control panel).
- Resources (no tool call needed): status://mcp_version, status://resolve_connection,
  status://current_project, status://current_timeline, status://caps_preset,
  analysis://recent_reports, capabilities://installed_tools,
  capabilities://install_guidance.

Rule of thumb: call resolve_control(action="launch") first in a new session — it
connects to or starts Resolve and is safe to call when already running.
timeline_versioning underlies other domains' snapshot/rollback guarantees; it is
not itself a content-editing tool.

Depth: docs/kernels/server-ops-kernel.md."""


# ─── Python Version Check ────────────────────────────────────────────────────

_py_ver = sys.version_info[:2]
if _py_ver >= (3, 13):
    logger.warning(
        f"Python {_py_ver[0]}.{_py_ver[1]} detected. This is verified working on recent "
        f"Resolve builds (Studio 20.3.2), but older builds may not load the scripting "
        f"bridge on 3.13+. If scriptapp('Resolve') returns None, recreate the venv with "
        f"Python 3.10-3.12."
    )

# ─── Resolve Connection (lazy) ───────────────────────────────────────────────
# Extracted to src/core/live_connection.py (restructure epic #52, Phase 3 / #46).
# Only names still referenced directly in this file are re-imported; `resolve`/
# `dvr_script` are NOT re-imported here since they're reassigned via `global`
# inside live_connection.py — a plain import would go stale. Always go through
# get_resolve().
from src.core.live_connection import (  # noqa: E402
    get_resolve,
    _destructive_versioning_provider,
    _bridge_lock,
)


# ─── Resolve 21 AI-ops ledger plumbing ────────────────────────────────────────

import uuid as _ledger_uuid
from src.core import resolve_ai_ledger as _resolve_ai_ledger

# One id per server process so the ledger / dashboard can scope "this session".








# ─── Resolve 21 AI-ops governance (soft tiers over the ledger) ────────────────

from src.core import resolve_ai_governance as _resolve_ai_governance














def _destructive_preference_provider(key: str) -> Any:
    """Reader for C6 preferences out of the existing media-analysis prefs file."""
    try:
        return _read_media_analysis_preferences().get(key)
    except Exception:
        return None


_destructive_hook.register_preference_provider(_destructive_preference_provider)


# Gated (tool, action) pairs routed through the destructive_hook wrapper that
# also live behind the confirm_token gate. The wrapper consults this set BEFORE


def _action_will_gate_pending_confirm(
    tool_name: str, action: str, params: Optional[Dict[str, Any]]
) -> bool:
    """True iff the next call to (tool_name, action, params) will short-circuit
    to issue a confirm_token (no mutation, nothing to archive yet)."""
    if not _confirm_token_required():
        return False
    if isinstance(params, dict) and ("confirm_token" in params or "confirmToken" in params):
        return False
    if (tool_name, action) in _TOKEN_GATED_DESTRUCTIVE_ACTIONS:
        return True
    # delete_clips is gated only when ripple=True.
    if (
        tool_name == "timeline"
        and action == "delete_clips"
        and isinstance(params, dict)
        and bool(params.get("ripple"))
    ):
        return True
    return False


_destructive_hook.register_pending_confirm_check(_action_will_gate_pending_confirm)


# ─── Analysis caps preference plumbing ────────────────────────────────────────






# Lazy import to avoid touching media_analysis at module-init time (it imports
# our destructive-hook + analysis_caps modules already, but the providers we
# register here read media-analysis preferences which the server owns).
from src.domains.media_analysis.utils import caps_gating as _media_analysis_module
from src.domains.media_analysis.utils.clip_identity_registry import normalize_sampling_mode as _normalize_sampling_mode
# _caps_preset_provider/_caps_overrides_provider moved to
# src.domains.media_analysis.actions (Phase 3, #46) — registered after the
# domain action imports further down, once those names are available.


# ─── Helpers ──────────────────────────────────────────────────────────────────













# ───────────────────────────────────────────────────────────────────────────
# B2 — Confirm-token gate on whole-grade-replacement and catastrophic ops.
# In-process, session-scoped store. Tokens are one-time-use, expire after 5 min,
# and are bound to (action, params_fingerprint) so a token for one mutation
# cannot be reused for a different one.
# ───────────────────────────────────────────────────────────────────────────

import hashlib as _hashlib
import time as _time
import uuid as _uuid







































































































































# Filters the live timeline adapter can populate from a timeline-item summary.
# Analysis-aware filters in clip_query (analyzed/has_transcription/shot_type/
# marker_color) require an analysis-DB join not yet wired here; reject them at
# the boundary rather than return silently-wrong (empty) matches.




















































































































































































# Binary interchange formats Resolve reads NATIVELY — the XML sanitize/relink pass
# (which parses the file as text) does not apply. Relinking happens via the media
# pool after import, not by rewriting the file.










































# A .drp/.drt is a zip of SeqContainer*.xml entries. Two on-disk naming conventions,
# mirroring the Node drt-format parser: tool-authored `<folder>/SeqContainer<N>.xml`
# and real-Resolve `SeqContainer/<uuid>.xml`. Never match MpFolder/project/Gallery.











































































# Timeline.CreateSubtitlesFromAudio({autoCaptionSettings}) — enum-keyed exactly
# like AutoSyncAudio (docs/reference/resolve_scripting_api.txt lines 733-771).




# ProjectManager CloudProject family — {cloudSettings} is keyed by
# resolve.CLOUD_SETTING_* constants with resolve.CLOUD_SYNC_* sync-mode values
# (docs/reference/resolve_scripting_api.txt lines 588-603). Same silent-rejection
# failure mode as AutoSyncAudio when handed plain string keys.

























# Clip-property keys whose SetClipProperty/SetMetadata write returns True but is
# silently dropped by Resolve under common project configurations (issue #77).
# For these we read the value back and refuse to report success unless it stuck.

# Per-key hint explaining the most common cause of a non-persisting write.






























def _media_analysis_extract_json_text(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None, "Sampling response did not contain a JSON object"
        try:
            payload = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            return None, f"Sampling response JSON parse failed: {exc}"
    if not isinstance(payload, dict):
        return None, "Sampling response JSON must be an object"
    return payload, None
















_SETUP_YES_NO_VALUES = {"yes", "no"}
































# Documented option/state key sets (docs/reference/resolve_scripting_api.txt).




def _setup_positive_float(value: Any, default: float, min_value: float = 0.1, max_value: float = 8760.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(parsed, max_value))












# V2 architecture decision: machine-generated per-shot / qc_warning / best_moment markers
# are NOT written to Resolve. Clip-level metadata (Description, Keywords, Comments,
# third_party namespace) still writes through for searchability in Resolve's bin.
# Editor's own markers in Resolve are untouched by the machine.
#
# Rationale: bidirectional sync with Resolve markers was the largest source of
# architectural pain (pull-only API with no event hooks, 362-char note truncation,
# one-marker-per-frame collisions). The canonical store is the analysis DB; the
# correction surface is the control panel + chat, not Resolve markers.
#
# See the V2 shot schema spec §9.1 (Decisions log) for details.





















































































































































































































# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 1: resolve
# ═══════════════════════════════════════════════════════════════════════════════

def _mcp_update_status_payload(force: bool = False, timeout: float = 3.0) -> Dict[str, Any]:
    update_env = _setup_update_env()
    if force:
        update = check_for_updates(VERSION, project_dir, env=update_env, timeout=timeout, force=True)
    else:
        update = get_cached_update_status(project_dir, VERSION, env=update_env)
    return {
        "version": VERSION,
        "update": update,
        "decision": update_prompt_decision(update, env=update_env),
    }


_SETUP_UPDATE_MODES = {"prompt", "auto", "notify", "never"}
_SETUP_CLEAR_VALUES = {"", "ask", "prompt", "clear", "default", "none", "null", "unset"}


def _setup_update_state() -> Dict[str, Any]:
    try:
        with open(update_state_path(project_dir), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _setup_update_state_strict() -> Dict[str, Any]:
    """Strict update-state read for read-modify-write paths — raises
    ConfigParseError on a corrupt existing file so the caller refuses to
    overwrite and wipe saved update prefs (PS2)."""
    return _read_json_strict(str(update_state_path(project_dir)))


def _write_setup_update_state(state: Dict[str, Any]) -> None:
    """Atomically persist update state (temp + os.replace), so a crash mid-write
    can't truncate the file that _setup_update_state then resets to {} (PS2)."""
    path = str(update_state_path(project_dir))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _setup_update_env() -> Dict[str, str]:
    env = dict(os.environ)
    state = _setup_update_state()
    if state.get("check_interval_hours") is not None and "DAVINCI_RESOLVE_MCP_UPDATE_INTERVAL_HOURS" not in env:
        env["DAVINCI_RESOLVE_MCP_UPDATE_INTERVAL_HOURS"] = str(state["check_interval_hours"])
    if state.get("snooze_hours") is not None and "DAVINCI_RESOLVE_MCP_UPDATE_SNOOZE_HOURS" not in env:
        env["DAVINCI_RESOLVE_MCP_UPDATE_SNOOZE_HOURS"] = str(state["snooze_hours"])
    return env


def _setup_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _setup_nested(params: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    for key in keys:
        value = params.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _setup_normalize_timed_marker_default(value: Any) -> Tuple[Optional[str], Optional[str], bool]:
    if value is None:
        return None, None, False
    if not isinstance(value, bool) and str(value).strip().lower().replace("-", "_").replace(" ", "_") in _SETUP_CLEAR_VALUES:
        return None, None, True
    choice = _normalize_timed_marker_choice(value)
    if choice in {"default_yes", "default_no"}:
        return ("yes" if choice == "default_yes" else "no"), None, False
    if choice in {"yes", "no"}:
        return choice, None, False
    if choice == "ask":
        return None, None, True
    return None, f"Unsupported timed markers default: {value!r}. Use yes, no, ask, default_yes, or default_no.", False


def _setup_media_analysis_defaults() -> Dict[str, Any]:
    effective = _media_analysis_effective_preferences()
    return {
        **effective,
        "preferences_path": _media_analysis_preferences_path(),
        "options": {
            "yes_no_ask": ["yes", "no", "ask"],
            "timed_markers": ["yes", "no", "ask", "default_yes", "default_no"],
            "vision_default": ["on", "off", "technical_only", "ask"],
            "analysis_persistence": ["session_only", "keep_reports", "keep_artifacts"],
            "metadata_writeback_default": [True, False],
            "metadata_overwrite_policy": ["preserve_human", "fill_empty", "overwrite_owned_blocks", "overwrite_all"],
            "timed_marker_types": ["shots", "slate_clap", "sync_events", "best_moments", "qc_warnings"],
            "analysis_summary_style": ["full", "concise", "creative", "technical"],
            "report_format": ["compact", "full", "machine_readable"],
            "source_trust": ["auto", "filename", "low", "medium", "high"],
            "default_depth": ["quick", "standard", "deep"],
            "default_post_operation_page": ["stay_put", "media", "cut", "edit", "fusion", "color", "fairlight", "deliver"],
            "sampling_mode_default": ["ask", "fixed", "per_minute", "adaptive_capped", "adaptive"],
        },
        "sampling_mode_labels": dict(_media_analysis_module.SAMPLING_MODE_LABELS),
    }


def _setup_updates_defaults() -> Dict[str, Any]:
    update_env = _setup_update_env()
    state = _setup_update_state()
    mode = get_update_mode(project_dir, update_env)
    update = get_cached_update_status(project_dir, VERSION, env=update_env)
    update["update_mode"] = mode
    return {
        "mode": mode,
        "check_interval_hours": _setup_positive_float(state.get("check_interval_hours"), 24.0, 0.1, 8760.0),
        "snooze_hours": _setup_positive_float(state.get("snooze_hours"), 24.0, 0.1, 8760.0),
        "state_path": str(update_state_path(project_dir)),
        "options": sorted(_SETUP_UPDATE_MODES),
        "update": update,
        "decision": update_prompt_decision(update, env=update_env),
    }


def _setup_defaults_snapshot() -> Dict[str, Any]:
    return {
        "media_analysis": _setup_media_analysis_defaults(),
        "updates": _setup_updates_defaults(),
    }


def _setup_set_media_analysis_defaults(media_defaults: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    if not media_defaults:
        return {"changed": False, "recognized": False}

    alias_to_key = {
        "timed_markers_default": "timed_markers_default",
        "timedmarkersdefault": "timed_markers_default",
        "timed_markers": "timed_markers_default",
        "timedmarkers": "timed_markers_default",
        "write_markers": "timed_markers_default",
        "writemarkers": "timed_markers_default",
        "slate_detection_default": "slate_detection_default",
        "slatedetectiondefault": "slate_detection_default",
        "slate_detection": "slate_detection_default",
        "slatedetection": "slate_detection_default",
        "vision_default": "vision_default",
        "visiondefault": "vision_default",
        "vision": "vision_default",
        "transcription_default": "transcription_default",
        "transcriptiondefault": "transcription_default",
        "transcription": "transcription_default",
        "analysis_persistence": "analysis_persistence",
        "analysispersistence": "analysis_persistence",
        "persistence": "analysis_persistence",
        "metadata_publish_fields": "metadata_publish_fields",
        "metadatapublishfields": "metadata_publish_fields",
        "fields": "metadata_publish_fields",
        "metadata_fields": "metadata_publish_fields",
        "metadata_overwrite_policy": "metadata_overwrite_policy",
        "metadataoverwritepolicy": "metadata_overwrite_policy",
        "overwrite_policy": "metadata_overwrite_policy",
        "overwritepolicy": "metadata_overwrite_policy",
        "timed_marker_types": "timed_marker_types",
        "timedmarkertypes": "timed_marker_types",
        "marker_types": "timed_marker_types",
        "markertypes": "timed_marker_types",
        "timed_marker_colors": "timed_marker_colors",
        "timedmarkercolors": "timed_marker_colors",
        "marker_colors": "timed_marker_colors",
        "markercolors": "timed_marker_colors",
        "max_timed_markers_per_clip": "max_timed_markers_per_clip",
        "maxtimedmarkersperclip": "max_timed_markers_per_clip",
        "max_markers": "max_timed_markers_per_clip",
        "maxmarkers": "max_timed_markers_per_clip",
        "include_confidence_scores": "include_confidence_scores",
        "includeconfidencescores": "include_confidence_scores",
        "include_source_time_notes": "include_source_time_notes",
        "includesourcetimenotes": "include_source_time_notes",
        "analysis_summary_style": "analysis_summary_style",
        "analysissummarystyle": "analysis_summary_style",
        "summary_style": "analysis_summary_style",
        "summarystyle": "analysis_summary_style",
        "report_format": "report_format",
        "reportformat": "report_format",
        "preferred_analysis_root": "preferred_analysis_root",
        "preferredanalysisroot": "preferred_analysis_root",
        "analysis_root": "preferred_analysis_root",
        "analysisroot": "preferred_analysis_root",
        "preferred_generated_media_folder": "preferred_generated_media_folder",
        "preferredgeneratedmediafolder": "preferred_generated_media_folder",
        "generated_media_folder": "preferred_generated_media_folder",
        "generatedmediafolder": "preferred_generated_media_folder",
        "inventory_limit": "inventory_limit",
        "inventorylimit": "inventory_limit",
        "inventory_exclude_bins": "inventory_exclude_bins",
        "inventoryexcludebins": "inventory_exclude_bins",
        "exclude_bins": "inventory_exclude_bins",
        "excludebins": "inventory_exclude_bins",
        "default_post_operation_page": "default_post_operation_page",
        "defaultpostoperationpage": "default_post_operation_page",
        "post_operation_page": "default_post_operation_page",
        "postoperationpage": "default_post_operation_page",
        "marker_custom_data": "marker_custom_data",
        "markercustomdata": "marker_custom_data",
        "metadata_writeback_default": "metadata_writeback_default",
        "metadatawritebackdefault": "metadata_writeback_default",
        "metadata_writeback": "metadata_writeback_default",
        "metadatawriteback": "metadata_writeback_default",
        "publish_metadata": "metadata_writeback_default",
        "publishmetadata": "metadata_writeback_default",
        "write_metadata": "metadata_writeback_default",
        "writemetadata": "metadata_writeback_default",
        "write_to_resolve": "metadata_writeback_default",
        "writetoresolve": "metadata_writeback_default",
        "ask_before_metadata_publish": "ask_before_metadata_publish",
        "askbeforemetadatapublish": "ask_before_metadata_publish",
        "dry_run_first_default": "dry_run_first_default",
        "dryrunfirstdefault": "dry_run_first_default",
        "dry_run_first": "dry_run_first_default",
        "dryrunfirst": "dry_run_first_default",
        "source_trust": "source_trust",
        "sourcetrust": "source_trust",
        "trust": "source_trust",
        "default_depth": "default_depth",
        "defaultdepth": "default_depth",
        "default_sample_frames": "default_sample_frames",
        "defaultsampleframes": "default_sample_frames",
        "sample_frames": "default_sample_frames",
        "sampleframes": "default_sample_frames",
        "sampling_mode_default": "sampling_mode_default",
        "samplingmodedefault": "sampling_mode_default",
        "sampling_mode": "sampling_mode_default",
        "samplingmode": "sampling_mode_default",
        "analysis_mode": "sampling_mode_default",
        "analysismode": "sampling_mode_default",
        "sampling_frames_per_minute": "sampling_frames_per_minute",
        "samplingframesperminute": "sampling_frames_per_minute",
        "frames_per_minute": "sampling_frames_per_minute",
        "framesperminute": "sampling_frames_per_minute",
        "sampling_frame_floor": "sampling_frame_floor",
        "samplingframefloor": "sampling_frame_floor",
        "frame_floor": "sampling_frame_floor",
        "framefloor": "sampling_frame_floor",
        "sampling_frame_ceiling": "sampling_frame_ceiling",
        "samplingframeceiling": "sampling_frame_ceiling",
        "frame_ceiling": "sampling_frame_ceiling",
        "frameceiling": "sampling_frame_ceiling",
    }

    requested: Dict[str, Any] = {}
    for key, value in media_defaults.items():
        normalized_key = alias_to_key.get(_setup_text_key(key).replace("_", ""))
        if not normalized_key:
            normalized_key = alias_to_key.get(_setup_text_key(key))
        if normalized_key:
            requested[normalized_key] = value
    if not requested:
        return {"changed": False, "recognized": False}

    # Strict read: this is a read-modify-write of the prefs file, so a corrupt
    # existing file must refuse rather than seed the write from {} and wipe every
    # saved preference (PS1).
    try:
        preferences = _read_media_analysis_preferences_strict()
    except ConfigParseError as exc:
        return _err(f"Refusing to update media-analysis defaults: {exc}. The preferences file exists but is unparseable; fix or delete it to avoid wiping saved settings.")
    before = _media_analysis_effective_preferences()
    next_preferences = dict(preferences)
    updates: Dict[str, Dict[str, Any]] = {}

    def clear_requested(raw: Any) -> bool:
        return raw is None or (not isinstance(raw, bool) and _setup_text_key(raw) in _SETUP_CHOICE_CLEAR_VALUES)

    def set_or_clear(key: str, raw: Any, value: Any, *, allow_clear: bool = True) -> None:
        if allow_clear and clear_requested(raw):
            next_preferences.pop(key, None)
            next_preferences.pop(f"{key}_updated_at", None)
            updates[key] = {"before": before.get(key), "after": _MEDIA_ANALYSIS_DEFAULT_PREFS.get(key), "cleared": True}
        else:
            next_preferences[key] = value
            updates[key] = {"before": before.get(key), "after": value}

    for key, raw_value in requested.items():
        if key == "timed_markers_default":
            normalized, error, clear = _setup_normalize_timed_marker_default(raw_value)
            if error:
                return _err(error)
            if clear:
                next_preferences.pop("timed_markers_default", None)
                next_preferences.pop("timed_markers_default_updated_at", None)
                updates[key] = {"before": before.get(key), "after": None, "cleared": True}
            else:
                next_preferences["timed_markers_default"] = normalized
                updates[key] = {"before": before.get(key), "after": normalized}
        elif clear_requested(raw_value):
            set_or_clear(key, raw_value, _MEDIA_ANALYSIS_DEFAULT_PREFS.get(key))
        elif key in {"slate_detection_default", "transcription_default"}:
            normalized = _normalize_yes_no_ask(raw_value)
            if normalized is None:
                return _err(f"Unsupported {key}: {raw_value!r}. Use yes, no, or ask.")
            set_or_clear(key, raw_value, normalized)
        elif key == "vision_default":
            normalized = _normalize_vision_default(raw_value)
            if normalized is None:
                return _err("Unsupported vision_default. Use on, off, technical_only, or ask.")
            set_or_clear(key, raw_value, normalized)
        elif key == "analysis_persistence":
            normalized = _normalize_analysis_persistence(raw_value)
            if normalized is None:
                return _err("Unsupported analysis_persistence. Use session_only, keep_reports, or keep_artifacts.")
            set_or_clear(key, raw_value, normalized)
        elif key == "metadata_publish_fields":
            fields = [str(field).strip() for field in _media_analysis_as_list(raw_value) if str(field).strip()]
            if not fields and not clear_requested(raw_value):
                return _err("metadata_publish_fields must be a non-empty list or comma-separated string.")
            set_or_clear(key, raw_value, fields)
        elif key == "metadata_overwrite_policy":
            normalized = _normalize_metadata_overwrite_policy(raw_value)
            if normalized is None:
                return _err("Unsupported metadata_overwrite_policy. Use preserve_human, fill_empty, overwrite_owned_blocks, or overwrite_all.")
            set_or_clear(key, raw_value, normalized)
        elif key == "timed_marker_types":
            marker_types = _normalize_setup_list(
                raw_value,
                aliases=_MEDIA_ANALYSIS_MARKER_TYPE_ALIASES,
                allowed=["shots", "slate_clap", "sync_events", "best_moments", "qc_warnings"],
            )
            if not marker_types and not clear_requested(raw_value):
                return _err("timed_marker_types must include shots, slate_clap, sync_events, best_moments, or qc_warnings.")
            set_or_clear(key, raw_value, marker_types)
        elif key == "timed_marker_colors":
            colors = _normalize_marker_colors(raw_value)
            if not colors and not clear_requested(raw_value):
                return _err("timed_marker_colors must be an object of marker type to Resolve marker color.")
            set_or_clear(key, raw_value, colors)
        elif key == "max_timed_markers_per_clip":
            set_or_clear(key, raw_value, _setup_marker_limit(raw_value, 12))
        elif key in {"include_confidence_scores", "include_source_time_notes", "metadata_writeback_default", "ask_before_metadata_publish", "dry_run_first_default"}:
            set_or_clear(key, raw_value, _media_analysis_bool(raw_value, _MEDIA_ANALYSIS_DEFAULT_PREFS[key]))
        elif key == "analysis_summary_style":
            normalized = _normalize_setup_choice(
                raw_value,
                ["full", "concise", "creative", "technical"],
                aliases={
                    "assistant_editor": "creative",
                    "assistant": "creative",
                    "editor": "creative",
                    "producer": "creative",
                    "qc": "technical",
                    "qc_focus": "technical",
                    "qc_focused": "technical",
                },
            )
            if normalized is None:
                return _err("Unsupported analysis_summary_style. Use full, concise, creative, or technical.")
            set_or_clear(key, raw_value, normalized)
        elif key == "report_format":
            normalized = _normalize_setup_choice(raw_value, ["compact", "full", "machine_readable"])
            if normalized is None:
                return _err("Unsupported report_format. Use compact, full, or machine_readable.")
            set_or_clear(key, raw_value, normalized)
        elif key == "source_trust":
            normalized = _normalize_setup_choice(
                raw_value,
                ["auto", "filename", "low", "medium", "high"],
                aliases={"none": "auto", "default": "auto"},
            )
            if normalized is None:
                return _err("Unsupported source_trust. Use auto, filename, low, medium, or high.")
            set_or_clear(key, raw_value, normalized)
        elif key == "default_depth":
            normalized = _normalize_setup_choice(raw_value, ["quick", "standard", "deep"])
            if normalized is None:
                return _err("Unsupported default_depth. Use quick, standard, or deep.")
            set_or_clear(key, raw_value, normalized)
        elif key == "default_sample_frames":
            try:
                frames_int = int(raw_value) if not isinstance(raw_value, bool) else 8
            except (TypeError, ValueError):
                return _err("default_sample_frames must be an integer between 0 and 48.")
            set_or_clear(key, raw_value, max(0, min(48, frames_int)))
        elif key == "sampling_mode_default":
            # "ask" clears the saved default so the first-run prompt fires again;
            # otherwise normalize a canonical key or friendly label.
            if _setup_text_key(raw_value) in {"ask", "prompt", "askme", "askuser"}:
                next_preferences.pop("sampling_mode_default", None)
                next_preferences.pop("sampling_mode_default_updated_at", None)
                updates[key] = {"before": before.get(key), "after": None, "cleared": True}
            else:
                normalized = _normalize_sampling_mode(raw_value, default=None)
                if normalized is None:
                    return _err(
                        "Unsupported sampling_mode_default. Use ask, fixed/economy, "
                        "per_minute/balanced, adaptive_capped/thorough, or adaptive."
                    )
                set_or_clear(key, raw_value, normalized)
        elif key == "sampling_frames_per_minute":
            try:
                rate = float(raw_value)
            except (TypeError, ValueError):
                return _err("sampling_frames_per_minute must be a positive number.")
            if rate <= 0:
                return _err("sampling_frames_per_minute must be greater than 0.")
            set_or_clear(key, raw_value, rate)
        elif key in {"sampling_frame_floor", "sampling_frame_ceiling"}:
            try:
                n = int(raw_value) if not isinstance(raw_value, bool) else 0
            except (TypeError, ValueError):
                return _err(f"{key} must be a positive integer.")
            if n <= 0:
                return _err(f"{key} must be a positive integer.")
            set_or_clear(key, raw_value, n)
        elif key == "default_post_operation_page":
            normalized = _normalize_setup_choice(
                raw_value,
                ["stay_put", "media", "cut", "edit", "fusion", "color", "fairlight", "deliver"],
                aliases={"media_pool": "media", "none": "stay_put"},
            )
            if normalized is None:
                return _err("Unsupported default_post_operation_page.")
            set_or_clear(key, raw_value, normalized)
        elif key == "marker_custom_data":
            normalized = _normalize_setup_choice(raw_value, ["namespaced", "minimal"])
            if normalized is None:
                return _err("Unsupported marker_custom_data. Use namespaced or minimal.")
            set_or_clear(key, raw_value, normalized)
        elif key == "inventory_limit":
            try:
                n = int(raw_value) if not isinstance(raw_value, bool) else 500
            except (TypeError, ValueError):
                return _err("inventory_limit must be an integer between 1 and 10000.")
            set_or_clear(key, raw_value, max(1, min(10000, n)))
        elif key == "inventory_exclude_bins":
            # Normalize a comma-separated list of folder names; empty excludes nothing.
            parts = [part.strip() for part in str(raw_value).split(",") if part.strip()]
            set_or_clear(key, raw_value, ",".join(parts))
        elif key in {"preferred_analysis_root", "preferred_generated_media_folder"}:
            path = None if clear_requested(raw_value) else os.path.realpath(os.path.abspath(os.path.expanduser(str(raw_value))))
            set_or_clear(key, raw_value, path)

    if dry_run:
        return {
            "changed": True,
            "recognized": True,
            "updates": updates,
            "before": before,
            "after": {**before, **{key: row.get("after") for key, row in updates.items()}},
            "dry_run": True,
        }

    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for key, row in updates.items():
        if row.get("cleared"):
            next_preferences.pop(f"{key}_updated_at", None)
        else:
            next_preferences[f"{key}_updated_at"] = updated_at
    _write_media_analysis_preferences(next_preferences)
    after = _media_analysis_effective_preferences()
    return {
        "changed": before != after,
        "recognized": True,
        "updates": updates,
        "before": before,
        "after": after,
        "updated_at": updated_at,
        "preferences_path": _media_analysis_preferences_path(),
    }


def _setup_set_updates_defaults(update_defaults: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    mode = _first_param(
        update_defaults,
        "mode",
        "update_mode",
        "updateMode",
        "policy",
        "update_policy",
        "updatePolicy",
        default=None,
    )
    interval = _first_param(
        update_defaults,
        "check_interval_hours",
        "checkIntervalHours",
        "interval_hours",
        "intervalHours",
        "update_interval_hours",
        "updateIntervalHours",
        default=None,
    )
    snooze = _first_param(
        update_defaults,
        "snooze_hours",
        "snoozeHours",
        "update_snooze_hours",
        "updateSnoozeHours",
        default=None,
    )
    if mode is None and interval is None and snooze is None:
        return {"changed": False, "recognized": False}

    before = _setup_updates_defaults()
    state = _setup_update_state()
    updates: Dict[str, Dict[str, Any]] = {}
    if mode is not None:
        normalized = str(mode).strip().lower().replace("-", "_")
        if normalized == "manual":
            normalized = "prompt"
        if normalized not in _SETUP_UPDATE_MODES:
            return _err("Unsupported update mode. Use prompt, auto, notify, or never.")
        updates["mode"] = {"before": before.get("mode"), "after": normalized}
    if interval is not None:
        normalized_interval = _setup_positive_float(interval, 24.0, 0.1, 8760.0)
        updates["check_interval_hours"] = {"before": before.get("check_interval_hours"), "after": normalized_interval}
    if snooze is not None:
        normalized_snooze = _setup_positive_float(snooze, 24.0, 0.1, 8760.0)
        updates["snooze_hours"] = {"before": before.get("snooze_hours"), "after": normalized_snooze}

    if dry_run:
        return {"changed": True, "recognized": True, "updates": updates, "before": before, "dry_run": True}

    # Strict re-read before mutating: a corrupt update-state file must refuse,
    # not seed the write from {} and wipe saved update prefs (PS2).
    try:
        state = _setup_update_state_strict()
    except ConfigParseError as exc:
        return _err(f"Refusing to update settings: {exc}. The update-state file exists but is unparseable; fix or delete it to avoid wiping saved update preferences.")
    if "mode" in updates:
        set_update_mode(project_dir, updates["mode"]["after"], env=_setup_update_env())
        try:
            state = _setup_update_state_strict()
        except ConfigParseError as exc:
            return _err(f"Refusing to update settings: {exc}. The update-state file became unparseable; fix or delete it to avoid wiping saved update preferences.")
    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if "check_interval_hours" in updates:
        state["check_interval_hours"] = updates["check_interval_hours"]["after"]
    if "snooze_hours" in updates:
        state["snooze_hours"] = updates["snooze_hours"]["after"]
    if updates:
        state["setup_defaults_updated_at"] = updated_at
        _write_setup_update_state(state)
    return {
        "changed": bool(updates),
        "recognized": True,
        "updates": updates,
        "before": before,
        "after": _setup_updates_defaults(),
        "state_path": str(update_state_path(project_dir)),
    }


def _setup_clear_defaults(keys: Any, dry_run: bool) -> Dict[str, Any]:
    if keys is None:
        normalized_keys = {"all"}
    elif isinstance(keys, str):
        normalized_keys = {part.strip().lower() for part in keys.split(",") if part.strip()}
    elif isinstance(keys, list):
        normalized_keys = {str(part).strip().lower() for part in keys if str(part).strip()}
    else:
        return _err("keys must be a string, list, or omitted")

    clear_all = not normalized_keys or "all" in normalized_keys
    result: Dict[str, Any] = {"dry_run": dry_run, "cleared": []}

    media_clear_keys = {
        "timed_markers_default": "media_analysis.timed_markers_default",
        "slate_detection_default": "media_analysis.slate_detection_default",
        "vision_default": "media_analysis.vision_default",
        "transcription_default": "media_analysis.transcription_default",
        "analysis_persistence": "media_analysis.analysis_persistence",
        "metadata_publish_fields": "media_analysis.metadata_publish_fields",
        "metadata_overwrite_policy": "media_analysis.metadata_overwrite_policy",
        "timed_marker_types": "media_analysis.timed_marker_types",
        "timed_marker_colors": "media_analysis.timed_marker_colors",
        "max_timed_markers_per_clip": "media_analysis.max_timed_markers_per_clip",
        "include_confidence_scores": "media_analysis.include_confidence_scores",
        "include_source_time_notes": "media_analysis.include_source_time_notes",
        "analysis_summary_style": "media_analysis.analysis_summary_style",
        "report_format": "media_analysis.report_format",
        "preferred_analysis_root": "media_analysis.preferred_analysis_root",
        "preferred_generated_media_folder": "media_analysis.preferred_generated_media_folder",
        "default_post_operation_page": "media_analysis.default_post_operation_page",
        "marker_custom_data": "media_analysis.marker_custom_data",
        "metadata_writeback_default": "media_analysis.metadata_writeback_default",
        "ask_before_metadata_publish": "media_analysis.ask_before_metadata_publish",
        "dry_run_first_default": "media_analysis.dry_run_first_default",
        "sampling_mode_default": "media_analysis.sampling_mode_default",
        "sampling_frames_per_minute": "media_analysis.sampling_frames_per_minute",
        "sampling_frame_floor": "media_analysis.sampling_frame_floor",
        "sampling_frame_ceiling": "media_analysis.sampling_frame_ceiling",
    }
    media_payload: Dict[str, Any] = {}
    if clear_all or "media_analysis" in normalized_keys:
        media_payload = {key: "clear" for key in media_clear_keys}
    else:
        for key, label in media_clear_keys.items():
            if key in normalized_keys or label in normalized_keys:
                media_payload[key] = "clear"
    if media_payload:
        result["media_analysis"] = _setup_set_media_analysis_defaults(media_payload, dry_run)
        if result["media_analysis"].get("error"):
            return result["media_analysis"]
        result["cleared"].extend(media_clear_keys[key] for key in media_payload)

    if clear_all or normalized_keys & {"updates", "updates.mode", "update_mode", "mcp_update_policy"}:
        result["updates"] = _setup_set_updates_defaults({"mode": "prompt"}, dry_run)
        if result["updates"].get("error"):
            return result["updates"]
        result["cleared"].append("updates.mode")

    if clear_all or normalized_keys & {"updates.check_interval_hours", "check_interval_hours", "update_interval_hours"}:
        if dry_run:
            result["update_check_interval_hours"] = {"changed": True, "dry_run": True}
        else:
            try:
                state = _setup_update_state_strict()
            except ConfigParseError as exc:
                return _err(f"Refusing to clear update interval: {exc}. The update-state file exists but is unparseable; fix or delete it to avoid wiping saved update preferences.")
            state.pop("check_interval_hours", None)
            _write_setup_update_state(state)
            result["update_check_interval_hours"] = {"changed": True, "state_path": str(update_state_path(project_dir))}
        result["cleared"].append("updates.check_interval_hours")

    if clear_all or normalized_keys & {"updates.snooze_hours", "snooze_hours", "update_snooze_hours"}:
        if dry_run:
            result["update_snooze_hours"] = {"changed": True, "dry_run": True}
        else:
            try:
                state = _setup_update_state_strict()
            except ConfigParseError as exc:
                return _err(f"Refusing to clear snooze: {exc}. The update-state file exists but is unparseable; fix or delete it to avoid wiping saved update preferences.")
            state.pop("snooze_hours", None)
            _write_setup_update_state(state)
            result["update_snooze_hours"] = {"changed": True, "state_path": str(update_state_path(project_dir))}
        result["cleared"].append("updates.snooze_hours")

    if clear_all or normalized_keys & {"updates.prompt_preferences", "updates.snooze", "updates.ignore", "update_prompt_preferences"}:
        if dry_run:
            result["update_prompt_preferences"] = {"changed": True, "dry_run": True}
        else:
            clear_update_prompt_preferences(project_dir)
            result["update_prompt_preferences"] = {"changed": True, "state_path": str(update_state_path(project_dir))}
        result["cleared"].append("updates.prompt_preferences")

    if not result["cleared"]:
        return _err("No known setup defaults matched keys")
    result["defaults"] = _setup_defaults_snapshot()
    return _ok(**result)


@mcp.tool()
def setup(action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Configure MCP conversational defaults and setup preferences.

    Actions:
      schema() -> {defaults, actions}
      get_defaults() -> {defaults}
      set_defaults(defaults?|media_analysis?|updates?|dry_run?) -> {defaults, changes}
      clear_defaults(keys?, dry_run?) -> {defaults, cleared}

    Current defaults:
      media_analysis.*: analysis, metadata, marker, reporting, and workflow defaults
      updates.*: MCP update policy, interval, and snooze defaults
    """
    p = params or {}
    if action in {"schema", "capabilities", "options"}:
        return {
            "actions": ["schema", "get_defaults", "set_defaults", "clear_defaults"],
            "defaults": {
                "media_analysis.timed_markers_default": {
                    "description": "Default answer for writing source-time analysis notes as Media Pool clip markers.",
                    "values": ["yes", "no", "ask", "default_yes", "default_no"],
                    "storage": _media_analysis_preferences_path(),
                },
                "media_analysis.slate_detection_default": {"values": ["yes", "no", "ask"], "storage": _media_analysis_preferences_path()},
                "media_analysis.vision_default": {"values": ["on", "off", "technical_only", "ask"], "storage": _media_analysis_preferences_path()},
                "media_analysis.transcription_default": {"values": ["yes", "no", "ask"], "storage": _media_analysis_preferences_path()},
                "media_analysis.analysis_persistence": {"values": ["session_only", "keep_reports", "keep_artifacts"], "storage": _media_analysis_preferences_path()},
                "media_analysis.metadata_publish_fields": {"values": "list of Resolve metadata field names", "storage": _media_analysis_preferences_path()},
                "media_analysis.metadata_overwrite_policy": {"values": ["preserve_human", "fill_empty", "overwrite_owned_blocks", "overwrite_all"], "storage": _media_analysis_preferences_path()},
                "media_analysis.timed_marker_types": {"values": ["shots", "slate_clap", "sync_events", "best_moments", "qc_warnings"], "storage": _media_analysis_preferences_path()},
                "media_analysis.timed_marker_colors": {"values": "object mapping marker type to Resolve marker color", "storage": _media_analysis_preferences_path()},
                "media_analysis.max_timed_markers_per_clip": {"values": "0 for unlimited, or integer 1-250", "storage": _media_analysis_preferences_path()},
                "media_analysis.include_confidence_scores": {"values": [True, False], "storage": _media_analysis_preferences_path()},
                "media_analysis.include_source_time_notes": {"values": [True, False], "storage": _media_analysis_preferences_path()},
                "media_analysis.analysis_summary_style": {"values": ["concise", "assistant_editor", "qc", "producer", "full"], "storage": _media_analysis_preferences_path()},
                "media_analysis.report_format": {"values": ["compact", "full", "machine_readable"], "storage": _media_analysis_preferences_path()},
                "media_analysis.preferred_analysis_root": {"values": "absolute or expandable path", "storage": _media_analysis_preferences_path()},
                "media_analysis.preferred_generated_media_folder": {"values": "absolute or expandable path", "storage": _media_analysis_preferences_path()},
                "media_analysis.inventory_limit": {"description": "Maximum clips indexed during the Media Pool inventory walk.", "values": "integer 1..10000 (default 500)", "storage": _media_analysis_preferences_path()},
                "media_analysis.inventory_exclude_bins": {"description": "Comma-separated folder names to skip entirely during the inventory walk. Empty indexes every folder.", "values": "comma-separated folder names (default none)", "storage": _media_analysis_preferences_path()},
                "media_analysis.default_post_operation_page": {"values": ["stay_put", "media", "cut", "edit", "fusion", "color", "fairlight", "deliver"], "storage": _media_analysis_preferences_path()},
                "media_analysis.marker_custom_data": {"values": ["namespaced", "minimal"], "storage": _media_analysis_preferences_path()},
                "media_analysis.metadata_writeback_default": {"values": [True, False], "storage": _media_analysis_preferences_path()},
                "media_analysis.ask_before_metadata_publish": {"values": [True, False], "storage": _media_analysis_preferences_path()},
                "media_analysis.dry_run_first_default": {"values": [True, False], "storage": _media_analysis_preferences_path()},
                "media_analysis.sampling_mode_default": {
                    "description": "Frame-sampling mode for visual analysis. 'ask' prompts on first analysis to set a standing default. fixed=Economy (flat frames), per_minute=Balanced (duration-scaled), adaptive_capped=Thorough (content-aware, bounded — recommended), adaptive=Thorough uncapped.",
                    "values": ["ask", "fixed", "per_minute", "adaptive_capped", "adaptive"],
                    "storage": _media_analysis_preferences_path(),
                },
                "media_analysis.sampling_frames_per_minute": {"description": "Frames per minute for Balanced mode (also seeds Thorough on short clips).", "values": "number > 0 (default 4)", "storage": _media_analysis_preferences_path()},
                "media_analysis.sampling_frame_floor": {"description": "Minimum frames per clip for duration/content-scaled modes.", "values": "integer > 0 (default 3)", "storage": _media_analysis_preferences_path()},
                "media_analysis.sampling_frame_ceiling": {"description": "Maximum frames per clip for Balanced + Thorough modes (the Thorough per-clip cap).", "values": "integer > 0 (default 80)", "storage": _media_analysis_preferences_path()},
                "updates.mode": {
                    "description": "Local MCP update policy.",
                    "values": sorted(_SETUP_UPDATE_MODES),
                    "storage": str(update_state_path(project_dir)),
                },
                "updates.check_interval_hours": {"values": "number >= 0.1", "storage": str(update_state_path(project_dir))},
                "updates.snooze_hours": {"values": "number >= 0.1", "storage": str(update_state_path(project_dir))},
            },
        }

    if action in {"get_defaults", "get", "status"}:
        return _ok(defaults=_setup_defaults_snapshot())

    dry_run = _setup_bool(p.get("dry_run", p.get("dryRun")), False)

    if action in {"set_defaults", "set", "configure"}:
        defaults = p.get("defaults") if isinstance(p.get("defaults"), dict) else {}
        merged = {**defaults, **{k: v for k, v in p.items() if k != "defaults"}}
        media_defaults = {
            **_setup_nested(merged, "media_analysis", "mediaAnalysis"),
            **{
                key: value
                for key, value in merged.items()
                if key not in {"updates", "mcp_updates", "mcpUpdates", "dry_run", "dryRun"}
            },
            **({
                "timed_markers_default": _first_param(
                    merged,
                    "timed_markers_default",
                    "timedMarkersDefault",
                    "timed_markers",
                    "timedMarkers",
                    "write_markers",
                    "writeMarkers",
                    default=None,
                )
            } if any(key in merged for key in ("timed_markers_default", "timedMarkersDefault", "timed_markers", "timedMarkers", "write_markers", "writeMarkers")) else {}),
        }
        update_defaults = {
            **_setup_nested(merged, "updates", "mcp_updates", "mcpUpdates"),
            **({
                "mode": _first_param(
                    merged,
                    "update_mode",
                    "updateMode",
                    "update_policy",
                    "updatePolicy",
                    "mcp_update_policy",
                    "mcpUpdatePolicy",
                    default=None,
                )
            } if any(key in merged for key in ("update_mode", "updateMode", "update_policy", "updatePolicy", "mcp_update_policy", "mcpUpdatePolicy")) else {}),
            **({
                "check_interval_hours": _first_param(
                    merged,
                    "check_interval_hours",
                    "checkIntervalHours",
                    "update_interval_hours",
                    "updateIntervalHours",
                    default=None,
                )
            } if any(key in merged for key in ("check_interval_hours", "checkIntervalHours", "update_interval_hours", "updateIntervalHours")) else {}),
            **({
                "snooze_hours": _first_param(
                    merged,
                    "snooze_hours",
                    "snoozeHours",
                    "update_snooze_hours",
                    "updateSnoozeHours",
                    default=None,
                )
            } if any(key in merged for key in ("snooze_hours", "snoozeHours", "update_snooze_hours", "updateSnoozeHours")) else {}),
        }

        media_result = _setup_set_media_analysis_defaults(media_defaults, dry_run)
        if media_result.get("error"):
            return media_result
        update_result = _setup_set_updates_defaults(update_defaults, dry_run)
        if update_result.get("error"):
            return update_result
        recognized = bool(media_result.get("recognized")) or bool(update_result.get("recognized"))
        if not recognized:
            return _err("set_defaults did not receive a recognized default to set")

        return _ok(
            dry_run=dry_run,
            changes={
                "media_analysis": media_result,
                "updates": update_result,
            },
            defaults=_setup_defaults_snapshot(),
        )

    if action in {"clear_defaults", "clear", "reset"}:
        return _setup_clear_defaults(p.get("keys"), dry_run)

    return _unknown(action, ["schema", "get_defaults", "set_defaults", "clear_defaults"])


@mcp.tool()
def resolve_control(action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """App-level DaVinci Resolve operations.

    Actions:
      launch() -> {success, message}  — Launch DaVinci Resolve if not running. Call this FIRST if any tool returns a 'Not connected' error.
      get_version() -> {product, version, version_string, mcp: {version, update, update_decision}}
        — mcp.update_decision piggybacks the cached update check: if its action is
          "notify" or "prompt", mention the available MCP update to the user ONCE
          per session (do not nag, do not auto-apply; updates are applied via
          install.py or the control panel).
      mcp_update_status(force_check?) -> {version, update, decision}
      set_mcp_update_policy(mode) -> {success, version, update, decision}
      ignore_mcp_update() -> {success, version, update, decision}
      snooze_mcp_update(hours?) -> {success, version, update, decision}
      clear_mcp_update_preferences() -> {success, version, update, decision}
      api_truth(query?) -> {verified_on, count, facts}  — look up behaviorally-verified
        facts about quirky/unreliable Resolve API behavior (no connection needed).
      verification_stats() -> {stats}  — readback-verification tally
        (verified/contradicted/unverified) since server start (no connection needed).
      env_audit() -> {poisoned, preload, crashy_entries, tokens, message}  — reports
        whether THIS server process inherited a known-crashy LD_PRELOAD (e.g.
        NoMachine's libnxegl.so). Spawned children are sanitized, but in-process
        GPU code (CUDA/cuDNN, GL) can still crash when poisoned (no connection needed).
      job_status(job_id) -> {id, label, status, result?, error?, started_at, ended_at}
        — poll a background job started by a long op run with background=True
          (no connection needed). status is running, done, or error.
      list_jobs() -> {jobs}  — compact status of every known background job
        (no connection needed).
      get_page() -> {page}
      open_page(page) -> {success}  — page: edit, cut, color, fusion, fairlight, deliver
      get_keyframe_mode() -> {mode}
      set_keyframe_mode(mode) -> {success}
      quit() -> {success}
      get_fairlight_presets() -> {presets}
      set_high_priority() -> {success}
      disable_background_tasks_for_current_session() -> {success}  — Resolve 21+
      install_launch_shim() -> {success, installed, shim, desktop_entry, warnings}
        — Linux only. Installs a user-scoped shim (~/.local/bin/resolve plus a
          user-level .desktop override) so the Fairlight raw-hw ALSA config is
          applied however Resolve is started, not only when this connector
          spawns it. Without it, a desktop-launcher or terminal start wedges
          renders mid-run. Idempotent; refuses to overwrite files it did not write.
      uninstall_launch_shim() -> {success, removed, skipped_not_ours}
      launch_shim_status() -> {supported, installed, shim, desktop_entry, resolve_on_path, warnings}
      open_control_panel(port?, host?, open_browser?) -> {success, url, pid, port, status}
        — Launches the analysis control panel (src/dashboard/) as a background process.
          Idempotent: returns the existing URL if already running.
      control_panel_status() -> {running, pid, port, url}
      close_control_panel() -> {success, was_running}
      save_state() -> {state_token, page, current_timeline_id, current_timecode, selected_clip_ids}
        — Captures the current Resolve UI state so it can be restored after a preview.
      restore_state(state_token) -> {success, restored: {...}}
        — Returns Resolve to a previously-saved state.
    """
    p = params or {}

    # api_truth is a static knowledge lookup — no Resolve connection needed.
    if action == "api_truth":
        facts = lookup_api_truth(p.get("query"))
        return {"verified_on": _API_TRUTH_VERIFIED_ON, "count": len(facts), "facts": facts}
    if action == "verification_stats":
        # Process-level readback-verification tally — no connection needed.
        stats = _verification_stats()
        return {"stats": stats, "note": "Counts since server start. A rising "
                "'contradicted' count means the API reported success but a readback disagreed."}
    if action == "env_audit":
        # Does THIS process carry a known-crashy LD_PRELOAD? No connection needed.
        return preload_audit()

    # Background-job polling is a registry read — no Resolve connection needed.
    if action == "job_status":
        job_id = p.get("job_id")
        if not job_id:
            return _err("job_status requires job_id", category="invalid_input")
        status = background_jobs.job_status(job_id)
        if status is None:
            return _err(f"Unknown job_id: {job_id}", category="invalid_input")
        return status
    if action == "list_jobs":
        return {"jobs": background_jobs.list_jobs()}

    # Control-panel actions don't require Resolve to be running.
    if action == "open_control_panel":
        return _open_control_panel(p)
    elif action == "control_panel_status":
        return _control_panel_status()
    elif action == "close_control_panel":
        return _close_control_panel()
    elif action == "save_state":
        return _resolve_save_state()
    elif action == "restore_state":
        return _resolve_restore_state(p)

    # Launch-shim lifecycle: no Resolve connection needed (the whole point is
    # fixing how Resolve gets started in the first place).
    elif action == "install_launch_shim":
        return _launch_shim.install()
    elif action == "uninstall_launch_shim":
        return _launch_shim.uninstall()
    elif action == "launch_shim_status":
        return _launch_shim.status()

    if action == "mcp_update_status":
        return _mcp_update_status_payload(
            force=_media_analysis_bool(p.get("force_check", p.get("forceCheck")), False),
            timeout=float(p.get("timeout", 3.0)),
        )
    elif action == "set_mcp_update_policy":
        mode = str(p.get("mode") or "").strip().lower()
        if mode not in {"prompt", "auto", "notify", "never"}:
            return _err("set_mcp_update_policy requires mode: prompt, auto, notify, or never")
        set_update_mode(project_dir, mode, env=_setup_update_env())
        return _ok(**_mcp_update_status_payload())
    elif action == "ignore_mcp_update":
        update = get_cached_update_status(project_dir, VERSION, env=_setup_update_env())
        if update.get("status") != "update_available":
            return _err("No available MCP update is cached to ignore.")
        ignore_update_version(project_dir, update, env=_setup_update_env())
        return _ok(**_mcp_update_status_payload())
    elif action == "snooze_mcp_update":
        snooze_update_prompt(project_dir, hours=p.get("hours"), env=_setup_update_env())
        return _ok(**_mcp_update_status_payload())
    elif action == "clear_mcp_update_preferences":
        clear_update_prompt_preferences(project_dir, env=_setup_update_env())
        return _ok(**_mcp_update_status_payload())

    # launch works even when Resolve is not connected
    if action == "launch":
        r = get_resolve()  # auto-launches if not running
        if r is not None:
            # Surface a bypassed launch shim here rather than leaving it to
            # whoever thinks to call launch_shim_status: this runs before any
            # render, which is where the bypass would otherwise show up as a
            # wedge. Absent when there is nothing wrong.
            advisory = _launch_shim.launch_advisory()
            extra = {"launch_shim": advisory} if advisory else {}
            return _ok(message="DaVinci Resolve is running and connected.", **extra)
        return _err("Could not connect to DaVinci Resolve. Check that Resolve Studio is installed and 'External scripting using' is set to Local in Preferences.")

    r = get_resolve()  # auto-launches if not running
    if r is None:
        return _err("Could not connect to DaVinci Resolve after auto-launch attempt. Check that Resolve Studio is installed.")

    if action == "get_version":
        update_env = _setup_update_env()
        mcp_update = get_cached_update_status(project_dir, VERSION, env=update_env)
        return {
            "product": r.GetProductName(),
            "version": r.GetVersion(),
            "version_string": r.GetVersionString(),
            "mcp": {
                "version": VERSION,
                "update": mcp_update,
                "update_decision": update_prompt_decision(mcp_update, env=update_env),
            },
        }
    elif action == "get_page":
        return {"page": r.GetCurrentPage()}
    elif action == "open_page":
        err, clean = _validate_params(p, {
            "page": {"enum": ["media", "cut", "edit", "color", "fusion", "fairlight", "deliver"],
                     "required": True},
        })
        if err:
            return _err(err)
        # Serialize page switches so concurrent agents can't flip the single
        # globally-active page underneath each other.
        return {"success": bool(_open_page_serialized(r, clean["page"]))}
    elif action == "get_keyframe_mode":
        return {"mode": r.GetKeyframeMode()}
    elif action == "set_keyframe_mode":
        return {"success": bool(r.SetKeyframeMode(p["mode"]))}
    elif action == "quit":
        r.Quit()
        return _ok()
    elif action == "get_fairlight_presets":
        return {"presets": _ser(r.GetFairlightPresets())}
    elif action == "set_high_priority":
        return {"success": bool(r.SetHighPriority())}
    elif action == "disable_background_tasks_for_current_session":
        missing = _requires_method(r, "DisableBackgroundTasksForCurrentResolveSession", "21.0")
        if missing:
            return missing
        r.DisableBackgroundTasksForCurrentResolveSession()
        return _ok()
    return _unknown(action, ["launch","get_version","api_truth","verification_stats","env_audit","job_status","list_jobs","mcp_update_status","set_mcp_update_policy","ignore_mcp_update","snooze_mcp_update","clear_mcp_update_preferences","get_page","open_page","get_keyframe_mode","set_keyframe_mode","quit","get_fairlight_presets","set_high_priority","disable_background_tasks_for_current_session","open_control_panel","control_panel_status","close_control_panel","save_state","restore_state","install_launch_shim","uninstall_launch_shim","launch_shim_status"])


# ─── V2 C4: Per-field corrections with provenance + changelog ────────────────
#
# Until the SQLite source-of-truth migration (C1) lands, corrections live in a
# per-clip sidecar JSON at {clip_dir}/corrections.json. Schema mirrors the V2
# DB design (V2 schema — subjective_fields + field_changelog tables):
#
#   {
#     "schema_version": "2.0",
#     "clip_uuid": "...",
#     "current": {
#       "<entity_type>:<entity_uuid>:<field_path>": {
#         "value": <any JSON>,
#         "confidence": "low|medium|high",
#         "source": "human" | "vision_v0.2",
#         "author": "editor@example.com",
#         "timestamp": "2026-05-19T...Z"
#       }
#     },
#     "changelog": [
#       {previous_value, new_value, source, author, change_reason, timestamp}
#     ]
#   }
#
# Subsequent commit_vision / analyze runs must read this file and PRESERVE any
# field whose `source == "human"` (V2 trust-but-fix-optionally contract).
# Migration target: once C1 lands, ingest corrections.json into the DB tables.
















# ─── V2 P12: Control panel lifecycle ──────────────────────────────────────────

def _control_panel_pidfile() -> str:
    return os.path.expanduser("~/Documents/davinci-resolve-mcp-analysis/.control_panel.pid")


def _control_panel_read_state() -> Optional[Dict[str, Any]]:
    """Read the saved PID/port from the pidfile if present. Returns None if absent or unreadable."""
    path = _control_panel_pidfile()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _control_panel_pid_alive(pid: int) -> bool:
    """Check whether a PID is alive on this OS without killing it."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = existence check
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def _control_panel_status() -> Dict[str, Any]:
    state = _control_panel_read_state() or {}
    pid = int(state.get("pid") or 0)
    running = _control_panel_pid_alive(pid)
    if not running:
        # Stale pidfile — clean up
        if state:
            try:
                os.remove(_control_panel_pidfile())
            except OSError:
                pass
        return {"running": False, "pid": None, "port": None, "url": None}
    return {
        "running": True,
        "pid": pid,
        "port": state.get("port"),
        "host": state.get("host"),
        "url": state.get("url"),
        "started_at": state.get("started_at"),
    }


def _pick_dashboard_python(repo_root: str) -> Tuple[str, Optional[str]]:
    """Pick the interpreter to launch the dashboard with.

    Prefer a repo-local venv whose interpreter can import ``mcp`` — that
    survives the MCP server itself being started under system Python.
    Returns ``(executable, source)`` where ``source`` is "venv:<path>" when
    a venv was picked, ``"sys.executable"`` otherwise.
    """
    import subprocess
    import sys as _sys

    candidates = [
        os.path.join(repo_root, "venv", "bin", "python"),
        os.path.join(repo_root, ".venv", "bin", "python"),
        os.path.join(repo_root, "venv", "Scripts", "python.exe"),
        os.path.join(repo_root, ".venv", "Scripts", "python.exe"),
    ]
    for candidate in candidates:
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        try:
            result = subprocess.run(
                [candidate, "-c", "import mcp"],
                capture_output=True,
                timeout=5,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return candidate, f"venv:{candidate}"
    return _sys.executable, "sys.executable"


def _control_panel_probe(host: str, port: int, timeout: float = 1.5) -> Dict[str, Any]:
    """Probe a port to see whether a dashboard is listening and what version.

    Returns ``{"is_dashboard": bool, "version": Optional[str]}``.

    - ``is_dashboard`` is True when /api/boot responds with a recognizable
      dashboard payload (``success: true`` plus a project field). This lets
      callers distinguish an older dashboard that predates the
      ``mcp_version`` surface from a non-dashboard process squatting on the
      port.
    - ``version`` is the reported MCP version, or None if the dashboard
      predates the field.
    """
    import urllib.request
    url = f"http://{host}:{port}/api/boot"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return {"is_dashboard": False, "version": None}
    if not isinstance(payload, dict):
        return {"is_dashboard": False, "version": None}
    is_dashboard = bool(payload.get("success")) and any(
        k in payload for k in ("project_name", "project_id", "project_root")
    )
    cap = payload.get("capabilities")
    version: Optional[str] = None
    if isinstance(cap, dict) and cap.get("mcp_version"):
        version = str(cap["mcp_version"])
    elif payload.get("mcp_version"):
        version = str(payload["mcp_version"])
    return {"is_dashboard": is_dashboard, "version": version}


def _control_panel_remote_version(host: str, port: int, timeout: float = 1.5) -> Optional[str]:
    """Backwards-compat wrapper around :func:`_control_panel_probe`. None on failure."""
    return _control_panel_probe(host, port, timeout).get("version")


def _port_owner_pid(host: str, port: int) -> Optional[int]:
    """Return PID of the process LISTENing on `port`, or None if free/unknown.

    Uses lsof with `-iTCP:<port> -sTCP:LISTEN -t`: one PID per line, no header.
    Host is informational only — lsof matches any local LISTEN socket on that
    port (which is what we care about for port-collision detection).
    """
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t"],
            capture_output=True, timeout=3, text=True, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def _open_control_panel(p: Dict[str, Any]) -> Dict[str, Any]:
    import subprocess
    import socket
    import time as _t

    host = p.get("host") or "127.0.0.1"
    port = int(p.get("port") or 8765)
    force_restart = _media_analysis_bool(p.get("force_restart", p.get("forceRestart")), False)

    # Freshness check: probe the port directly (works whether or not THIS MCP
    # tracks the listener in its pidfile). This unifies two scenarios:
    #   1. Tracked stale: pidfile present, our prior MCP spawn still alive,
    #      now running an older VERSION.
    #   2. Untracked stale: process survived an MCP restart with
    #      start_new_session=True; pidfile was removed but the dashboard is
    #      still listening on the port.
    # Both surface as `status: "stale_running"` so the caller knows to
    # force_restart. A non-dashboard squatter on the port still falls through
    # to a port-collision error.
    existing = _control_panel_status()
    port_pid = _port_owner_pid(host, port)
    live_version = VERSION

    if port_pid is not None and not force_restart:
        probe = _control_panel_probe(host, port)
        tracked_url = (existing or {}).get("url") if existing.get("running") else None
        url = tracked_url or f"http://{host}:{port}"
        if probe["is_dashboard"]:
            remote_version = probe["version"]
            # Compare with explicit None handling: an older dashboard that
            # predates the mcp_version field is also stale — it can't honor
            # newer surfaces and the caller needs to know.
            if remote_version != live_version:
                reported = remote_version or "unknown (predates the mcp_version field)"
                return {
                    "success": True,
                    "status": "stale_running",
                    "url": url,
                    "pid": port_pid,
                    "port": port,
                    "running_version": remote_version,
                    "live_version": live_version,
                    "remediation": (
                        f"The running control panel reports version {reported} but the "
                        f"MCP server is at {live_version}. Re-call open_control_panel with "
                        "force_restart=true to terminate the stale process and relaunch."
                    ),
                }
            return {
                "success": True,
                "status": "already_running",
                "url": url,
                "pid": port_pid,
                "port": port,
                "running_version": remote_version,
                "note": "Control panel already running; returning existing URL.",
            }
        # Port held by a non-dashboard process — surface as collision.
        return _err(
            f"Port {port} is already in use by PID {port_pid} (not a control panel). "
            "Re-call with force_restart=true to terminate it, or pass a different port.",
        )

    # Force-restart path: kill whatever owns the port (could be the tracked PID
    # or an untracked stale process) before re-spawning.
    if force_restart:
        tracked_pid = int((existing or {}).get("pid") or 0)
        for victim in {tracked_pid, port_pid or 0} - {0}:
            try:
                os.kill(victim, 15)  # SIGTERM
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for _ in range(20):
            if _port_owner_pid(host, port) is None:
                break
            _t.sleep(0.1)
        try:
            os.remove(_control_panel_pidfile())
        except OSError:
            pass

    # Safety net: if force_restart couldn't free the port (e.g. PID owned by
    # another user, SIGTERM ignored), bail rather than spawning a child that
    # will crash silently with "Address already in use".
    port_pid = _port_owner_pid(host, port)
    if port_pid is not None:
        return _err(
            f"Port {port} is still in use by PID {port_pid} after force_restart. "
            "Pass a different port or kill the process manually.",
        )
    project_name = p.get("project_name") or "Dashboard Analysis"
    project_id = p.get("project_id") or "dashboard"
    analysis_root = p.get("analysis_root") or os.path.expanduser("~/Documents/davinci-resolve-mcp-analysis")
    open_browser = _media_analysis_bool(p.get("open_browser", p.get("openBrowser")), False)

    # Locate the repo root so we can run the dashboard module
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python_exe, python_source = _pick_dashboard_python(repo_root)
    cmd = [
        python_exe, "-m", "src.dashboard.main",
        "--host", str(host),
        "--port", str(port),
        "--project-name", str(project_name),
        "--project-id", str(project_id),
        "--analysis-root", str(analysis_root),
    ]
    if open_browser:
        cmd.append("--open")
    else:
        cmd.append("--no-open")

    # Detach so the dashboard outlives this MCP call.
    log_path = os.path.join(os.path.expanduser("~/Documents/davinci-resolve-mcp-analysis"), ".control_panel.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        log_handle = open(log_path, "a", encoding="utf-8")
    except OSError:
        log_handle = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=repo_root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as exc:
        return _err(f"Failed to launch control panel: {type(exc).__name__}: {exc}")
    finally:
        # The child holds its own copy of the fd; keeping ours open would leak
        # one descriptor per panel launch.
        if log_handle is not subprocess.DEVNULL:
            log_handle.close()

    # Verify the child actually came up. Bind errors (port in use), import
    # failures, etc. would otherwise leave us reporting "launched" while the
    # child has already died. Poll until the child is serving or we time out.
    serving = False
    for _ in range(40):  # ~4 seconds total
        rc = proc.poll()
        if rc is not None:
            # Child already exited — capture the tail of the log for diagnostics.
            tail = ""
            try:
                with open(log_path, "r", encoding="utf-8") as handle:
                    tail = handle.read()[-800:]
            except OSError:
                pass
            return _err(
                f"Control panel child exited (rc={rc}) before serving. "
                f"Log tail: {tail.strip()!r}",
            )
        try:
            with socket.create_connection((host, port), timeout=0.25):
                serving = True
                break
        except OSError:
            pass
        _t.sleep(0.1)
    if not serving:
        try:
            proc.terminate()
        except OSError:
            pass
        return _err(
            f"Control panel did not start accepting connections within 4s "
            f"(pid {proc.pid}). Check {log_path} for details.",
        )

    # Write the pidfile so subsequent calls find it
    url = f"http://{host}:{port}"
    state = {
        "pid": proc.pid,
        "port": port,
        "host": host,
        "url": url,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project_name": project_name,
        "project_id": project_id,
        "analysis_root": analysis_root,
        "log_path": log_path,
        "python_executable": python_exe,
        "python_source": python_source,
    }
    try:
        os.makedirs(os.path.dirname(_control_panel_pidfile()), exist_ok=True)
        with open(_control_panel_pidfile(), "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
    except OSError:
        pass  # non-fatal; status-check will just spawn a new one next time

    return {
        "success": True,
        "status": "launched",
        "url": url,
        "pid": proc.pid,
        "port": port,
        "host": host,
        "log_path": log_path,
        "python_executable": python_exe,
        "python_source": python_source,
        "note": (
            "Control panel launched in background. Open the URL in a browser, or "
            "call again with open_browser=true to auto-open. Use close_control_panel "
            "to terminate."
        ),
    }


# ─── V2 B4: Save / restore Resolve UI state for preview workflows ─────────────
#
# The control panel's "Open in Resolve at this timecode" flow wants to:
#   1. Save where the editor was (page, current timeline, timecode)
#   2. Preview a different clip in source viewer (via media_pool_item.open_in_viewer)
#   3. Restore the editor to their prior context once they close the preview
#
# State is held in-memory in a small token-keyed dict (single-user model).

_RESOLVE_STATE_SNAPSHOTS: Dict[str, Dict[str, Any]] = {}


def _resolve_save_state() -> Dict[str, Any]:
    r = get_resolve()
    if r is None:
        return _err("Not connected to DaVinci Resolve.")
    pm = r.GetProjectManager()
    proj = pm.GetCurrentProject() if pm else None
    state: Dict[str, Any] = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "page": r.GetCurrentPage(),
    }
    if proj is not None:
        try:
            tl = proj.GetCurrentTimeline()
        except Exception:
            tl = None
        if tl is not None:
            try:
                state["current_timeline_id"] = tl.GetUniqueId()
                state["current_timeline_name"] = tl.GetName()
                state["current_timecode"] = tl.GetCurrentTimecode()
            except Exception:
                pass
        try:
            mp = proj.GetMediaPool()
            if mp:
                selected = mp.GetSelectedClips() or []
                state["selected_clip_ids"] = [c.GetUniqueId() for c in selected if c]
                current_folder = mp.GetCurrentFolder()
                if current_folder is not None:
                    state["current_folder_name"] = current_folder.GetName()
        except Exception:
            pass
    token = short_hash(json.dumps(state, sort_keys=True, default=str), length=12)
    _RESOLVE_STATE_SNAPSHOTS[token] = state
    # Prune old snapshots (keep last 20)
    if len(_RESOLVE_STATE_SNAPSHOTS) > 20:
        oldest = sorted(_RESOLVE_STATE_SNAPSHOTS.items(), key=lambda kv: kv[1].get("saved_at") or "")
        for k, _ in oldest[:-20]:
            _RESOLVE_STATE_SNAPSHOTS.pop(k, None)
    return {"state_token": token, **state}


def _resolve_restore_state(p: Dict[str, Any]) -> Dict[str, Any]:
    token = p.get("state_token") or p.get("stateToken")
    if not token:
        return _err("restore_state requires state_token")
    state = _RESOLVE_STATE_SNAPSHOTS.get(token)
    if not state:
        return _err(f"Unknown state_token: {token}")
    r = get_resolve()
    if r is None:
        return _err("Not connected to DaVinci Resolve.")
    restored: Dict[str, Any] = {}

    # Restore page first so subsequent ops land in the right context
    if state.get("page"):
        try:
            r.OpenPage(state["page"])
            restored["page"] = state["page"]
        except Exception as exc:
            restored["page_error"] = str(exc)

    pm = r.GetProjectManager()
    proj = pm.GetCurrentProject() if pm else None
    if proj is not None and state.get("current_timeline_id"):
        try:
            count = proj.GetTimelineCount() or 0
            for i in range(1, count + 1):
                tl = proj.GetTimelineByIndex(i)
                if tl and tl.GetUniqueId() == state["current_timeline_id"]:
                    proj.SetCurrentTimeline(tl)
                    restored["current_timeline_id"] = state["current_timeline_id"]
                    if state.get("current_timecode"):
                        try:
                            tl.SetCurrentTimecode(state["current_timecode"])
                            restored["current_timecode"] = state["current_timecode"]
                        except Exception:
                            pass
                    break
        except Exception as exc:
            restored["timeline_error"] = str(exc)

    # Restore media pool selection
    if proj is not None and state.get("selected_clip_ids"):
        try:
            mp = proj.GetMediaPool()
            if mp:
                root = mp.GetRootFolder()
                for cid in state["selected_clip_ids"]:
                    found, parent = _find_clip_with_parent(root, cid)
                    if found and parent is not None:
                        mp.SetCurrentFolder(parent)
                        mp.SetSelectedClip(found)
                        restored["selected_clip_id"] = cid
                        break  # SetSelectedClip is singular; pick the first
        except Exception as exc:
            restored["selection_error"] = str(exc)

    return {"success": True, "state_token": token, "restored": restored}


def _close_control_panel() -> Dict[str, Any]:
    state = _control_panel_read_state()
    if not state:
        return {"success": True, "was_running": False, "note": "No control panel was running."}
    pid = int(state.get("pid") or 0)
    if not _control_panel_pid_alive(pid):
        try:
            os.remove(_control_panel_pidfile())
        except OSError:
            pass
        return {"success": True, "was_running": False, "note": "Stale pidfile cleaned up."}
    try:
        os.kill(pid, 15)  # SIGTERM
    except (ProcessLookupError, PermissionError, OSError) as exc:
        return _err(f"Failed to terminate control panel (pid {pid}): {exc}")
    # Best-effort: give it a moment to die, then remove pidfile
    import time as _t
    for _ in range(10):
        if not _control_panel_pid_alive(pid):
            break
        _t.sleep(0.1)
    try:
        os.remove(_control_panel_pidfile())
    except OSError:
        pass
    return {"success": True, "was_running": True, "pid": pid}


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 2: layout_presets
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 3: render_presets
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 4: project_manager
# ═══════════════════════════════════════════════════════════════════════════════




























































# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 5: project_manager_folders
# ═══════════════════════════════════════════════════════════════════════════════





# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 6: project_manager_cloud
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 7: project_manager_database
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 8: project_settings
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 9: render
# ═══════════════════════════════════════════════════════════════════════════════















































# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 10: media_storage
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 11: media_pool
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 12: folder
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 13: media_pool_item
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 14: media_pool_item_markers
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 15: media_analysis
# ═══════════════════════════════════════════════════════════════════════════════









# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 16: timeline
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def timeline_versioning(action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Timeline version-on-mutate, archive, rollback, and brain-edit history (C6).

    Every destructive timeline op auto-archives the working timeline to the Archive
    bin under an analysis_run_id; these actions let you inspect and control that.

    Actions:
      begin_run(label?, initiator?, analysis_run_id?) -> {success, analysis_run_id, label, initiator, started_at}
        Open a run. Subsequent destructive calls without an explicit
        analysis_run_id auto-thread this one — so a multi-step brain operation
        creates ONE archive instead of N. Pair with end_run to capture the
        cumulative metric delta.
      end_run(analysis_run_id?) -> {success, analysis_run_id, ended_at, summary}
        Close the active run (or a specific one). Aggregates brain_edits into
        per-metric rollup in analysis_runs.summary_json.
      list_runs(limit?) -> {success, runs}
        Recent runs with their summaries, newest first.
      archive_current(reason?, analysis_run_id?) -> {success, timeline_name, archived_timeline_name, version, archive_bin, row_id}
        Manually checkpoint the current timeline. If analysis_run_id is supplied
        and that run already archived the current timeline, this is a no-op.
      list_versions(timeline_name) -> [{version, archived_timeline_name, created_at, ...}]
        Version chain for `timeline_name`, oldest first. Includes any retention-
        collapsed versions (drt_export_path populated).
      diff_timelines(from_timeline, to_timeline) -> {added, removed, moved, trimmed, summary}
        Structural diff between two LIVE timelines by name (read-only; no
        archived versions needed). Built for edit-engine variants — tighten/
        selects produce new-name timelines with no shared version chain.
      get_history(timeline_name?, analysis_run_id?, limit?) -> [{edit_type, target_metric, before_value, after_value, delta, ...}]
        Brain-edit history. Filter by timeline_name or analysis_run_id; defaults
        to the most recent 50 across the project.
      rollback(timeline_name, version, analysis_run_id?) -> {success, restored_timeline_name, archive_of_previous}
        Restore an archived version. Archives current state first; restored copy
        gets a "_rolled_back_<HHMMSS>" suffix.
      prune(timeline_name, keep_n=10) -> {success, pruned, kept, details}
        Collapse old versions to .drt exports under _soul/timeline_versions/<slug>/,
        delete the archived timeline from the bin, keep the DB row for rollback.
      registry() -> {entries, registry_path}
        Read the cross-project brain_edits registry that lives one level above
        each project root (analogous to analysis_registry.json).
    """
    r = get_resolve()
    if r is None:
        return _err("Resolve not available")
    ctx = _destructive_versioning_provider()
    if ctx is None:
        return _err("No current project / can't resolve project root")
    resolve_h, project_h, project_root, project_name = ctx
    p = params or {}

    if action == "begin_run":
        return _analysis_runs.begin_run(
            project_root=project_root,
            label=p.get("label"),
            initiator=p.get("initiator"),
            analysis_run_id=p.get("analysis_run_id"),
        )
    if action == "end_run":
        return _analysis_runs.end_run(
            project_root=project_root,
            analysis_run_id=p.get("analysis_run_id"),
        )
    if action == "list_runs":
        return {
            "success": True,
            "runs": _analysis_runs.list_runs(project_root, limit=_safe_int(p.get("limit"), 50, minimum=1, maximum=1000)),
        }
    if action == "archive_current":
        return _timeline_versioning.archive_current_timeline(
            resolve=resolve_h,
            project=project_h,
            project_root=project_root,
            reason=p.get("reason"),
            analysis_run_id=p.get("analysis_run_id"),
        )
    if action == "list_versions":
        if not p.get("timeline_name"):
            return _err("timeline_name required")
        rows = _timeline_versioning.list_timeline_versions(
            project_root=project_root,
            timeline_name=str(p["timeline_name"]),
        )
        return {"success": True, "versions": rows}
    if action == "diff_versions":
        if not p.get("timeline_name") or "from_version" not in p or "to_version" not in p:
            return _err("timeline_name, from_version, to_version required")
        try:
            from_version = int(p["from_version"])
            to_version = int(p["to_version"])
        except (TypeError, ValueError):
            return _err("from_version and to_version must be integers")
        return {
            "success": True,
            **_timeline_versioning.diff_versions(
                project_root=project_root,
                timeline_name=str(p["timeline_name"]),
                from_version=from_version,
                to_version=to_version,
            ),
        }
    if action == "diff_timelines":
        if not p.get("from_timeline") or not p.get("to_timeline"):
            return _err("from_timeline and to_timeline required")
        return _timeline_versioning.diff_timelines(
            project=project_h,
            from_timeline=str(p["from_timeline"]),
            to_timeline=str(p["to_timeline"]),
        )
    if action == "get_history":
        rows = _brain_edits.get_brain_edit_history(
            project_root=project_root,
            timeline_name=p.get("timeline_name"),
            analysis_run_id=p.get("analysis_run_id"),
            limit=_safe_int(p.get("limit"), 50, minimum=1, maximum=1000),
        )
        return {"success": True, "edits": rows}
    if action == "rollback":
        if not p.get("timeline_name") or "version" not in p:
            return _err("timeline_name and version required")
        try:
            version = int(p["version"])
        except (TypeError, ValueError):
            return _err("version must be an integer")
        if version < 0:
            return _err("version must be >= 0")
        return _timeline_versioning.rollback_to_version(
            resolve=resolve_h,
            project=project_h,
            project_root=project_root,
            timeline_name=str(p["timeline_name"]),
            version=version,
            analysis_run_id=p.get("analysis_run_id"),
        )
    if action == "prune":
        if not p.get("timeline_name"):
            return _err("timeline_name required")
        return _timeline_versioning.prune_archived_versions(
            resolve=resolve_h,
            project=project_h,
            project_root=project_root,
            timeline_name=str(p["timeline_name"]),
            keep_n=_safe_int(p.get("keep_n"), 10, minimum=1, maximum=1000),
        )
    if action == "registry":
        return {"success": True, **_brain_edits.read_brain_edits_registry(project_root)}
    if action == "media_pool_changes":
        return {
            "success": True,
            "changes": _media_pool_changes.get_media_pool_change_history(
                project_root=project_root,
                analysis_run_id=p.get("analysis_run_id"),
                action=p.get("media_pool_action"),
                limit=_safe_int(p.get("limit"), 50, minimum=1, maximum=1000),
            ),
        }
    return _err(f"Unknown action: {action}")


















# ═══════════════════════════════════════════════════════════════════════════════
# TOOL: auto_edit — brief-to-rendered-video pipeline (Phase 1: talking head)
# ═══════════════════════════════════════════════════════════════════════════════
















# ═══════════════════════════════════════════════════════════════════════════════
# TOOL: orchestrate — resumable ingest-to-deliver post conductor (Phase 1: state
# machine + persistence only; run_stage/gates/rollback land in later phases)
# ═══════════════════════════════════════════════════════════════════════════════














































# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 16: timeline_markers
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 17: timeline_ai
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 18: timeline_item
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 19: timeline_item_markers
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 20: timeline_item_fusion
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 21: timeline_item_color
# ═══════════════════════════════════════════════════════════════════════════════










































# ───────────────────────────────────────────────────────────────────────────
# B4 — action_help dict. Long-form per-action guidance + examples are pulled
# on demand via the action_help sub-action so the top-level docstring (sent on
# every tool catalog turn) can stay short.
# ───────────────────────────────────────────────────────────────────────────











# Full valid-action list per tool, for tools that expose one. Lets action_help
# report every action (not only the documented subset) and tell an unknown
# action apart from a valid-but-undocumented one.









# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 22: timeline_item_takes
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 23: gallery
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 24: gallery_stills
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 25: graph
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 26: color_group
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 27: fusion_comp
# ═══════════════════════════════════════════════════════════════════════════════
















































# Friendly mask aliases -> Fusion tool RegID, for add_fusion_mask (issue #73).

# Friendly param name -> Fusion input id, for add_fusion_mask. Center is handled
# separately because it is a Point input (see _fusion_set_point_input).














# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 28: fuse_plugin
# ═══════════════════════════════════════════════════════════════════════════════

# Fusion's naming rule (Fuse SDK p. 40): identifiers must match this pattern,
# else the resulting comp will save but fail to reopen.














# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 29: dctl
# ═══════════════════════════════════════════════════════════════════════════════

# Fuse identifier rules don't apply to DCTL filenames, but we still want safe
# filesystem names. Disallow path separators and shell-hostile characters.




# LUT relocation for Graph.SetLUT lives in src.domains.color_grade.utils.lut_paths so server.py and
# src/granular/graph.py share one implementation (see the module docstring for
# the live-verified behavior).














# ═══════════════════════════════════════════════════════════════════════════════
# TOOL 30: script_plugin
# ═══════════════════════════════════════════════════════════════════════════════

# Resolve-page Lua/Python scripts must be filesystem-safe identifiers.














# ─── Script execution ─────────────────────────────────────────────────────────














































# ═══════════════════════════════════════════════════════════════════════════════
# MCP Resources — agentic-flow improvement E1
#
# Resources are read-only state surfaces that hosts can pull WITHOUT consuming
# a turn (unlike tools, which require a tool_use round-trip). They mirror
# state-read tools (get_version, get_current project/timeline, get_caps,
# capabilities) so hosts that consume MCP resources don't have to spend turns
# on passive state polling. The equivalent tools are intentionally kept — they
# remain the lowest-common-denominator path for hosts that ignore resources.
#
# All resource handlers MUST be cheap; they're called by the host without
# user awareness. Heavy work (analysis, Resolve scripting that may block)
# stays in tools.
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_resource(fn):
    """Wrap a resource handler so an exception returns a structured error dict
    instead of bubbling out of the MCP transport."""
    import functools

    @functools.wraps(fn)
    def _wrap():
        try:
            return fn()
        except Exception as exc:
            return {"error": {
                "code": "RESOURCE_FAILED",
                "category": "resolve_api_failed",
                "retryable": True,
                "message": f"{type(exc).__name__}: {exc}",
            }}
    return _wrap


@mcp.resource("status://mcp_version")
@_safe_resource
def _resource_mcp_version() -> Dict[str, Any]:
    """Server version, build, and update channel. Pure read — no Resolve required."""
    return {
        "version": VERSION,
        "channel": get_update_channel(),
    }


@mcp.resource("status://resolve_connection")
@_safe_resource
def _resource_resolve_connection() -> Dict[str, Any]:
    """Whether Resolve is currently reachable, and what version. Cheap probe."""
    r = get_resolve()
    if r is None:
        return {"connected": False}
    try:
        product = r.GetProductName()
        version = r.GetVersion()
        return {"connected": True, "product": product, "version": version}
    except Exception as exc:
        return {"connected": False, "error": f"{type(exc).__name__}: {exc}"}


@mcp.resource("status://current_project")
@_safe_resource
def _resource_current_project() -> Dict[str, Any]:
    """Current Resolve project name + id, or null if no project is open."""
    r = get_resolve()
    if r is None:
        return {"open": False}
    pm = r.GetProjectManager()
    if pm is None:
        return {"open": False}
    proj = pm.GetCurrentProject()
    if proj is None:
        return {"open": False}
    return {
        "open": True,
        "name": proj.GetName(),
        "id": proj.GetUniqueId() if hasattr(proj, "GetUniqueId") else None,
    }


@mcp.resource("status://current_timeline")
@_safe_resource
def _resource_current_timeline() -> Dict[str, Any]:
    """Current timeline name, id, start/end frame, start TC — or null if none."""
    r = get_resolve()
    if r is None:
        return {"open": False}
    pm = r.GetProjectManager()
    if pm is None:
        return {"open": False}
    proj = pm.GetCurrentProject()
    if proj is None:
        return {"open": False}
    tl = proj.GetCurrentTimeline()
    if tl is None:
        return {"open": False}
    return {
        "open": True,
        "name": tl.GetName(),
        "id": tl.GetUniqueId() if hasattr(tl, "GetUniqueId") else None,
        "start_frame": tl.GetStartFrame(),
        "end_frame": tl.GetEndFrame(),
        "start_timecode": tl.GetStartTimecode(),
    }


@mcp.resource("status://caps_preset")
@_safe_resource
def _resource_caps_preset() -> Dict[str, Any]:
    """Active analysis caps preset + overrides + effective caps. No Resolve required."""
    from src.domains.media_analysis.utils import analysis_caps as _ac
    active = _ac.resolve_caps(_caps_preset_provider(), _caps_overrides_provider())
    return {
        "preset": active.preset,
        "overrides": _caps_overrides_provider() or {},
        "effective_caps": active.to_dict(),
        "presets_available": _ac.list_presets(),
    }


@mcp.resource("analysis://recent_reports")
@_safe_resource
def _resource_recent_reports() -> Dict[str, Any]:
    """Last 20 published analysis reports (clip_id, signature, completed_at, path)
    from the global analysis registry. Catalog read — no Resolve required."""
    try:
        from src.core import analysis_runs  # noqa: F401
    except Exception:
        pass
    registry_path = os.path.join(
        os.path.expanduser("~"),
        "Documents",
        "davinci-resolve-mcp-analysis",
        "analysis_registry.json",
    )
    if not os.path.isfile(registry_path):
        return {"registry_path": registry_path, "entries": [], "available": False}
    try:
        with open(registry_path, "r", encoding="utf-8") as fh:
            registry = json.load(fh)
    except Exception as exc:
        return {"registry_path": registry_path, "entries": [],
                "error": f"{type(exc).__name__}: {exc}"}
    # Registry is typically a dict keyed by signature; surface as a list, newest first.
    entries = []
    if isinstance(registry, dict):
        for sig, rec in registry.items():
            if not isinstance(rec, dict):
                continue
            entries.append({
                "analysis_signature": sig,
                "clip_id": rec.get("clip_id"),
                "clip_name": rec.get("clip_name"),
                "completed_at": rec.get("completed_at") or rec.get("published_at"),
                "report_path": rec.get("analysis_report_path") or rec.get("path"),
            })
    entries.sort(key=lambda e: e.get("completed_at") or "", reverse=True)
    return {
        "registry_path": registry_path,
        "available": True,
        "entries": entries[:20],
    }


@mcp.resource("capabilities://installed_tools")
@_safe_resource
def _resource_installed_tools() -> Dict[str, Any]:
    """Detected tool installations (ffmpeg, ffprobe, whisper, etc.) — doesn't change mid-session."""
    return _media_analysis_capabilities_for_request(None)


@mcp.resource("capabilities://install_guidance")
@_safe_resource
def _resource_install_guidance() -> Dict[str, Any]:
    """Install guidance for missing analysis tools. Reference data; no installs performed."""
    caps = _media_analysis_capabilities_for_request(None)
    return media_analysis_install_guidance(caps)


# ═══════════════════════════════════════════════════════════════════════════════
# Server Startup
# ═══════════════════════════════════════════════════════════════════════════════

def _install_threaded_tool_dispatch(fastmcp) -> int:
    """Run synchronous tool bodies in a worker thread instead of inline on the
    event loop.

    The MCP SDK invokes a sync tool function directly on the single asyncio
    event-loop thread, so a blocking Resolve call (or subprocess, or the up-to-
    60s launch wait) freezes the whole server — including the stdio read loop —
    until it returns. Wrapping each sync tool as an async function that offloads
    to a worker thread keeps the event loop servicing the transport. Bodies are
    serialized on _bridge_lock so the single-threaded Resolve bridge is never
    entered concurrently. A body that outlives a client cancellation runs to
    completion (the bridge is never left half-mutated) still holding the lock.

    Couples to mcp SDK private attrs (ToolManager._tools, Tool.fn /
    Tool.is_async; verified on mcp 1.27). Best-effort: if that shape changes,
    leave the tools as-is, falling back to the current inline behavior.
    """
    import functools
    import anyio

    manager = getattr(fastmcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict) or not tools:
        return 0

    def _offloaded(fn):
        @functools.wraps(fn)
        async def run_off_thread(**kwargs):
            def call():
                with _bridge_lock:
                    return fn(**kwargs)
            return await anyio.to_thread.run_sync(call)
        return run_off_thread

    wrapped = 0
    for tool in tools.values():
        if getattr(tool, "is_async", False):
            continue
        try:
            tool.fn = _offloaded(tool.fn)
            tool.is_async = True
        except Exception as exc:  # unexpected SDK shape — keep the original tool
            logger.warning(f"threaded tool dispatch skipped for {getattr(tool, 'name', '?')}: {exc}")
            continue
        wrapped += 1
    logger.info(f"Threaded tool dispatch installed for {wrapped} tools")
    return wrapped


def _log_preload_audit() -> None:
    """Warn prominently if THIS process inherited a known-crashy LD_PRELOAD.

    Spawn sites are sanitized, but in-process GPU code can't be; surface the
    poisoning at boot rather than letting it fail obscurely later. Warn-only by
    design (all spawn sites already strip it) — see resolve_control env_audit
    and docs/guides/media-analysis-guide.md.
    """
    audit = preload_audit()
    if audit["poisoned"]:
        logger.warning("ENV AUDIT: %s", audit["message"])



# ─── Domain action modules (restructure epic #52, Phase 3 / #46) ──────────────
# Importing each domain's actions module registers its mcp-tool-decorated
# functions (a side effect of module import); the explicit `import X as X`
# form re-exports every name into this module's namespace too, so
# `src.server.<name>` keeps working for tests/tooling that expect it.
from src.domains.color_grade.actions import (
    _COLOR_GRADE_KERNEL_ACTIONS,
    _COLOR_ITEM_METHODS,
    _GRAPH_METHODS,
    _LUT_EXPORT_TYPES,
    _PROPOSE_GRADE_OPERATION_CLASSES,
    _bulk_match_to_hero,
    _color_graph_from_params,
    _color_group_capabilities,
    _copy_grades,
    _extract_clip_frames,
    _gallery_capabilities,
    _grade_boundary_report,
    _grade_capabilities,
    _grade_evidence_base,
    _grade_evidence_line,
    _grade_item_snapshot,
    _grade_temp_path_ok,
    _grade_version_restore,
    _grade_version_snapshot,
    _graph_snapshot,
    _normalize_cdl,
    _probe_color_node_graph,
    _propose_grade,
    _propose_grade_validate,
    _resolve_lut_export_type,
    _safe_apply_drx,
    _safe_copy_grade,
    _safe_export_lut,
    _safe_set_cdl,
    _timeline_items_for_grade_copy,
    _validate_cdl_payload,
    color_group,
    gallery,
    gallery_stills,
    graph,
    logger,
    timeline_item_color,
)
from src.domains.timeline_edit.actions import (
    _DUPLICATE_COPY_ALL,
    _DUPLICATE_COPY_GROUP_ALIASES,
    _DUPLICATE_KEYFRAME_PROPERTIES,
    _DUPLICATE_PLACEMENTS,
    _IMPORT_INTO_TIMELINE_OPTION_KEYS,
    _LIVE_CLIP_WHERE_FILTERS,
    _OVERLAY_INSERTERS,
    _VOICE_ISOLATION_STATE_KEYS,
    _WRITEBACK_VERIFY_HINTS,
    _WRITEBACK_VERIFY_KEYS,
    _advanced_edit_lookup_clip_db_id,
    _advanced_edit_resolve_clip,
    _advanced_timeline_edit,
    _append_and_recover_timeline_item,
    _append_clip_info_from_timeline_item,
    _build_append_clip_info_dict,
    _coerce_duplicate_int,
    _copy_cache_state,
    _copy_clip_color,
    _copy_duplicate_item_state,
    _copy_enabled_state,
    _copy_flags,
    _copy_fusion_comps,
    _copy_keyframes,
    _copy_property_group,
    _copy_takes,
    _find_appended_timeline_item_summary,
    _find_next_gap_record_frame,
    _linked_items_for_duplicate,
    _merge_property_groups,
    _normalize_clip_property_value,
    _normalize_copy_properties,
    _normalize_duplicate_placement,
    _normalize_include_linked,
    _normalize_record_frame,
    _resolve_duplicate_record_frame,
    _resolve_duplicate_track_index,
    _safe_place_overlay,
    _serialize_appended_timeline_item,
    _timeline_add_transition_impl,
    _timeline_apply_look_to_items,
    _timeline_bulk_set_item_properties,
    _timeline_bulk_set_title_text,
    _timeline_clip_where,
    _timeline_contact_sheet_samples,
    _timeline_copy_range_impl,
    _timeline_create_variant_from_ranges,
    _timeline_duplicate_clips_impl,
    _timeline_edit_kernel_capabilities,
    _timeline_fit_to_fill_edit_impl,
    _timeline_import_srt_impl,
    _timeline_insert_edit_impl,
    _timeline_lift_range_impl,
    _timeline_list_transitions_impl,
    _timeline_marker_thumbnail_review,
    _timeline_move_clip_impl,
    _timeline_place_on_top_edit_impl,
    _timeline_probe_edit_kernel_item,
    _timeline_render_in_place_impl,
    _timeline_replace_edit_impl,
    _timeline_set_clip_speed_impl,
    _timeline_set_title_text,
    _timeline_slide_clip_impl,
    _timeline_slip_clip_impl,
    _timeline_split_clip_impl,
    _timeline_story_spine_report,
    _timeline_thumbnail_contact_sheet,
    _timeline_title_property_scan,
    _timeline_trim_clip_impl,
    _unique_timeline_name,
    _variant_item_placement,
    _verify_clip_property_writeback,
    _verify_writeback,
    logger,
    timeline,
    timeline_ai,
    timeline_item,
    timeline_item_takes,
)
from src.domains.timeline_conform_interchange.actions import (
    _BINARY_INTERCHANGE_EXTS,
    _PRPROJ_REFUSAL,
    _SEQ_CONTAINER_RE,
    _TIMELINE_EXPORT_ALIASES,
    _binary_post_import_relink,
    _build_relink_plan,
    _compare_timeline_append_readback,
    _compare_timeline_snapshots,
    _compare_timelines,
    _conform_boundary_report,
    _conform_capabilities,
    _detect_gaps_overlaps_from_snapshot,
    _detect_missing_media,
    _detect_missing_media_from_snapshot,
    _drp_seq_containers,
    _export_timeline_checked,
    _extract_seqcontainer_from_drp,
    _import_from_drp,
    _import_timeline_checked,
    _missing_media_diagnosis,
    _probe_interchange_roundtrip,
    _source_ranges_from_snapshot,
    _story_spine_from_snapshot,
    _timeline_conform_snapshot,
    _timeline_export_spec,
    _timeline_export_value,
    _timeline_identity,
    _timeline_item_conform_summary,
    _timeline_media_coverage,
    _timeline_media_type,
    logger,
)
from src.domains.render_deliver.actions import (
    _RENDER_KERNEL_ACTIONS,
    _RENDER_METHODS,
    _RENDER_SETTING_KEYS,
    _build_proxies,
    _export_render_boundary_report,
    _prepare_render_job,
    _probe_render_matrix,
    _quick_export_capabilities,
    _render_capabilities,
    _render_codecs,
    _render_cut_summary_for,
    _render_format_id,
    _render_format_id_from_formats,
    _render_format_requested,
    _render_formats,
    _render_job_lifecycle_probe,
    _render_settings_snapshot,
    _render_temp_path_ok,
    _resolve_proxy_clips,
    _safe_quick_export,
    _safe_set_render_settings,
    _validate_render_settings_action,
    _validate_render_settings_payload,
    logger,
    render,
    render_presets,
)
from src.domains.fusion_composition.actions import (
    _COMMON_FUSION_TOOLS,
    _FUSION_GROUP_KERNEL_ACTIONS,
    _FUSION_KERNEL_ACTIONS,
    _MASK_INPUT_ALIASES,
    _MASK_TOOL_ALIASES,
    _find_fusion_group,
    _fusion_add_mask,
    _fusion_boundary_report,
    _fusion_comp_bulk_set_expressions,
    _fusion_comp_bulk_set_inputs,
    _fusion_comp_snapshot,
    _fusion_find_text_tool,
    _fusion_flow_view,
    _fusion_get_text_plus,
    _fusion_graph_capabilities,
    _fusion_group_advisory,
    _fusion_group_settings_export,
    _fusion_group_settings_load,
    _fusion_group_settings_splice_inputs,
    _fusion_probe_group_published_inputs,
    _fusion_set_point_input,
    _fusion_set_text_plus,
    _fusion_tool_names,
    _fusion_tool_summary,
    _get_fusion_comp_on_timeline_item,
    _get_timeline_item_for_fusion,
    _has_fusion_timeline_scope,
    _iter_fusion_tools,
    _parse_pos,
    _probe_fusion_tool,
    _resolve_fusion_comp,
    _resolve_setting_path,
    _safe_add_fusion_tool,
    _safe_connect_fusion_tools,
    _safe_set_fusion_inputs,
    fusion_comp,
    logger,
    timeline_item_fusion,
)
from src.domains.audio_fairlight.actions import (
    _AUDIO_PROPERTY_KEYS,
    _AUTO_CAPTION_FIELD_SPECS,
    _AUTO_CAPTION_LANGUAGES,
    _AUTO_CAPTION_LINE_BREAKS,
    _AUTO_CAPTION_PRESETS,
    _audio_capabilities,
    _audio_item_from_params,
    _audio_mapping_report,
    _audio_mix_capability_report,
    _audio_track_probe,
    _copy_voice_isolation,
    _fairlight_boundary_report,
    _normalize_auto_caption_settings,
    _normalize_auto_sync_settings,
    _probe_audio_item,
    _safe_auto_sync_audio,
    _safe_create_subtitles,
    _safe_set_audio_properties,
    _subtitle_generation_probe,
    _synced_audio,
    _timeline_item_audio_snapshot,
    _timeline_set_clip_pan_impl,
    _timeline_set_clip_volume_impl,
    _timeline_transcript,
    _transcription_capabilities,
    _variant_audio_summary,
    _voice_isolation_capabilities,
    logger,
)
from src.domains.media_pool_ingest.actions import (
    _IMPORT_TIMELINE_OPTION_KEYS,
    _MEDIA_POOL_ITEM_METHODS,
    _MEDIA_POOL_KERNEL_ACTIONS,
    _MEDIA_POOL_KNOWN_CLIP_PROPERTIES,
    _MEDIA_POOL_METHODS,
    _METADATA_FIELD_PROPERTY_ALIASES,
    _METADATA_PANEL_GROUP_FIELD_HINTS,
    _METADATA_PANEL_GROUP_ORDER,
    _add_proxy_mismatch,
    _add_proxy_resolution_mismatch,
    _append_to_timeline_verified_operation,
    _append_to_timeline_with_verification,
    _bounded_basename_matches,
    _build_create_clip_info_dict,
    _check_proxy_media_compatibility,
    _check_proxy_media_compatibility_checked,
    _clear_clip_marks,
    _clip_media_signature,
    _collect_media_pool_items,
    _compact_structural_diff,
    _copy_clip_annotations,
    _copy_metadata,
    _ensure_folder_path,
    _ensure_timeline_tracks,
    _find_clip_by_file_path,
    _find_folder_by_id,
    _folder_probe,
    _format_sequence_path,
    _imported_clip_summaries,
    _link_full_resolution_checked,
    _link_proxy_checked,
    _media_path_volume_root,
    _media_pool_boundary_report,
    _media_pool_ingest_capabilities,
    _media_pool_item_probe,
    _media_pool_probe,
    _media_pool_probe_ingest_items,
    _media_token_matches,
    _metadata_clip_property_key_for_field,
    _metadata_field_inventory,
    _metadata_panel_group_for_field,
    _metadata_panel_group_inventory,
    _missing_sequence_frames,
    _mp_rename_folder_live,
    _navigate_folder,
    _normalize_media_token,
    _normalize_metadata,
    _normalized_path_for_compare,
    _organize_clips,
    _parse_int_text,
    _parse_rate,
    _parse_resolution,
    _probe_clip_properties,
    _probe_media_file,
    _proxy_link_journal_event,
    _proxy_link_readback,
    _proxy_media_signature,
    _proxy_readback_matches_path,
    _resolve_folder_ids,
    _resolve_timeline_create_policy,
    _restore_current_folder,
    _safe_import_folder,
    _safe_import_media,
    _safe_import_sequence,
    _safe_relink,
    _safe_unlink,
    _sanitize_media_path,
    _set_clip_marks,
    _set_current_folder_temporarily,
    _set_multicam_track_names,
    _setup_multicam_timeline,
    _timeline_append_readback_snapshot,
    _verified_operation,
    folder,
    logger,
    media_pool,
    media_pool_item,
    media_storage,
)
from src.domains.auto_edit.actions import (
    _STRATA_CLIP_REF_KEYS,
    _apply_cuts_skip_reason,
    _auto_edit_build_rows,
    _auto_edit_ffprobe_ok,
    _auto_edit_project_context,
    _edit_engine_capture,
    _edit_engine_collect_items,
    _edit_engine_find_slot_item,
    _edit_engine_linked_audio_tracks,
    _edit_engine_timeline_fps,
    _edit_engine_track_counts,
    _is_montage_plan,
    _strata_clip_ref,
    auto_edit,
    edit_engine,
    logger,
)
from src.domains.media_analysis.actions import (
    DEFAULT_TIMELINE_MARKER_REVIEW_PROMPT,
    V2_MACHINE_MARKER_WRITEBACK_ENABLED,
    _MCP_METADATA_BLOCK_END,
    _MCP_METADATA_BLOCK_START,
    _MCP_METADATA_PROVENANCE_PREFIX,
    _MEDIA_ANALYSIS_CONTAINER_TYPE_PARTS,
    _MEDIA_ANALYSIS_FILL_EMPTY_FIELDS,
    _MEDIA_ANALYSIS_LIST_FIELDS,
    _apply_media_analysis_clip_markers,
    _apply_sync_event_markers,
    _caps_overrides_provider,
    _caps_preset_provider,
    _compact_clip_row_for_response,
    _compact_manifest_for_response,
    _compact_metadata_publish_for_response,
    _media_analysis_apply_setup_defaults,
    _media_analysis_best_sync_event,
    _media_analysis_capabilities_for_request,
    _media_analysis_chat_context_slate_review,
    _media_analysis_clip_record,
    _media_analysis_collect_slate_fields,
    _media_analysis_confidence_rank,
    _media_analysis_confirmed,
    _media_analysis_confirmed_slate_sources,
    _media_analysis_dedupe_records,
    _media_analysis_float,
    _media_analysis_folder_records,
    _media_analysis_host_chat_image_review_payload,
    _media_analysis_make_marker,
    _media_analysis_marker_candidates_from_report,
    _media_analysis_marker_note,
    _media_analysis_marker_options,
    _media_analysis_marker_writeback_enabled,
    _media_analysis_merge_lists,
    _media_analysis_merge_metadata_field,
    _media_analysis_metadata_text,
    _media_analysis_metadata_writeback_enabled,
    _media_analysis_missing_capabilities_response,
    _media_analysis_note_items,
    _media_analysis_pick_nested,
    _media_analysis_provenance_metadata,
    _media_analysis_publish_confirmed,
    _media_analysis_record_analyzable,
    _media_analysis_records_from_target,
    _media_analysis_replace_owned_block,
    _media_analysis_report_fps,
    _media_analysis_report_metadata_candidates,
    _media_analysis_sampling_mode_decision,
    _media_analysis_sampling_mode_prompt,
    _media_analysis_sequence_track_types,
    _media_analysis_slate_visible,
    _media_analysis_slate_visual_confirmed,
    _media_analysis_sync_event_records,
    _media_analysis_sync_marker_suggestions,
    _media_analysis_target_dict,
    _media_analysis_time_to_frame,
    _media_analysis_timed_marker_decision,
    _media_analysis_timed_marker_prompt,
    _media_analysis_timeline_records,
    _media_analysis_transcription_options,
    _media_analysis_vision_options,
    _media_analysis_visual_analysis_succeeded,
    _publish_clip_metadata_from_analysis,
    _v2_corrections_path_for_clip,
    _v2_get_field_history,
    _v2_list_corrections,
    _v2_read_corrections,
    _v2_revert_field,
    _v2_update_field,
    _v2_write_corrections,
    logger,
    media_analysis,
)
from src.domains.orchestration.actions import (
    _orchestrate_capture_fingerprint,
    _orchestrate_default_analysis_base_root,
    _orchestrate_execute_reversible_stage,
    _orchestrate_gc_snapshots_live,
    _orchestrate_plan_stage_talking_head,
    _orchestrate_project_context,
    _orchestrate_resolve_fingerprint,
    _orchestrate_restore_snapshot,
    _orchestrate_run_analysis,
    _orchestrate_run_audio,
    _orchestrate_run_conform,
    _orchestrate_run_deliver,
    _orchestrate_run_edit,
    _orchestrate_run_grade,
    _orchestrate_run_ingest,
    _orchestrate_run_review,
    _orchestrate_take_snapshot,
    logger,
    orchestrate,
)
from src.domains.extension_authoring.actions import (
    _DCTL_MARKER,
    _DCTL_NAME_RE,
    _DCTL_VALID_CATEGORIES,
    _DCTL_VALID_EXT,
    _EXTENSION_KERNEL_ACTIONS,
    _EXTENSION_TYPES,
    _FUSE_MARKER,
    _FUSE_NAME_RE,
    _SCRIPT_LANG_ALIASES,
    _SCRIPT_LANG_EXT,
    _SCRIPT_MARKER,
    _SCRIPT_NAME_RE,
    _SCRIPT_VALID_LANG,
    _dctl_dir,
    _dctl_path,
    _execute_lua_script,
    _execute_python_script,
    _extension_boundary_report,
    _extension_capabilities,
    _extension_safe_name,
    _extension_template_matrix,
    _extension_template_name,
    _extension_type,
    _file_has_marker,
    _fuse_path,
    _fuses_dir,
    _normalize_script_language,
    _probe_dctl_lifecycle,
    _probe_fuse_lifecycle,
    _probe_script_lifecycle,
    _python_env_for_resolve,
    _refresh_or_restart_required,
    _resolve_dctl_subdir,
    _resolve_safe_dir,
    _run_inline_lua,
    _run_inline_python,
    _safe_install_extension,
    _safe_remove_extension,
    _script_install_source,
    _script_path,
    _scripts_dir,
    _source_has_marker,
    _validate_dctl_name,
    _validate_dctl_source,
    _validate_fuse_name,
    _validate_glsl_minimal,
    _validate_lua_syntax,
    _validate_script_language,
    _validate_script_name,
    _validate_script_source,
    dctl,
    fuse_plugin,
    logger,
    script_plugin,
)
from src.domains.project_lifecycle.actions import (
    _CLOUD_SETTINGS_FIELD_SPECS,
    _CLOUD_SYNC_MODES,
    _PROJECT_KERNEL_ACTIONS,
    _PROJECT_MANAGER_METHODS,
    _PROJECT_METHODS,
    _PROJECT_SETTING_PROBE_KEYS,
    _SpecLiveExecutor,
    _database_capabilities,
    _find_project_timeline,
    _is_disposable_project_name,
    _make_spec_hook_runner,
    _normalize_cloud_settings,
    _preset_lifecycle_probe,
    _probe_project_settings,
    _project_boundary_report,
    _project_capabilities,
    _project_folder_summary,
    _project_lint_live,
    _project_manager_snapshot,
    _project_object_summary,
    _project_path_guard,
    _project_path_parent,
    _project_settings_snapshot,
    _require_disposable_project_name,
    _safe_project_archive,
    _safe_project_create,
    _safe_project_delete,
    _safe_project_export,
    _safe_project_import,
    _safe_project_restore,
    _safe_set_current_database,
    _safe_set_project_settings,
    _spec_action,
    _verify_cloud_import_restore,
    layout_presets,
    logger,
    project_manager,
    project_manager_cloud,
    project_manager_database,
    project_manager_folders,
    project_settings,
)
from src.domains.review_annotation.actions import (
    _ANNOTATION_KERNEL_ACTIONS,
    _add_marker,
    _annotation_boundary_report,
    _annotation_capabilities,
    _annotation_snapshot,
    _annotation_target,
    _clear_annotations_by_scope,
    _coerce_marker_number,
    _copy_annotations,
    _copy_timeline_item_markers,
    _current_timeline_marker_frame_id,
    _export_review_report,
    _marker_add_payload,
    _marker_display_frame,
    _marker_frame_from_params,
    _marker_from_existing,
    _marker_rebase_to_timeline_start,
    _marker_timecode_to_frame_id,
    _marker_value,
    _normalize_marker_payload_action,
    _probe_annotations,
    _sync_marker_custom_data,
    _timeline_marker_rows_from_snapshot,
    logger,
    media_pool_item_markers,
    timeline_item_markers,
    timeline_markers,
)

_media_analysis_module.register_caps_preset_provider(_caps_preset_provider)
_media_analysis_module.register_caps_overrides_provider(_caps_overrides_provider)

if __name__ == "__main__":
    _log_preload_audit()
    start_background_update_check(VERSION, project_dir, logger, env=_setup_update_env())
    _install_threaded_tool_dispatch(mcp)

    # Support --full flag to run the 341-tool granular server instead
    if "--full" in sys.argv:
        logger.info("Starting full 341-tool granular server...")
        sys.argv = [arg for arg in sys.argv if arg != "--full"]
        from src.granular import mcp as granular_mcp

        _install_threaded_tool_dispatch(granular_mcp)
        run_fastmcp_stdio(granular_mcp)
        sys.exit(0)

    # --transport stdio (default) | sse | streamable-http. Networked modes bind
    # loopback by default and require a bearer token (see src/core/mcp_transport).
    transport = "stdio"
    if "--transport" in sys.argv:
        _i = sys.argv.index("--transport")
        if _i + 1 < len(sys.argv):
            transport = sys.argv[_i + 1]
            del sys.argv[_i:_i + 2]
    if transport in ("sse", "streamable-http"):
        from src.core.mcp_transport import run_networked
        actor_identity.set_instance("network-sse" if transport == "sse" else "network-http")
        logger.info(f"Starting DaVinci Resolve MCP Server ({transport} transport)")
        run_networked(mcp, transport)
        sys.exit(0)
    if transport != "stdio":
        logger.error(f"Unknown --transport {transport!r}; use stdio|sse|streamable-http")
        sys.exit(2)

    logger.info("Starting DaVinci Resolve MCP Server (36 compound tools)")
    run_fastmcp_stdio(mcp)
