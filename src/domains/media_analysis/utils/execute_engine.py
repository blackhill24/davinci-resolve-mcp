"""Analysis-plan execution engine (async + sync entry points)."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from src.domains.media_analysis.utils import analysis_memory

from src.domains.media_analysis.utils.analysis_index_build import build_analysis_index
from src.domains.media_analysis.utils.capabilities_and_planning import analysis_request_signature, detect_capabilities, vision_is_pending_host_analysis, vision_requested, vision_uses_chat_context, visual_analysis_completed
from src.domains.media_analysis.utils.caps_gating import ANALYSIS_VERSION, AVG_VISION_TOKENS_PER_FRAME, DEFAULT_DEPTH, DEFAULT_TRANSCRIPTION_ENABLED, _annotate_clip_vision_failure, _annotate_manifest_caps_refusal, _annotate_partial_success, _coerce_bool, _resolve_source_trust
from src.domains.media_analysis.utils.clip_identity_registry import _is_relative_to, normalize_path, update_analysis_registry
from src.domains.media_analysis.utils.marker_plan import _analysis_fps, _build_clip_marker_plan
from src.domains.media_analysis.utils.sampling_and_frames import _motion_and_keyframes
from src.domains.media_analysis.utils.technical_probe import _cut_boundary_analysis, _ffprobe, _ingest_report_into_db, _media_duration_seconds, _read_json, _readthrough_analysis, _write_json
from src.domains.media_analysis.utils.transcription import _transcribe
from src.domains.media_analysis.utils.vision_prompt import _vision_analysis


def _synthesize_analysis(
    record: Dict[str, Any],
    technical: Dict[str, Any],
    readthrough: Dict[str, Any],
    motion: Dict[str, Any],
    transcript: Dict[str, Any],
    vision: Dict[str, Any],
    *,
    depth: str = DEFAULT_DEPTH,
    options: Optional[Dict[str, Any]] = None,
    frame_count: int = 0,
    analysis_signature: Optional[Dict[str, Any]] = None,
    marker_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    warnings = []
    if technical.get("summary", {}).get("warnings"):
        warnings.extend(technical["summary"]["warnings"])
    for key in ("loudness", "scenes", "black_frames", "silence", "interlace"):
        item = readthrough.get(key)
        if isinstance(item, dict) and item.get("success") is False:
            warnings.append(f"{key} analysis did not complete")
    summary_parts = []
    if record.get("clip_name"):
        summary_parts.append(str(record["clip_name"]))
    duration = _media_duration_seconds(record, technical)
    if duration is not None:
        summary_parts.append(f"{duration:.1f}s")
    if motion.get("overall_motion_level"):
        summary_parts.append(f"{motion['overall_motion_level']} motion")
    return {
        "success": True,
        "analysis_version": ANALYSIS_VERSION,
        "analysis_signature": analysis_signature or analysis_request_signature(record, depth, options or {}, frame_count),
        "analysis_profile": {
            "depth": depth,
            "analysis_keyframe_budget": int(frame_count or 0),
            "transcription_enabled": _coerce_bool(((options or {}).get("transcription") or {}).get("enabled"), default=DEFAULT_TRANSCRIPTION_ENABLED),
            "vision_enabled": _coerce_bool(((options or {}).get("vision") or {}).get("enabled"), default=False),
            "source_trust": _resolve_source_trust(options),
        },
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_file": record.get("file_path"),
        "clip": record,
        "summary": ", ".join(summary_parts) if summary_parts else "Analyzed media clip",
        "technical_warnings": warnings,
        "technical": technical.get("summary", {}),
        "readthrough": readthrough,
        "cut_analysis": readthrough.get("cut_analysis") if isinstance(readthrough.get("cut_analysis"), dict) else {},
        "motion": motion,
        "transcription": transcript,
        "visual": vision,
        "analysis_keyframes": motion.get("analysis_keyframes", []),
        "clip_analysis_markers": marker_plan or {},
    }


async def _maybe_run_vision_analysis(
    record: Dict[str, Any],
    motion: Dict[str, Any],
    options: Dict[str, Any],
    artifacts: Dict[str, Any],
    capabilities: Dict[str, Any],
    vision_runner: Any = None,
) -> Dict[str, Any]:
    if vision_runner is not None and vision_uses_chat_context(options, capabilities):
        payload = vision_runner(record, motion, options, artifacts, capabilities)
        if inspect.isawaitable(payload):
            payload = await payload
        if isinstance(payload, dict):
            if artifacts.get("visual_json"):
                _write_json(artifacts["visual_json"], payload)
            return payload
    return _vision_analysis(record, motion, options, artifacts, capabilities)


def _clip_is_reused(clip: Any) -> bool:
    """A clip is satisfied by an existing report and runs no fresh analysis."""
    return bool(
        isinstance(clip, dict)
        and clip.get("skip_execution")
        and (clip.get("existing_report") or {}).get("path")
    )


def executing_clips(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Clips in ``plan`` that still require fresh analysis (not pure reuse)."""
    return [
        clip
        for clip in plan.get("clips", [])
        if isinstance(clip, dict) and not _clip_is_reused(clip)
    ]


