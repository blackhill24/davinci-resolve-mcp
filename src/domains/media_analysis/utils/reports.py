"""Committing visual analysis, loading/summarizing reports, coverage reporting."""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

from src.domains.media_analysis.utils.analysis_index_build import build_analysis_index
from src.domains.media_analysis.utils.capabilities_and_planning import build_plan
from src.domains.media_analysis.utils.caps_gating import ANALYSIS_VERSION, DEFAULT_TRANSCRIPTION_ENABLED, HOST_CHAT_PATHS_PROVIDER, SOURCE_TRUST_VALUES, _apply_caps_to_response, _record_caps_usage, _timestamp_from_analyzed_at
from src.domains.media_analysis.utils.clip_identity_registry import _is_relative_to, _read_analysis_registry, analysis_registry_path, normalize_path, update_analysis_registry
from src.domains.media_analysis.utils.execute_engine import preserve_human_corrections
from src.domains.media_analysis.utils.marker_plan import _build_clip_marker_plan
from src.domains.media_analysis.utils.subtitles_and_reuse import _record_has_analysis_provenance
from src.domains.media_analysis.utils.technical_probe import _ingest_report_into_db, _parse_float, _read_json, _write_json


def _normalize_host_chat_visual(payload: Any, *, fallback_record: Optional[Dict[str, Any]] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Coerce a host-chat visual payload into the canonical visual shape.

    Returns (normalized_visual, error). If the payload is missing required structure
    that we cannot safely default, returns (None, reason).
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, f"visual payload was a string but not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "visual payload must be a JSON object matching the vision schema"

    normalized: Dict[str, Any] = dict(payload)
    normalized["success"] = True
    normalized["provider"] = HOST_CHAT_PATHS_PROVIDER
    normalized.pop("status", None)

    clip_summary = normalized.get("clip_summary")
    if not isinstance(clip_summary, str) or not clip_summary.strip():
        if fallback_record:
            normalized["clip_summary"] = f"Host-chat visual analysis for {fallback_record.get('clip_name') or fallback_record.get('file_path') or 'clip'}."
        else:
            normalized["clip_summary"] = "Host-chat visual analysis (no summary provided)."

    def _ensure_dict(key: str, default: Dict[str, Any]) -> None:
        value = normalized.get(key)
        if not isinstance(value, dict):
            normalized[key] = dict(default)

    def _ensure_list(container: Dict[str, Any], key: str) -> None:
        if not isinstance(container.get(key), list):
            container[key] = []

    _ensure_dict("editorial_classification", {"primary_use": "unknown", "select_potential": "medium", "reason": ""})
    _ensure_dict("content", {"locations": [], "people_visible": "unknown", "actions": [], "objects": [], "visible_text": [], "notable_audio_context": []})
    for list_key in ("locations", "actions", "objects", "visible_text", "notable_audio_context"):
        _ensure_list(normalized["content"], list_key)
    _ensure_dict("shot_and_style", {"shot_sizes": [], "camera_motion": [], "composition_notes": "", "lighting_mood": "", "color_mood": ""})
    for list_key in ("shot_sizes", "camera_motion"):
        _ensure_list(normalized["shot_and_style"], list_key)
    _ensure_dict("slate", {"slate_visible": False, "scene": "", "shot": "", "take": "", "camera": "", "roll": "", "date": "", "production": "", "visible_text": [], "confidence": {}})
    _ensure_list(normalized["slate"], "visible_text")
    _ensure_dict("motion", {"overall_level": "unknown", "motion_events": [], "quiet_regions": []})
    _ensure_list(normalized["motion"], "motion_events")
    _ensure_list(normalized["motion"], "quiet_regions")
    _ensure_dict("cut_understanding", {"cut_count": 0, "likely_edited_sequence": False, "flash_frame_candidates": [], "notes": []})
    _ensure_list(normalized["cut_understanding"], "flash_frame_candidates")
    _ensure_list(normalized["cut_understanding"], "notes")
    if not isinstance(normalized.get("analysis_keyframes"), list):
        normalized["analysis_keyframes"] = []
    raw_shot_descriptions = normalized.get("shot_descriptions")
    coerced_shot_descriptions: List[Dict[str, Any]] = []
    if isinstance(raw_shot_descriptions, list):
        for row in raw_shot_descriptions:
            if not isinstance(row, dict):
                continue
            entry: Dict[str, Any] = dict(row)
            try:
                entry["shot_index"] = int(entry.get("shot_index"))
            except (TypeError, ValueError):
                entry.pop("shot_index", None)
            for time_key in ("time_seconds_start", "time_seconds_end"):
                parsed = _parse_float(entry.get(time_key))
                if parsed is not None:
                    entry[time_key] = parsed
                else:
                    entry.pop(time_key, None)
            description = entry.get("description") or entry.get("visual_description")
            entry["description"] = str(description).strip() if description else ""
            if not isinstance(entry.get("qc_flags"), list):
                entry["qc_flags"] = []
            if not isinstance(entry.get("frame_indices_used"), list):
                entry.pop("frame_indices_used", None)
            coerced_shot_descriptions.append(entry)
    normalized["shot_descriptions"] = coerced_shot_descriptions
    _ensure_dict("editing_notes", {"best_moments": [], "continuity_flags": [], "qc_flags": [], "search_tags": []})
    for list_key in ("best_moments", "continuity_flags", "qc_flags", "search_tags"):
        _ensure_list(normalized["editing_notes"], list_key)
    _ensure_dict("confidence", {"visual": "low", "motion": "computed", "transcript": "unavailable"})

    return normalized, None


def _find_clip_dir_for_commit(
    project_root: str,
    *,
    clip_id: Optional[str] = None,
    file_path: Optional[str] = None,
    clip_dir: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    root = normalize_path(project_root)
    clips_root = os.path.join(root, "clips")
    if clip_dir:
        candidate = normalize_path(clip_dir if os.path.isabs(clip_dir) else os.path.join(clips_root, clip_dir))
        if not _is_relative_to(candidate, root):
            return None, "clip_dir must be under the project analysis root"
        if os.path.isdir(candidate):
            return candidate, None
        return None, f"clip_dir not found: {candidate}"
    if not os.path.isdir(clips_root):
        return None, f"No clips directory under analysis root: {clips_root}"
    target_clip_id = str(clip_id) if clip_id else None
    target_file = normalize_path(file_path) if file_path else None
    for entry in sorted(os.listdir(clips_root)):
        candidate = os.path.join(clips_root, entry)
        analysis_path = os.path.join(candidate, "analysis.json")
        if not os.path.isfile(analysis_path):
            continue
        try:
            with open(analysis_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        clip_block = report.get("clip") or {}
        if target_clip_id and str(clip_block.get("clip_id") or "") == target_clip_id:
            return candidate, None
        if target_file and normalize_path(clip_block.get("file_path") or "") == target_file:
            return candidate, None
    if target_clip_id:
        return None, f"No persisted analysis found for clip_id={target_clip_id} under {clips_root}"
    if target_file:
        return None, f"No persisted analysis found for file_path={target_file} under {clips_root}"
    return None, "commit_vision requires clip_id, file_path, or clip_dir"


def commit_visual_analysis(
    *,
    project_root: str,
    visual: Any,
    clip_id: Optional[str] = None,
    file_path: Optional[str] = None,
    clip_dir: Optional[str] = None,
    vision_token: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge host-chat visual analysis into an already-persisted clip report.

    Reads analysis.json under <project_root>/clips/<clip_dir>/, validates the
    optional vision_token against the stored deferred payload, normalizes the
    visual JSON, rewrites visual.json + analysis.json + clip_analysis_markers.json,
    refreshes the SQLite index entry, and returns the new report path.
    """
    root = normalize_path(project_root)
    if not os.path.isdir(root):
        return {"success": False, "error": f"Project analysis root not found: {root}"}

    clip_dir_path, lookup_err = _find_clip_dir_for_commit(
        root, clip_id=clip_id, file_path=file_path, clip_dir=clip_dir,
    )
    if lookup_err:
        return {"success": False, "error": lookup_err}

    analysis_json_path = os.path.join(clip_dir_path, "analysis.json")
    try:
        with open(analysis_json_path, "r", encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return {"success": False, "error": f"Failed to read analysis.json: {exc}"}

    existing_vision = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    stored_token = existing_vision.get("vision_token") or report.get("vision_token")
    if vision_token and stored_token and str(vision_token) != str(stored_token):
        return {
            "success": False,
            "error": "vision_token mismatch; the analysis report has been re-analyzed since the deferred payload was issued.",
            "expected_vision_token": stored_token,
            "received_vision_token": vision_token,
        }

    record = report.get("clip") or {}
    normalized_visual, normalize_err = _normalize_host_chat_visual(visual, fallback_record=record)
    if normalize_err:
        return {"success": False, "error": normalize_err}

    # V2 trust-but-fix-optionally contract: re-apply human corrections so
    # re-analysis never silently overwrites editor edits.
    corrections_metrics = preserve_human_corrections(
        clip_dir_path,
        normalized_visual,
        clip_id=record.get("clip_id"),
    )

    technical = {"summary": report.get("technical") or {}}
    if isinstance(report.get("readthrough"), dict):
        readthrough = report["readthrough"]
    else:
        readthrough = {}
    motion = report.get("motion") if isinstance(report.get("motion"), dict) else {}
    transcript = report.get("transcription") if isinstance(report.get("transcription"), dict) else {}
    analysis_signature = report.get("analysis_signature") or {}
    profile = report.get("analysis_profile") or {}

    merged_options: Dict[str, Any] = {
        "vision": {"enabled": True, "provider": HOST_CHAT_PATHS_PROVIDER},
        "transcription": {"enabled": bool(profile.get("transcription_enabled", DEFAULT_TRANSCRIPTION_ENABLED))},
        "marker_plan": (options or {}).get("marker_plan") or {},
    }

    marker_plan = _build_clip_marker_plan(
        record,
        technical,
        readthrough,
        motion,
        transcript,
        normalized_visual,
        options=merged_options,
        analysis_signature=analysis_signature,
    )
    marker_plan_path = os.path.join(clip_dir_path, "clip_analysis_markers.json")
    marker_plan["path"] = marker_plan_path
    _write_json(marker_plan_path, marker_plan)

    visual_json_path = os.path.join(clip_dir_path, "visual.json")
    _write_json(visual_json_path, normalized_visual)

    report["visual"] = normalized_visual
    report["clip_analysis_markers"] = marker_plan
    report["analysis_profile"] = {
        **(profile if isinstance(profile, dict) else {}),
        "vision_enabled": True,
    }
    report.pop("vision_status", None)
    report.pop("vision_token", None)
    report["vision_committed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # C1 — DB rows first (canonical), then the derived JSON export.
    db_ingest = _ingest_report_into_db(root, report, clip_dir_path)
    _write_json(analysis_json_path, report)

    index_status_info: Dict[str, Any] = {}
    try:
        index_status_info = build_analysis_index(root)
    except Exception as exc:  # noqa: BLE001 — index refresh is best-effort
        index_status_info = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        registry_status = update_analysis_registry(root, report_paths=[analysis_json_path])
    except Exception as exc:  # noqa: BLE001 — registry refresh is best-effort
        registry_status = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    # Record caps usage if the host reported token counts in the visual payload.
    # Host clients that don't report tokens fall through with zeros (recorded as
    # frames_uploaded so the per-clip + per-day rollups still show activity).
    try:
        visual_dict = visual if isinstance(visual, dict) else {}
        usage_block = visual_dict.get("usage") if isinstance(visual_dict.get("usage"), dict) else {}
        vision_tokens = int(usage_block.get("vision_tokens") or usage_block.get("total_tokens") or 0)
        frames_uploaded = int(usage_block.get("frames_uploaded") or len(record.get("frame_paths") or []) or 0)
        _record_caps_usage(
            project_root=root,
            clip_id=record.get("clip_id") or clip_id,
            vision_tokens=vision_tokens,
            frames_uploaded=frames_uploaded,
        )
    except Exception:
        pass

    return _apply_caps_to_response({
        "success": True,
        "analysis_json": analysis_json_path,
        "visual_json": visual_json_path,
        "marker_plan_json": marker_plan_path,
        "marker_count": marker_plan.get("marker_count"),
        "clip_dir": clip_dir_path,
        "record": record,
        "index": index_status_info,
        "analysis_registry": registry_status,
        "corrections": corrections_metrics,
        "db_ingest": db_ingest,
    })


def _safe_report_path(project_root: str, report_path: str) -> Tuple[Optional[str], Optional[str]]:
    root = normalize_path(project_root)
    candidate = normalize_path(report_path)
    if not _is_relative_to(candidate, root):
        return None, "report_path must be under the project analysis root"
    if not os.path.isfile(candidate):
        return None, f"Report not found: {candidate}"
    return candidate, None


def load_report(project_root: str, report_path: Optional[str] = None, clip_dir: Optional[str] = None) -> Dict[str, Any]:
    if report_path:
        path, err = _safe_report_path(project_root, report_path)
        if err:
            return {"success": False, "error": err}
    elif clip_dir:
        path, err = _safe_report_path(project_root, os.path.join(project_root, "clips", clip_dir, "analysis.json"))
        if err:
            return {"success": False, "error": err}
    else:
        path, err = _safe_report_path(project_root, os.path.join(project_root, "manifest.json"))
        if err:
            return {"success": False, "error": err}
    payload = _read_json(path)
    return {"success": True, "path": path, "report": payload}


def _collect_reports_for_summary(root: str) -> Tuple[List[Dict[str, Any]], List[str], str]:
    """(reports, report_paths, source) for summarize_reports.

    DB-first: when every report dir on disk is covered by an ingested clip
    row, reports come from the DB-canonical store (blob + human overlay —
    identical content to the lockstep JSON export). Pre-v9 roots and MIXED
    roots (some clips not ingested) fall back WHOLESALE to the JSON walk —
    a partial DB view would silently under-report.
    """
    clips_root = os.path.join(root, "clips")
    disk_paths: List[str] = []
    if os.path.isdir(clips_root):
        for dirpath, _, filenames in os.walk(clips_root):
            if "analysis.json" in filenames:
                disk_paths.append(os.path.join(dirpath, "analysis.json"))
    disk_paths.sort()

    try:
        from src.core import timeline_brain_db
        from src.domains.media_analysis.utils import analysis_store

        conn = timeline_brain_db.connect(root)
        db_dirs = {
            str(r["clip_dir"]): str(r["clip_uuid"])
            for r in conn.execute(
                "SELECT clip_dir, clip_uuid FROM clips WHERE clip_dir IS NOT NULL"
            ).fetchall()
        }
    except Exception:  # noqa: BLE001 — no DB (pre-v9) → JSON
        db_dirs = {}
    if disk_paths and db_dirs:
        dir_names = [os.path.basename(os.path.dirname(p)) for p in disk_paths]
        if all(name in db_dirs for name in dir_names):
            from src.domains.media_analysis.utils import analysis_store

            reports: List[Dict[str, Any]] = []
            complete = True
            for path, name in zip(disk_paths, dir_names):
                try:
                    report = analysis_store.export_report(root, db_dirs[name])
                except Exception:  # noqa: BLE001
                    report = None
                if not isinstance(report, dict):
                    complete = False
                    break
                reports.append(report)
            if complete:
                return reports, disk_paths, "db"

    reports = []
    report_paths: List[str] = []
    for path in disk_paths:
        try:
            reports.append(_read_json(path))
            report_paths.append(path)
        except (OSError, json.JSONDecodeError):
            continue
    return reports, report_paths, "json"


def summarize_reports(project_root: str) -> Dict[str, Any]:
    root = normalize_path(project_root)
    reports, report_paths, reports_source = _collect_reports_for_summary(root)
    warnings = []
    motion_counts: Dict[str, int] = {}
    tags: Dict[str, int] = {}
    signed_report_count = 0
    newest_ts = 0.0
    # F1 — provenance source list, parallel to `reports`.
    source_reports: List[Dict[str, Any]] = []
    missing_reports: List[Dict[str, Any]] = []
    for report, report_path in zip(reports, report_paths):
        if report.get("analysis_signature"):
            signed_report_count += 1
        else:
            # Unsigned reports surface in `missing_reports` so the caller can
            # tell which contributing clips would need re-analysis to verify.
            missing_reports.append({
                "report_path": report_path,
                "reason": "unsigned_report",
            })
        analyzed_ts = _timestamp_from_analyzed_at(report.get("analyzed_at")) or 0
        newest_ts = max(newest_ts, analyzed_ts)
        warnings.extend(report.get("technical_warnings") or [])
        level = ((report.get("motion") or {}).get("overall_motion_level") or "unknown")
        motion_counts[level] = motion_counts.get(level, 0) + 1
        visual = report.get("visual") or {}
        editing_notes = visual.get("editing_notes") or {}
        for tag in editing_notes.get("search_tags") or []:
            tags[tag] = tags.get(tag, 0) + 1
        # F1 source-report citation entry.
        record = report.get("record") or {}
        source_reports.append({
            "clip_id": record.get("clip_id") or report.get("clip_id"),
            "clip_name": record.get("clip_name") or report.get("clip_name"),
            "analysis_signature": report.get("analysis_signature"),
            "analysis_report_path": report_path,
            "analyzed_at": report.get("analyzed_at"),
        })
    summary = {
        "success": True,
        "project_root": root,
        "source": reports_source,  # "db" (canonical store) | "json" (walk fallback)
        "clip_reports": len(reports),
        "motion_distribution": motion_counts,
        "technical_warning_count": len(warnings),
        "technical_warnings": warnings[:50],
        "search_tags": sorted(tags, key=tags.get, reverse=True)[:50],
        "cache": {
            "signed_report_count": signed_report_count,
            "unsigned_report_count": max(0, len(reports) - signed_report_count),
            "newest_analysis_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(newest_ts))
                if newest_ts else None
            ),
        },
        # F1 — provenance citation map. Lets callers (and the model) trace
        # each summary claim back to the underlying analysis reports, so
        # cross-clip statements aren't load-bearing without verification.
        "provenance": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scope": {"type": "project", "project_root": root},
            "source_reports": source_reports,
            "missing_reports": missing_reports,
        },
    }
    _write_json(os.path.join(root, "project_summary.json"), summary)
    return summary


_SOURCE_TRUST_RANK = {
    "unknown": -1,
    "auto": 0,
    "filename": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
}


def _source_trust_rank(value: Any) -> int:
    return _SOURCE_TRUST_RANK.get(str(value or "auto").strip().lower(), 0)


def _normalize_min_source_trust(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    candidate = str(value).strip().lower()
    if candidate not in SOURCE_TRUST_VALUES:
        return None
    return candidate


def _layers_present_in_report(report: Optional[Dict[str, Any]]) -> List[str]:
    """Return the analysis layers that have meaningful content in this report.

    Layer names match the planner's vocabulary (technical, readthrough,
    cut_analysis, motion, transcription, vision, marker_plan).
    """
    if not isinstance(report, dict):
        return []
    present: List[str] = []
    technical = report.get("technical")
    if isinstance(technical, dict) and technical:
        present.append("technical")
    readthrough = report.get("readthrough")
    if isinstance(readthrough, dict):
        if any(
            isinstance(readthrough.get(k), dict) and readthrough.get(k, {}).get("success") is not False
            for k in ("loudness", "scenes", "black_frames", "silence", "interlace")
        ):
            present.append("readthrough")
        if isinstance(readthrough.get("cut_analysis"), dict):
            present.append("cut_analysis")
    motion = report.get("motion")
    if isinstance(motion, dict) and (motion.get("analysis_keyframes") or motion.get("overall_motion_level")):
        present.append("motion")
    transcript = report.get("transcription")
    if isinstance(transcript, dict) and (transcript.get("text") or transcript.get("segments")):
        present.append("transcription")
    visual = report.get("visual")
    if isinstance(visual, dict):
        status = visual.get("status")
        is_pending = status == "pending_host_analysis" or visual.get("vision_token") is not None
        has_content = bool(
            visual.get("clip_summary")
            or visual.get("shot_descriptions")
            or visual.get("editorial_classification")
        )
        if has_content and not is_pending:
            present.append("vision")
    markers = report.get("clip_analysis_markers")
    if isinstance(markers, dict) and markers:
        present.append("marker_plan")
    return present


def _recommend_coverage_action(
    *,
    cache_status: str,
    reuse_blocked: bool,
    below_min_source_trust: bool,
    superseded_by_relink: bool,
    missing_layers: List[str],
    staleness_reasons: List[str],
    record: Dict[str, Any],
) -> str:
    if superseded_by_relink:
        return (
            "The Media Pool clip was replaced or relinked after analysis. The prior "
            "report is preserved for reference but should not be reused. Re-analyze "
            "with the current source media."
        )
    if reuse_blocked:
        return (
            "Resolve clip metadata claims prior analysis but no compatible report "
            "could be validated. Restore the referenced report or pass force_refresh=true."
        )
    if below_min_source_trust:
        return (
            "Existing analysis is below the requested min_source_trust. Re-run with "
            "source_trust raised (analyze_clip with source_trust=...) once the higher "
            "trust is justified."
        )
    if cache_status == "miss":
        clip_id = record.get("clip_id")
        target = f"clip_id={clip_id}" if clip_id else "this clip"
        return f"No analysis on disk. Run media_analysis(action=\"analyze_clip\", target={{...{target}...}})."
    if cache_status == "stale_or_incomplete":
        if missing_layers:
            return (
                "Existing report is missing layers: "
                + ", ".join(missing_layers)
                + ". Re-analyze with those layers enabled."
            )
        if staleness_reasons:
            return (
                "Existing report is stale ("
                + ", ".join(staleness_reasons)
                + "). Re-analyze or pass force_refresh=true."
            )
        return "Existing report exists but is not currently reusable. Re-analyze."
    if cache_status == "reusable":
        return "Report is current and reusable for the requested depth and modalities."
    return "Coverage state could not be determined; inspect clip details."


def _coverage_evidence_line(summary: Dict[str, Any]) -> str:
    total = int(summary.get("clips_total") or 0)
    if not total:
        return "evidence base: no clips in target."
    analyzed = int(summary.get("clips_analyzed") or 0)
    stale = int(summary.get("clips_stale") or 0)
    missing = int(summary.get("clips_missing") or 0)
    blocked = int(summary.get("clips_reuse_blocked") or 0)
    needs_trust = int(summary.get("clips_needs_higher_trust") or 0)
    pct = (analyzed / total) * 100.0
    fragments = [
        f"{analyzed}/{total} clips analyzed ({pct:.0f}%)",
    ]
    if stale:
        fragments.append(f"{stale} stale")
    if missing:
        fragments.append(f"{missing} missing")
    if blocked:
        fragments.append(f"{blocked} reuse-blocked")
    if needs_trust:
        fragments.append(f"{needs_trust} below min_source_trust")
    return "evidence base: " + ", ".join(fragments) + "."


def build_coverage_report(
    *,
    project_name: Any,
    project_id: Any = None,
    records: List[Dict[str, Any]],
    target: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    capabilities: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Pure-read coverage assessment for a target's clips.

    Reports per-clip analysis state (reusable / stale_or_incomplete / miss /
    reuse_blocked), layer presence, source_trust, and a recommended next action.
    Never triggers analysis. Builds on the planner's existing reuse pipeline
    (signature, registry, related project roots, provenance integrity).

    Optional params:
      min_source_trust: filter clips below this trust tier. Tiers in ascending
        order: auto < filename < low < medium < high. Clips below the threshold
        are classified `needs_higher_trust` regardless of report freshness.
      include_layers: ignored for now — layer expectations follow the planner's
        depth-driven requirements. Future extension point.
      max_report_age_days: forwarded to planner for freshness gating.
    """
    coverage_params = dict(params or {})
    coverage_params.setdefault("dry_run", True)
    coverage_params.setdefault("session_only", False)
    min_source_trust = _normalize_min_source_trust(coverage_params.pop("min_source_trust", coverage_params.pop("minSourceTrust", None)))

    plan = build_plan(
        project_name=project_name,
        project_id=project_id,
        records=records,
        target=target,
        params=coverage_params,
        capabilities=capabilities,
    )
    if not plan.get("success"):
        return plan

    coverage_clips: List[Dict[str, Any]] = []
    source_trust_dist: Dict[str, int] = {}
    layer_coverage: Dict[str, int] = {}
    summary_counts = {
        "analyzed": 0,
        "missing": 0,
        "stale": 0,
        "reuse_blocked": 0,
        "needs_higher_trust": 0,
    }

    for clip_plan in plan.get("clips") or []:
        if not isinstance(clip_plan, dict):
            continue
        record = clip_plan.get("record") or {}
        existing = clip_plan.get("existing_report") or {}
        report_path = existing.get("path")
        report: Optional[Dict[str, Any]] = None
        if report_path and os.path.isfile(str(report_path)):
            try:
                report = _read_json(str(report_path))
            except (OSError, json.JSONDecodeError):
                report = None
        layers_present = _layers_present_in_report(report)
        for layer in layers_present:
            layer_coverage[layer] = layer_coverage.get(layer, 0) + 1

        source_trust = "unknown"
        if isinstance(report, dict):
            profile = report.get("analysis_profile") if isinstance(report.get("analysis_profile"), dict) else {}
            value = profile.get("source_trust")
            if value:
                source_trust = str(value).strip().lower()
        source_trust_dist[source_trust] = source_trust_dist.get(source_trust, 0) + 1

        cache_status = str(clip_plan.get("cache_status") or "not_checked")
        is_reusable = bool(clip_plan.get("skip_execution"))
        reuse_blocked = bool(clip_plan.get("reuse_blocked"))
        missing_layers = list(existing.get("missing_layers") or [])
        staleness_reasons = list(existing.get("cache_issues") or [])
        cache_warnings = list(existing.get("cache_warnings") or [])
        superseded_by_relink = bool(existing.get("superseded_by_relink"))

        below_min_source_trust = False
        if min_source_trust and source_trust != "unknown":
            below_min_source_trust = _source_trust_rank(source_trust) < _source_trust_rank(min_source_trust)

        if superseded_by_relink:
            summary_counts["stale"] += 1
        elif reuse_blocked:
            summary_counts["reuse_blocked"] += 1
        elif below_min_source_trust:
            summary_counts["needs_higher_trust"] += 1
        elif is_reusable:
            summary_counts["analyzed"] += 1
        elif report_path and (missing_layers or staleness_reasons):
            summary_counts["stale"] += 1
        else:
            summary_counts["missing"] += 1

        recommended_action = _recommend_coverage_action(
            cache_status=cache_status,
            reuse_blocked=reuse_blocked,
            below_min_source_trust=below_min_source_trust,
            superseded_by_relink=superseded_by_relink,
            missing_layers=missing_layers,
            staleness_reasons=staleness_reasons,
            record=record,
        )

        coverage_clips.append({
            "clip_id": record.get("clip_id"),
            "clip_name": record.get("clip_name"),
            "file_path": record.get("file_path"),
            "media_id": record.get("media_id"),
            "analyzed": is_reusable and not superseded_by_relink,
            "report_path": report_path,
            "report_project_root": existing.get("project_root"),
            "report_source": existing.get("source"),
            "cache_status": cache_status,
            "reuse_blocked": reuse_blocked,
            "superseded_by_relink": superseded_by_relink,
            "superseded_at": existing.get("superseded_at"),
            "superseded_reason": existing.get("superseded_reason"),
            "layers_present": layers_present,
            "missing_layers": missing_layers,
            "staleness_reasons": staleness_reasons,
            "cache_warnings": cache_warnings,
            "source_trust": source_trust,
            "below_min_source_trust": below_min_source_trust,
            "provenance_present": _record_has_analysis_provenance(record),
            "analyzed_at": existing.get("analyzed_at"),
            "why_not_reused": clip_plan.get("why_not_reused"),
            "recommended_action": recommended_action,
        })

    total = len(coverage_clips)
    summary = {
        "clips_total": total,
        "clips_analyzed": summary_counts["analyzed"],
        "clips_missing": summary_counts["missing"],
        "clips_stale": summary_counts["stale"],
        "clips_reuse_blocked": summary_counts["reuse_blocked"],
        "clips_needs_higher_trust": summary_counts["needs_higher_trust"],
        "coverage_percent": (summary_counts["analyzed"] / total * 100.0) if total else 0.0,
        "layer_coverage": layer_coverage,
        "source_trust_distribution": source_trust_dist,
    }

    return {
        "success": True,
        "action": "coverage_report",
        "target": plan.get("target"),
        "min_source_trust": min_source_trust,
        "evidence_base": _coverage_evidence_line(summary),
        "summary": summary,
        "clips": coverage_clips,
        "output_root": plan.get("output_root"),
        "reuse_project_roots": plan.get("reuse_project_roots"),
        "related_project_roots": plan.get("related_project_roots"),
        "analysis_version": ANALYSIS_VERSION,
        "notes": [
            "coverage_report is a pure read — it never triggers analysis.",
            "Editorial and color tools should call this first and lead any recommendation with `evidence_base`.",
        ],
    }


def analysis_root_coverage(project_root: str) -> Dict[str, Any]:
    """Standalone coverage summary — reads on-disk reports + registry, no Resolve required.

    Powers the control panel Readiness widget. Reports per-layer coverage
    counts, source_trust distribution, superseded_by_relink counts (from the
    registry), recent activity, and warning counts. Returns roughly the same
    shape as `build_coverage_report.summary` plus an `analyzed_clips` list,
    minus per-clip target/missing-layer detail (those require live records).
    """
    root = normalize_path(project_root)
    if not os.path.isdir(root):
        return {"success": False, "error": f"Analysis project root not found: {root}"}

    reports: List[Tuple[str, Dict[str, Any]]] = []
    clips_root = os.path.join(root, "clips")
    if os.path.isdir(clips_root):
        for dirpath, _, filenames in os.walk(clips_root):
            if "analysis.json" not in filenames:
                continue
            report_path = os.path.join(dirpath, "analysis.json")
            try:
                reports.append((report_path, _read_json(report_path)))
            except (OSError, json.JSONDecodeError):
                continue

    registry = _read_analysis_registry(root)
    superseded_by_path: Dict[str, Dict[str, Any]] = {}
    for entry in registry.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("superseded_by_relink"):
            continue
        path = normalize_path(entry.get("analysis_json") or "")
        if path:
            superseded_by_path[path] = {
                "superseded_at": entry.get("superseded_at"),
                "superseded_reason": entry.get("superseded_reason"),
            }

    layer_coverage: Dict[str, int] = {}
    source_trust_dist: Dict[str, int] = {}
    motion_dist: Dict[str, int] = {}
    warnings: List[str] = []
    signed_count = 0
    newest_ts = 0.0
    analyzed_clips: List[Dict[str, Any]] = []
    superseded_count = 0

    for report_path, report in reports:
        normalized_report_path = normalize_path(report_path)
        layers = _layers_present_in_report(report)
        for layer in layers:
            layer_coverage[layer] = layer_coverage.get(layer, 0) + 1

        profile = report.get("analysis_profile") if isinstance(report.get("analysis_profile"), dict) else {}
        trust = str(profile.get("source_trust") or "").strip().lower() or "unknown"
        source_trust_dist[trust] = source_trust_dist.get(trust, 0) + 1

        motion_level = (report.get("motion") or {}).get("overall_motion_level") or "unknown"
        motion_dist[str(motion_level)] = motion_dist.get(str(motion_level), 0) + 1

        warnings.extend(str(w) for w in (report.get("technical_warnings") or []))

        if report.get("analysis_signature"):
            signed_count += 1
        analyzed_ts = _timestamp_from_analyzed_at(report.get("analyzed_at")) or 0
        newest_ts = max(newest_ts, analyzed_ts)

        clip_info = report.get("clip") if isinstance(report.get("clip"), dict) else {}
        superseded_info = superseded_by_path.get(normalized_report_path)
        if superseded_info:
            superseded_count += 1
        analyzed_clips.append({
            "clip_id": clip_info.get("clip_id"),
            "clip_name": clip_info.get("clip_name"),
            "source_file": report.get("source_file") or clip_info.get("file_path"),
            "report_path": normalized_report_path,
            "analyzed_at": report.get("analyzed_at"),
            "layers_present": layers,
            "source_trust": trust,
            "superseded_by_relink": bool(superseded_info),
            "superseded_reason": (superseded_info or {}).get("superseded_reason"),
            "vision_pending": bool(
                (report.get("visual") or {}).get("status") == "pending_host_analysis"
                or (report.get("visual") or {}).get("vision_token")
            ),
            "depth": profile.get("depth"),
        })

    return {
        "success": True,
        "project_root": root,
        "registry_path": analysis_registry_path(root),
        "summary": {
            "clips_total_with_reports": len(reports),
            "clips_signed": signed_count,
            "clips_unsigned": max(0, len(reports) - signed_count),
            "clips_superseded_by_relink": superseded_count,
            "clips_vision_pending": sum(1 for clip in analyzed_clips if clip["vision_pending"]),
            "layer_coverage": layer_coverage,
            "source_trust_distribution": source_trust_dist,
            "motion_distribution": motion_dist,
            "technical_warning_count": len(warnings),
            "newest_analysis_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(newest_ts)) if newest_ts else None
            ),
        },
        "warnings": warnings[:50],
        "analyzed_clips": sorted(
            analyzed_clips,
            key=lambda row: (
                0 if row.get("superseded_by_relink") else 1,
                -float(_timestamp_from_analyzed_at(row.get("analyzed_at")) or 0),
            ),
        )[:200],
        "notes": [
            "analysis_root_coverage is a standalone read of the analysis directory.",
            "It does NOT compare against live Resolve clips; use coverage_report (action) for per-target missing-clip detection.",
        ],
    }


def cleanup_artifacts(project_root: str, *, frames_only: bool = True) -> Dict[str, Any]:
    root = normalize_path(project_root)
    if not os.path.isdir(root):
        return {"success": False, "error": f"Project analysis root not found: {root}"}
    removed = []
    if frames_only:
        for dirpath, dirnames, _ in os.walk(root):
            for dirname in list(dirnames):
                if dirname == "frames":
                    full = os.path.join(dirpath, dirname)
                    shutil.rmtree(full, ignore_errors=True)
                    removed.append(full)
    else:
        shutil.rmtree(root, ignore_errors=True)
        removed.append(root)
    return {"success": True, "removed": removed, "frames_only": frames_only}