def plan_requires_capabilities(plan: Dict[str, Any]) -> bool:
    """True when at least one clip needs fresh analysis.

    ``build_plan`` records ``capability_gaps`` from the *requested* options
    before the per-clip reuse decision runs. When every clip is satisfied by an
    existing reusable report, execution only re-keys/imports those reports into
    the current root and performs no fresh transcription/vision/ffprobe — so the
    missing-capability gate must not fire. Callers gate with
    ``plan.get("capability_gaps") and plan_requires_capabilities(plan)``.
    """
    return bool(executing_clips(plan))


async def execute_plan_async(
    plan: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    capabilities: Optional[Dict[str, Any]] = None,
    vision_runner: Any = None,
) -> Dict[str, Any]:
    params = params or {}
    caps = capabilities or detect_capabilities()
    session_only = _coerce_bool(params.get("session_only"), default=False)
    keep_artifacts = _coerce_bool(params.get("keep_artifacts"), default=False)
    if not plan.get("success"):
        return plan
    blocked = [
        clip for clip in plan.get("clips", [])
        if isinstance(clip, dict) and clip.get("reuse_blocked")
    ]
    if blocked:
        return {
            "success": False,
            "status": "reuse_blocked",
            "error": (
                "Analysis provenance exists for one or more Resolve clips, but no reusable "
                "report could be validated. Pass force_refresh=true to intentionally reanalyze."
            ),
            "blocked_clip_count": len(blocked),
            "reuse_summary": plan.get("reuse_summary"),
            "clips": [
                {
                    "record": clip.get("record"),
                    "cache_status": clip.get("cache_status"),
                    "why_not_reused": clip.get("why_not_reused"),
                    "reuse_block_reason": clip.get("reuse_block_reason"),
                    "existing_report": clip.get("existing_report"),
                    "analysis_provenance": clip.get("analysis_provenance"),
                }
                for clip in blocked
            ],
        }
    fresh_clips = executing_clips(plan)
    if plan.get("capability_gaps") and fresh_clips:
        return {
            "success": False,
            "error": "Cannot execute analysis with missing required capabilities",
            "capability_gaps": plan.get("capability_gaps"),
            "install_guidance": plan.get("install_guidance"),
        }
    output_root = plan["output_root"]["project_root"]
    os.makedirs(output_root, exist_ok=True)
    options = {
        "transcription": params.get("transcription") or {},
        "vision": params.get("vision") or {},
        "marker_plan": params.get("marker_plan") or params.get("markerPlan") or {},
        # Thread the batch-runner's job_id (if any) into per-clip options so
        # _record_caps_usage + _check_caps_pre_call can populate the JOB scope.
        "job_id": params.get("job_id"),
        # Same for project_root — caps recording needs it to address the per-
        # project usage DB; falling back to the plan's output_root is fine.
        "project_root": params.get("project_root") or output_root,
        # Phase B — depth threads into the vision payload builder so deep runs
        # carry the per-shot field-group schema.
        "depth": plan.get("depth", DEFAULT_DEPTH),
    }
    keep_frame_artifacts_for_vision = vision_uses_chat_context(options, caps)
    depth = plan.get("depth", DEFAULT_DEPTH)
    manifest = {
        "success": True,
        "analysis_version": ANALYSIS_VERSION,
        "target": plan.get("target"),
        "depth": depth,
        "session_only": session_only,
        "persistent": not session_only,
        "keep_artifacts": keep_artifacts,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project_root": output_root,
        "reuse_summary": plan.get("reuse_summary"),
        "clips": [],
    }
    _write_json(os.path.join(output_root, "capabilities.json"), caps)

    # Phase B — deep depth is opt-in with an explicit cost estimate first.
    # The per-shot field-group pass multiplies vision spend, so the first call
    # returns the estimate; re-call with confirm_deep=true to run. Caps still
    # apply downstream — confirmation does not bypass budgets.
    if (
        depth == "deep"
        and vision_uses_chat_context(options, caps)
        and not _coerce_bool(params.get("confirm_deep") or params.get("confirmDeep"), default=False)
    ):
        estimated_frames = sum(int(c.get("analysis_keyframe_budget") or 0) for c in fresh_clips)
        return {
            "success": True,
            "status": "confirmation_required",
            "reason": "deep_depth_cost_estimate",
            "estimate": {
                "clip_count": len(fresh_clips),
                "estimated_frames": estimated_frames,
                "estimated_vision_tokens": estimated_frames * AVG_VISION_TOKENS_PER_FRAME,
                "tokens_per_frame_assumption": AVG_VISION_TOKENS_PER_FRAME,
            },
            "note": (
                "Deep analysis fills per-shot Visual/Content/Editorial field groups "
                "and costs vision tokens accordingly. Re-call the same analyze action "
                "with confirm_deep=true to proceed, or drop depth to 'standard'."
            ),
        }

    for clip_plan in plan.get("clips", []):
        record = clip_plan["record"]
        artifacts = clip_plan["artifacts"]
        source = record.get("file_path")
        existing_report = clip_plan.get("existing_report") or {}
        clip_result = {
            "record": record,
            "artifacts": artifacts,
            "success": False,
        }
        if clip_plan.get("skip_execution") and existing_report.get("path"):
            # DB-canonical (C1): a reused report — especially one matched from
            # ANOTHER project's root via the registry — must still land rows
            # and a lockstep export in THIS root, keyed to THIS project's clip
            # identity. Without this, media_ref lookups against the current
            # media pool (edit_engine planners, panel readers) find nothing
            # even though the manifest reports success.
            local_analysis_json = existing_report["path"]
            try:
                with open(existing_report["path"], "r", encoding="utf-8") as handle:
                    reused_report = json.load(handle)
            except (OSError, json.JSONDecodeError):
                reused_report = None
            if isinstance(reused_report, dict):
                reused_report = dict(reused_report)
                clip_block = dict(reused_report.get("clip") or {})
                for key in ("clip_id", "clip_name", "media_id", "bin_path"):
                    if record.get(key):
                        clip_block[key] = record[key]
                if record.get("file_path"):
                    clip_block["file_path"] = record["file_path"]
                reused_report["clip"] = clip_block
                db_ingest = _ingest_report_into_db(
                    output_root,
                    reused_report,
                    os.path.dirname(artifacts["analysis_json"]),
                )
                if not db_ingest.get("success"):
                    clip_result["db_ingest_error"] = db_ingest.get("error")
                if os.path.normpath(artifacts["analysis_json"]) != os.path.normpath(existing_report["path"]):
                    _write_json(artifacts["analysis_json"], reused_report)
                local_analysis_json = artifacts["analysis_json"]
            clip_result.update({
                "success": True,
                "reused": True,
                "analysis_json": local_analysis_json,
                "reuse_reason": clip_plan.get("reuse_reason"),
                "cache_status": clip_plan.get("cache_status"),
                "cache_warnings": existing_report.get("cache_warnings", []),
                "reuse_source": clip_plan.get("reuse_source"),
                "reused_from": clip_plan.get("reused_from") or existing_report["path"],
            })
            manifest["clips"].append(clip_result)
            continue
        if not source or not os.path.isfile(source):
            clip_result["error"] = f"Source media not found: {source}"
            manifest["clips"].append(clip_result)
            continue

        technical = _ffprobe(source)
        if not technical.get("success"):
            clip_result["error"] = technical.get("error")
            manifest["clips"].append(clip_result)
            continue
        _write_json(artifacts["technical_json"], technical)

        readthrough: Dict[str, Any] = {"success": True, "status": "skipped", "reason": "quick analysis depth"}
        motion: Dict[str, Any] = {"success": True, "status": "skipped", "analysis_keyframes": []}
        if depth in {"standard", "deep", "custom"}:
            readthrough = _readthrough_analysis(source)
            duration = _media_duration_seconds(record, technical)
            fps = _analysis_fps(record, technical)
            readthrough["cut_analysis"] = _cut_boundary_analysis(
                duration,
                (readthrough.get("scenes") or {}).get("items", []),
                fps,
            )
            motion = _motion_and_keyframes(
                source,
                duration,
                (readthrough.get("scenes") or {}).get("items", []),
                artifacts,
                int(clip_plan.get("analysis_keyframe_budget") or 0),
                fps=fps,
                cut_analysis=readthrough.get("cut_analysis"),
                write_frames=keep_frame_artifacts_for_vision or not _coerce_bool(params.get("cleanup_frames"), default=False),
                sampling=clip_plan.get("sampling"),
            )
            if artifacts.get("motion_json"):
                _write_json(artifacts["motion_json"], motion)

        transcript = _transcribe(source, artifacts, options, caps)
        vision = await _maybe_run_vision_analysis(record, motion, options, artifacts, caps, vision_runner)
        vision_pending = vision_is_pending_host_analysis(vision)
        vision_failed = (
            vision_requested(options)
            and not vision_pending
            and not visual_analysis_completed(vision)
        )
        frame_count = int(clip_plan.get("analysis_keyframe_budget") or 0)
        marker_plan = _build_clip_marker_plan(
            record,
            technical,
            readthrough,
            motion,
            transcript,
            vision,
            options=options,
            analysis_signature=clip_plan.get("analysis_signature"),
        )
        if vision_pending:
            marker_plan["vision_status"] = "pending_host_analysis"
        if artifacts.get("marker_plan_json"):
            marker_plan["path"] = artifacts["marker_plan_json"]
            _write_json(artifacts["marker_plan_json"], marker_plan)
        analysis = _synthesize_analysis(
            record,
            technical,
            readthrough,
            motion,
            transcript,
            vision,
            depth=depth,
            options=options,
            frame_count=frame_count,
            analysis_signature=clip_plan.get("analysis_signature"),
            marker_plan=marker_plan,
        )
        if vision_pending:
            analysis["vision_status"] = "pending_host_analysis"
            analysis["vision_token"] = vision.get("vision_token")
        # C1 — DB rows first (canonical), then the derived JSON export. The DB
        # lives under output_root (same root as clips/), not the caps root.
        db_ingest = _ingest_report_into_db(
            output_root,
            analysis,
            os.path.dirname(artifacts["analysis_json"]),
        )
        if not db_ingest.get("success"):
            clip_result["db_ingest_error"] = db_ingest.get("error")
        _write_json(artifacts["analysis_json"], analysis)
        cleanup_frames_requested = _coerce_bool(params.get("cleanup_frames"), default=False)
        if cleanup_frames_requested and not vision_pending and artifacts.get("frames_dir"):
            shutil.rmtree(artifacts["frames_dir"], ignore_errors=True)
        clip_result.update({
            "success": True,
            "analysis_json": artifacts["analysis_json"],
            "marker_plan_json": artifacts.get("marker_plan_json"),
            "marker_count": marker_plan.get("marker_count"),
        })
        if vision_pending:
            clip_result.update({
                "vision_status": "pending_host_analysis",
                "vision_token": vision.get("vision_token"),
                "visual": vision,
            })
            manifest["vision_pending"] = True
        elif vision_failed:
            _annotate_clip_vision_failure(clip_result, vision)
        manifest["clips"].append(clip_result)

    manifest["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest["clip_count"] = len(manifest["clips"])
    manifest["successful_clip_count"] = sum(1 for row in manifest["clips"] if row.get("success"))
    manifest["failed_clip_count"] = manifest["clip_count"] - manifest["successful_clip_count"]
    manifest["vision_pending_clip_count"] = sum(
        1 for row in manifest["clips"] if row.get("vision_status") == "pending_host_analysis"
    )
    manifest["vision_pending"] = bool(manifest["vision_pending_clip_count"])
    manifest["success"] = manifest["failed_clip_count"] == 0
    # D3 — partial-success preservation. When some clips succeeded and others
    # failed, surface explicit completed/failed clip-id lists so callers can
    # retry only the failed subset instead of redoing completed work.
    _annotate_partial_success(manifest)
    _annotate_manifest_caps_refusal(manifest)
    if manifest["vision_pending"]:
        manifest["pending_action"] = {
            "tool": "media_analysis",
            "action": "commit_vision",
            "note": (
                "Host chat must read each clip's frame_paths, produce visual analysis "
                "JSON, and call commit_vision with the result. Until then, vision-derived "
                "metadata (Description, Keywords, slate fields) and vision-derived clip "
                "markers (best_moments, visual qc_flags) are deferred."
            ),
        }

    if (
        not session_only
        and manifest["successful_clip_count"]
        and _coerce_bool(params.get("auto_build_index"), default=True)
    ):
        manifest["index"] = build_analysis_index(output_root)

    if not session_only and manifest["successful_clip_count"]:
        report_paths = [
            row.get("analysis_json")
            for row in manifest["clips"]
            if row.get("success") and row.get("analysis_json") and os.path.isfile(str(row.get("analysis_json")))
        ]
        if report_paths:
            manifest["analysis_registry"] = update_analysis_registry(output_root, report_paths=report_paths)

    # V2 memory + heartbeat layer (per V2 shot schema spec §9).
    # Heartbeat tracks current project state for session-start awareness.
    # Bin summary is the machine's "first impression" briefing of the bin.
    if not session_only and manifest["successful_clip_count"]:
        try:
            analysis_memory.ensure_memory_structure(output_root)
            analysis_memory.ensure_soul_structure(os.path.dirname(output_root))
            pending_clips = [
                {"clip_id": (row.get("record") or {}).get("clip_id"), "reason": "vision_pending"}
                for row in manifest["clips"]
                if row.get("vision_status") == "pending_host_analysis"
            ]
            failed_clips = [
                {"clip_id": (row.get("record") or {}).get("clip_id"), "error": row.get("error")}
                for row in manifest["clips"]
                if not row.get("success") and row.get("vision_status") != "pending_host_analysis"
            ]
            analysis_memory.update_heartbeat(
                output_root,
                last_run={
                    "completed_at": manifest.get("completed_at"),
                    "depth": manifest.get("depth"),
                    "analysis_version": manifest.get("analysis_version"),
                    "schema_version": "2.0",
                },
                clip_counts={
                    "total": manifest["clip_count"],
                    "analyzed": manifest["successful_clip_count"],
                    "failed": manifest["failed_clip_count"],
                    "vision_pending": manifest["vision_pending_clip_count"],
                },
                pending=pending_clips,
                recent_failures=failed_clips,
            )
            # Regenerate bin summary only when vision has actually committed
            # (otherwise per-clip summaries don't exist yet).
            if not manifest.get("vision_pending"):
                analysis_memory.regenerate_bin_summary_from_manifest(
                    output_root, manifest, project_name=manifest.get("project_name"),
                )
        except Exception as exc:  # defensive: memory layer must never break analysis
            manifest.setdefault("memory_layer_warnings", []).append(
                f"{type(exc).__name__}: {exc}"
            )

    _write_json(os.path.join(output_root, "manifest.json"), manifest)

    if session_only:
        reports = []
        for row in manifest["clips"]:
            report_path = row.get("analysis_json")
            if report_path and os.path.isfile(report_path):
                try:
                    reports.append(_read_json(report_path))
                except (OSError, json.JSONDecodeError):
                    continue
        manifest["reports"] = reports
        from src.domains.media_analysis.utils.reports import summarize_reports
        manifest["project_summary"] = summarize_reports(output_root)
        manifest["artifacts_cleaned_up"] = False
        if not keep_artifacts:
            cleanup_root = output_root
            session_temp_base = params.get("_session_temp_base_root")
            if session_temp_base:
                candidate = normalize_path(session_temp_base)
                if (
                    os.path.basename(candidate).startswith("davinci-resolve-mcp-analysis-session-")
                    and _is_relative_to(output_root, candidate)
                ):
                    cleanup_root = candidate
            shutil.rmtree(cleanup_root, ignore_errors=True)
            manifest["artifacts_cleaned_up"] = True
            manifest["artifact_cleanup_root"] = cleanup_root

    return manifest


def execute_plan(plan: Dict[str, Any], params: Optional[Dict[str, Any]] = None, capabilities: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(execute_plan_async(plan, params=params, capabilities=capabilities))

    # A loop is already running in this thread (e.g. invoked from an async MCP handler).
    # Run the coroutine in a worker thread with its own loop so we can block-wait here.
    result: Dict[str, Any] = {}
    error: Dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(
                execute_plan_async(plan, params=params, capabilities=capabilities)
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            error["exc"] = exc

    worker = threading.Thread(target=_runner, name="execute_plan_worker", daemon=True)
    worker.start()
    worker.join()
    if "exc" in error:
        raise error["exc"]
    return result["value"]


def _walk_set(container: Any, path_parts: List[str], value: Any) -> bool:
    """Set value at a dotted path inside a nested dict, creating intermediate dicts as needed.

    Returns True if the leaf node was modified, False if container is not navigable.
    """
    if not path_parts:
        return False
    cursor = container
    for part in path_parts[:-1]:
        if not isinstance(cursor, dict):
            return False
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    if not isinstance(cursor, dict):
        return False
    cursor[path_parts[-1]] = value
    return True


def _walk_get(container: Any, path_parts: List[str]) -> Tuple[bool, Any]:
    """Return (found, value) for a dotted path inside a nested dict."""
    cursor = container
    for part in path_parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return False, None
        cursor = cursor[part]
    return True, cursor


def _find_shot_entry(shot_descriptions: List[Dict[str, Any]], entity_uuid: str) -> Optional[Dict[str, Any]]:
    """Locate a shot in shot_descriptions by shot_uuid or shot_index match."""
    if not isinstance(shot_descriptions, list):
        return None
    target = str(entity_uuid)
    # First pass: match shot_uuid
    for entry in shot_descriptions:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("shot_uuid") or "") == target:
            return entry
    # Second pass: match shot_index (V1/sidecar identifier)
    try:
        target_int = int(target)
    except (TypeError, ValueError):
        target_int = None
    if target_int is not None:
        for entry in shot_descriptions:
            if not isinstance(entry, dict):
                continue
            entry_idx = entry.get("shot_index")
            if entry_idx is None:
                continue
            try:
                if int(entry_idx) == target_int:
                    return entry
            except (TypeError, ValueError):
                continue
    return None


def preserve_human_corrections(
    clip_dir_path: str,
    normalized_visual: Dict[str, Any],
    *,
    clip_id: Optional[str] = None,
) -> Dict[str, Any]:
    """V2 contract: read corrections.json sidecar and re-apply human-edited fields.

    Called from commit_visual_analysis between normalization and persistence so
    that re-analyzing a clip never silently overwrites editor corrections.

    Returns a metrics dict:
      {preserved_count, applied: [{entity_type, entity_uuid, field_path}],
       skipped: [{key, reason}], changelog_added}
    """
    corrections_path = os.path.join(clip_dir_path, "corrections.json")
    metrics: Dict[str, Any] = {
        "preserved_count": 0,
        "applied": [],
        "skipped": [],
        "changelog_added": 0,
        "corrections_path": corrections_path,
    }
    if not os.path.isfile(corrections_path):
        return metrics

    try:
        with open(corrections_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        metrics["error"] = f"Failed to read corrections.json: {exc}"
        return metrics

    if not isinstance(data, dict):
        metrics["error"] = "corrections.json is not a JSON object"
        return metrics

    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    changelog = data.get("changelog") if isinstance(data.get("changelog"), list) else []
    data.setdefault("schema_version", "2.0")
    data["current"] = current
    data["changelog"] = changelog
    if clip_id and not data.get("clip_id"):
        data["clip_id"] = str(clip_id)

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    shot_descriptions = normalized_visual.get("shot_descriptions") if isinstance(normalized_visual.get("shot_descriptions"), list) else []
    new_changelog_entries: List[Dict[str, Any]] = []

    for key, entry in list(current.items()):
        if not isinstance(entry, dict):
            metrics["skipped"].append({"key": key, "reason": "entry not a dict"})
            continue
        if entry.get("source") != "human":
            continue
        # Key format: "{entity_type}:{entity_uuid}:{field_path}"
        parts = key.split(":", 2)
        if len(parts) != 3:
            metrics["skipped"].append({"key": key, "reason": "malformed key"})
            continue
        entity_type, entity_uuid, field_path = parts
        path_parts = [p for p in field_path.split(".") if p]
        if not path_parts:
            metrics["skipped"].append({"key": key, "reason": "empty field_path"})
            continue
        human_value = entry.get("value")

        if entity_type == "clip":
            target_container = normalized_visual
        elif entity_type == "shot":
            target_container = _find_shot_entry(shot_descriptions, entity_uuid)
            if target_container is None:
                metrics["skipped"].append({"key": key, "reason": "shot not found in vision output"})
                continue
        else:
            metrics["skipped"].append({"key": key, "reason": f"unknown entity_type '{entity_type}'"})
            continue

        found, machine_value = _walk_get(target_container, path_parts)
        if not _walk_set(target_container, path_parts, human_value):
            metrics["skipped"].append({"key": key, "reason": "could not write into target container"})
            continue
        metrics["preserved_count"] += 1
        metrics["applied"].append({
            "entity_type": entity_type,
            "entity_uuid": entity_uuid,
            "field_path": field_path,
        })

        if found and machine_value != human_value:
            new_changelog_entries.append({
                "entity_type": entity_type,
                "entity_uuid": entity_uuid,
                "field_path": field_path,
                "previous_value": machine_value,
                "new_value": human_value,
                "previous_source": "vision",
                "new_source": "human",
                "previous_author": "system",
                "new_author": entry.get("author") or "unknown",
                "change_reason": "preserved across re-analysis",
                "timestamp": now,
            })

    if new_changelog_entries:
        changelog.extend(new_changelog_entries)
        metrics["changelog_added"] = len(new_changelog_entries)
        try:
            os.makedirs(os.path.dirname(corrections_path), exist_ok=True)
            tmp_path = corrections_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True, default=str)
            os.replace(tmp_path, corrections_path)
        except OSError as exc:
            metrics["error"] = f"Failed to write corrections.json: {exc}"

    return metrics


