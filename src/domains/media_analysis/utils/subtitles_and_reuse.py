"""SRT/VTT export helpers + existing-report reuse matching and scoring."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.domains.media_analysis.utils.caps_gating import DEFAULT_DEPTH, DEFAULT_TRANSCRIPTION_ENABLED, FRAME_CAPS, SAMPLING_MODE_RANK, _coerce_bool, _timestamp_from_analyzed_at
from src.domains.media_analysis.utils.clip_identity_registry import _analysis_report_project_root, _read_analysis_registry, _registry_entry_matches_record, analysis_registry_path, normalize_path, registry_entry_superseded_info
from src.domains.media_analysis.utils.technical_probe import _read_json


def seconds_to_srt_time(seconds: float) -> str:
    ms_total = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(ms_total, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def seconds_to_vtt_time(seconds: float) -> str:
    return seconds_to_srt_time(seconds).replace(",", ".")


def segments_to_srt(segments: List[Dict[str, Any]]) -> str:
    lines = []
    for index, segment in enumerate(segments, 1):
        start = seconds_to_srt_time(float(segment.get("start", 0)))
        end = seconds_to_srt_time(float(segment.get("end", segment.get("start", 0))))
        text = str(segment.get("text", "")).strip()
        lines.append(f"{index}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def segments_to_vtt(segments: List[Dict[str, Any]]) -> str:
    lines = ["WEBVTT\n"]
    for segment in segments:
        start = seconds_to_vtt_time(float(segment.get("start", 0)))
        end = seconds_to_vtt_time(float(segment.get("end", segment.get("start", 0))))
        text = str(segment.get("text", "")).strip()
        lines.append(f"{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _iter_analysis_reports(project_root: str) -> List[Tuple[str, Dict[str, Any]]]:
    clips_root = os.path.join(normalize_path(project_root), "clips")
    reports: List[Tuple[str, Dict[str, Any]]] = []
    if not os.path.isdir(clips_root):
        return reports
    for dirpath, _, filenames in os.walk(clips_root):
        if "analysis.json" not in filenames:
            continue
        path = os.path.join(dirpath, "analysis.json")
        try:
            reports.append((path, _read_json(path)))
        except (OSError, json.JSONDecodeError):
            continue
    return reports


def _normalized_report_match_value(value: Any, *, path_like: bool = False) -> Optional[str]:
    if value in (None, ""):
        return None
    if path_like:
        try:
            return normalize_path(value)
        except Exception:
            return str(value)
    return str(value)


def _report_matches_record(report: Dict[str, Any], record: Dict[str, Any]) -> bool:
    clip = report.get("clip") or {}
    report_source = _normalized_report_match_value(report.get("source_file") or clip.get("file_path"), path_like=True)
    record_source = _normalized_report_match_value(record.get("file_path"), path_like=True)
    if report_source and record_source and report_source == record_source:
        return True
    for key in ("clip_id", "media_id"):
        report_value = _normalized_report_match_value(clip.get(key))
        record_value = _normalized_report_match_value(record.get(key))
        if report_value and record_value and report_value == record_value:
            return True
    return False


def _report_missing_layers(report: Dict[str, Any], depth: str, options: Dict[str, Any]) -> List[str]:
    missing = []
    if not report.get("technical"):
        missing.append("technical")
    if not report.get("clip_analysis_markers"):
        missing.append("marker_plan")
    if depth in {"standard", "deep", "custom"}:
        motion = report.get("motion") or {}
        readthrough = report.get("readthrough") or {}
        if not motion or motion.get("status") == "skipped":
            missing.append("motion")
        if not readthrough or readthrough.get("reason") == "quick analysis depth":
            missing.append("readthrough")
        if not isinstance(readthrough.get("cut_analysis"), dict):
            missing.append("cut_analysis")
    transcription = options.get("transcription") or {}
    if _coerce_bool(transcription.get("enabled"), default=DEFAULT_TRANSCRIPTION_ENABLED):
        transcript = report.get("transcription") or {}
        if not transcript.get("success") or transcript.get("status") == "skipped":
            missing.append("transcription")
    vision = options.get("vision") or {}
    if _coerce_bool(vision.get("enabled"), default=False):
        visual = report.get("visual") or {}
        if not visual.get("success") or visual.get("status") == "skipped":
            missing.append("vision")
    return missing


def _report_cache_state(
    report: Dict[str, Any],
    request_signature: Dict[str, Any],
    *,
    max_report_age_days: Optional[float] = None,
    reuse_policy: str = "compatible",
) -> Tuple[List[str], List[str]]:
    issues: List[str] = []
    warnings: List[str] = []

    analyzed_ts = _timestamp_from_analyzed_at(report.get("analyzed_at"))
    if max_report_age_days is not None:
        if analyzed_ts is None:
            issues.append("analysis_age_unknown")
        else:
            age_days = (time.time() - analyzed_ts) / 86400.0
            if age_days > max_report_age_days:
                issues.append(f"analysis_older_than_{max_report_age_days:g}_days")

    report_signature = report.get("analysis_signature") or {}
    if not report_signature:
        message = "analysis_signature_missing"
        if reuse_policy in {"fresh", "strict"}:
            issues.append(message)
        else:
            warnings.append(message)
        return issues, warnings

    if report_signature.get("analysis_version") != request_signature.get("analysis_version"):
        issues.append("analysis_version_changed")

    report_source = report_signature.get("source_file") or {}
    request_source = request_signature.get("source_file") or {}
    if report_source.get("path") and request_source.get("path") and report_source.get("path") != request_source.get("path"):
        issues.append("source_path_changed")
    for key in ("size_bytes", "mtime_ns"):
        report_value = report_source.get(key)
        request_value = request_source.get(key)
        if report_value is not None and request_value is not None and report_value != request_value:
            issues.append(f"source_{key}_changed")

    report_budget = int(report_signature.get("analysis_keyframe_budget") or 0)
    request_budget = int(request_signature.get("analysis_keyframe_budget") or 0)
    if report_budget < request_budget:
        issues.append("analysis_keyframe_budget_lower_than_requested")

    # Sampling-mode reconciliation: a prior report sampled under a less-thorough
    # mode can't satisfy a request for a more-thorough one. The reverse (richer
    # report, cheaper request) is reused as a free upgrade.
    report_mode = (report_signature.get("analysis_sampling") or {}).get("mode")
    request_mode = (request_signature.get("analysis_sampling") or {}).get("mode")
    if request_mode and report_mode and request_mode != report_mode:
        if SAMPLING_MODE_RANK.get(request_mode, 0) > SAMPLING_MODE_RANK.get(report_mode, 0):
            issues.append("sampling_mode_increased")

    report_layers = report_signature.get("layers") or {}
    request_layers = request_signature.get("layers") or {}
    report_vision = report_layers.get("vision") or {}
    request_vision = request_layers.get("vision") or {}
    if request_vision.get("enabled"):
        if report_vision.get("provider") and request_vision.get("provider") and report_vision.get("provider") != request_vision.get("provider"):
            issues.append("vision_provider_changed")
        if report_vision.get("prompt_hash") and request_vision.get("prompt_hash") and report_vision.get("prompt_hash") != request_vision.get("prompt_hash"):
            issues.append("vision_prompt_changed")

    report_transcription = report_layers.get("transcription") or {}
    request_transcription = request_layers.get("transcription") or {}
    if request_transcription.get("enabled"):
        for key in ("backend", "model", "language"):
            report_value = report_transcription.get(key)
            request_value = request_transcription.get(key)
            if report_value and request_value and report_value != request_value:
                issues.append(f"transcription_{key}_changed")

    return issues, warnings


def find_reusable_report(
    project_root: str,
    record: Dict[str, Any],
    depth: str,
    options: Dict[str, Any],
    *,
    request_signature: Optional[Dict[str, Any]] = None,
    max_report_age_days: Optional[float] = None,
    reuse_policy: str = "compatible",
) -> Optional[Dict[str, Any]]:
    """Find an existing analysis report that satisfies the requested layers."""
    from src.domains.media_analysis.utils.capabilities_and_planning import analysis_request_signature
    frame_count = int((request_signature or {}).get("analysis_keyframe_budget") or FRAME_CAPS.get(depth, FRAME_CAPS[DEFAULT_DEPTH]))
    request_signature = request_signature or analysis_request_signature(record, depth, options, frame_count)
    matches = []
    for path, report in _iter_analysis_reports(project_root):
        if not _report_matches_record(report, record):
            continue
        missing = _report_missing_layers(report, depth, options)
        cache_issues, cache_warnings = _report_cache_state(
            report,
            request_signature,
            max_report_age_days=max_report_age_days,
            reuse_policy=reuse_policy,
        )
        superseded = registry_entry_superseded_info(project_root, path)
        if superseded:
            cache_issues = list(cache_issues) + [
                f"source_relinked:{superseded.get('superseded_reason') or 'source_relinked'}"
            ]
        matches.append({
            "path": path,
            "report": report,
            "missing_layers": missing,
            "cache_issues": cache_issues,
            "cache_warnings": cache_warnings,
            "analyzed_at": report.get("analyzed_at"),
            "analyzed_timestamp": _timestamp_from_analyzed_at(report.get("analyzed_at")) or 0,
            "superseded_by_relink": bool(superseded),
            "superseded_at": (superseded or {}).get("superseded_at"),
            "superseded_reason": (superseded or {}).get("superseded_reason"),
        })
    if not matches:
        return None
    matches.sort(key=lambda row: (
        len(row["missing_layers"]) + len(row["cache_issues"]),
        -float(row.get("analyzed_timestamp") or 0),
    ))
    best = matches[0]
    result: Dict[str, Any] = {
        "path": best["path"],
        "missing_layers": best["missing_layers"],
        "cache_issues": best["cache_issues"],
        "cache_warnings": best["cache_warnings"],
        "analyzed_at": best.get("analyzed_at"),
    }
    if best.get("superseded_by_relink"):
        result["superseded_by_relink"] = True
        result["superseded_at"] = best.get("superseded_at")
        result["superseded_reason"] = best.get("superseded_reason")
    if best["missing_layers"] or best["cache_issues"]:
        result["reusable"] = False
        return result
    result["reusable"] = True
    result["report"] = best["report"]
    return result


def _record_analysis_report_paths(record: Dict[str, Any]) -> List[str]:
    paths: List[str] = []
    for key in (
        "analysis_report_path",
        "analysisReportPath",
        "published_analysis_report_path",
        "publishedAnalysisReportPath",
    ):
        value = record.get(key)
        if isinstance(value, str):
            paths.append(value)
        elif isinstance(value, list):
            paths.extend(str(item) for item in value if item)

    third_party = record.get("third_party_metadata") or record.get("thirdPartyMetadata")
    if isinstance(third_party, dict):
        value = third_party.get("davinci_resolve_mcp.analysis_report_path")
        if value:
            paths.append(str(value))

    deduped: List[str] = []
    for path in paths:
        normalized = normalize_path(path)
        if normalized not in deduped:
            deduped.append(normalized)
    return deduped


def _analysis_project_root_from_report_path(path: str) -> Optional[str]:
    return _analysis_report_project_root(path)


def _record_analysis_provenance(record: Dict[str, Any]) -> Dict[str, Any]:
    provenance = record.get("analysis_provenance")
    if isinstance(provenance, dict) and provenance:
        return dict(provenance)

    found: Dict[str, Any] = {}
    report_paths = _record_analysis_report_paths(record)
    if report_paths:
        found["analysis_report_paths"] = report_paths
    for key in ("published_analysis_signature", "publishedAnalysisSignature"):
        if record.get(key):
            found["analysis_signature"] = record.get(key)
            break
    for key in ("published_analysis_at", "publishedAnalysisAt"):
        if record.get(key):
            found["published_at"] = record.get(key)
            break
    third_party = record.get("third_party_metadata") or record.get("thirdPartyMetadata")
    if isinstance(third_party, dict):
        third_party_keys = sorted(
            key for key in third_party
            if str(key).startswith("davinci_resolve_mcp.")
        )
        if third_party_keys:
            found["third_party_keys"] = third_party_keys
    if record.get("analysis_metadata_present"):
        found["standard_metadata_present"] = True
        if record.get("analysis_metadata_fields"):
            found["standard_metadata_fields"] = list(record.get("analysis_metadata_fields") or [])
    return found


def _record_has_analysis_provenance(record: Dict[str, Any]) -> bool:
    return bool(_record_analysis_provenance(record))


def _reuse_issue_summary(existing: Optional[Dict[str, Any]]) -> List[str]:
    if not existing:
        return []
    issues: List[str] = []
    issues.extend(str(item) for item in existing.get("missing_layers") or [])
    issues.extend(str(item) for item in existing.get("cache_issues") or [])
    return issues


def _why_not_reused(existing: Optional[Dict[str, Any]], *, provenance_present: bool = False) -> str:
    if existing:
        issues = _reuse_issue_summary(existing)
        if issues:
            return "Existing analysis was found but could not be reused: " + ", ".join(issues)
        return "Existing analysis was found but was not marked reusable."
    if provenance_present:
        return "Resolve metadata indicates prior MCP analysis, but no reusable analysis report could be validated."
    return "No existing compatible analysis report was found."


def _mark_reuse_blocked(clip_plan: Dict[str, Any], record: Dict[str, Any], existing: Optional[Dict[str, Any]]) -> None:
    provenance = _record_analysis_provenance(record)
    clip_plan["cache_status"] = "reuse_blocked"
    clip_plan["reuse_blocked"] = True
    clip_plan["analysis_provenance"] = provenance
    clip_plan["why_not_reused"] = _why_not_reused(existing, provenance_present=True)
    clip_plan["reuse_block_reason"] = (
        "Analysis provenance is already published on this Resolve clip, but the planner "
        "could not validate a compatible report. Pass force_refresh=true to intentionally "
        "reanalyze, or restore the referenced analysis report."
    )
    if existing:
        clip_plan["reuse_block_issues"] = _reuse_issue_summary(existing)


def _report_path_candidate_issue(path: str, issue: str) -> Dict[str, Any]:
    return {
        "path": normalize_path(path),
        "missing_layers": [],
        "cache_issues": [issue],
        "cache_warnings": [],
        "analyzed_at": None,
        "reusable": False,
        "source": "record_analysis_report_path",
    }


def find_reusable_report_from_path(
    report_path: str,
    record: Dict[str, Any],
    depth: str,
    options: Dict[str, Any],
    *,
    request_signature: Optional[Dict[str, Any]] = None,
    max_report_age_days: Optional[float] = None,
    reuse_policy: str = "compatible",
) -> Optional[Dict[str, Any]]:
    """Validate a report path published on the Resolve clip and score it for reuse."""
    from src.domains.media_analysis.utils.capabilities_and_planning import analysis_request_signature
    candidate_path = normalize_path(report_path)
    project_root = _analysis_project_root_from_report_path(candidate_path)
    if not project_root:
        return _report_path_candidate_issue(candidate_path, "analysis_report_path_not_analysis_json_layout")
    if not os.path.isfile(candidate_path):
        return _report_path_candidate_issue(candidate_path, "analysis_report_path_missing")

    try:
        report = _read_json(candidate_path)
    except (OSError, json.JSONDecodeError):
        return _report_path_candidate_issue(candidate_path, "analysis_report_path_unreadable")

    if not _report_matches_record(report, record):
        return _report_path_candidate_issue(candidate_path, "analysis_report_path_record_mismatch")

    frame_count = int((request_signature or {}).get("analysis_keyframe_budget") or FRAME_CAPS.get(depth, FRAME_CAPS[DEFAULT_DEPTH]))
    request_signature = request_signature or analysis_request_signature(record, depth, options, frame_count)
    missing = _report_missing_layers(report, depth, options)
    cache_issues, cache_warnings = _report_cache_state(
        report,
        request_signature,
        max_report_age_days=max_report_age_days,
        reuse_policy=reuse_policy,
    )
    superseded = registry_entry_superseded_info(project_root, candidate_path)
    if superseded:
        cache_issues = list(cache_issues) + [
            f"source_relinked:{superseded.get('superseded_reason') or 'source_relinked'}"
        ]
    base = {
        "path": candidate_path,
        "missing_layers": missing,
        "cache_issues": cache_issues,
        "cache_warnings": cache_warnings,
        "analyzed_at": report.get("analyzed_at"),
        "project_root": project_root,
        "source": "record_analysis_report_path",
    }
    if superseded:
        base["superseded_by_relink"] = True
        base["superseded_at"] = superseded.get("superseded_at")
        base["superseded_reason"] = superseded.get("superseded_reason")
    if missing or cache_issues:
        return {**base, "reusable": False}
    return {**base, "reusable": True, "report": report}


def find_reusable_report_from_registry(
    project_root: str,
    record: Dict[str, Any],
    depth: str,
    options: Dict[str, Any],
    *,
    request_signature: Optional[Dict[str, Any]] = None,
    max_report_age_days: Optional[float] = None,
    reuse_policy: str = "compatible",
) -> Optional[Dict[str, Any]]:
    registry = _read_analysis_registry(project_root)
    candidates: List[Dict[str, Any]] = []
    for entry in registry.get("entries") or []:
        if not isinstance(entry, dict) or not _registry_entry_matches_record(entry, record):
            continue
        report_path = entry.get("analysis_json")
        if not report_path:
            continue
        candidate = find_reusable_report_from_path(
            str(report_path),
            record,
            depth,
            options,
            request_signature=request_signature,
            max_report_age_days=max_report_age_days,
            reuse_policy=reuse_policy,
        )
        if not candidate:
            continue
        candidate = dict(candidate)
        candidate["source"] = "analysis_registry"
        candidate["registry_path"] = analysis_registry_path(project_root)
        candidates.append(candidate)
    if not candidates:
        return None
    reusable = [row for row in candidates if row.get("reusable")]
    pool = reusable or candidates
    pool.sort(key=_report_reuse_score)
    return pool[0]


def _report_reuse_score(candidate: Optional[Dict[str, Any]]) -> Tuple[int, float]:
    if not candidate:
        return (9999, 0.0)
    missing = candidate.get("missing_layers") or []
    issues = candidate.get("cache_issues") or []
    timestamp = _timestamp_from_analyzed_at(candidate.get("analyzed_at")) or 0
    return (len(missing) + len(issues), -float(timestamp))


def find_reusable_report_across_roots(
    project_roots: Iterable[Any],
    record: Dict[str, Any],
    depth: str,
    options: Dict[str, Any],
    *,
    request_signature: Optional[Dict[str, Any]] = None,
    max_report_age_days: Optional[float] = None,
    reuse_policy: str = "compatible",
) -> Optional[Dict[str, Any]]:
    """Find the best compatible report across active and prior project roots."""
    candidates: List[Dict[str, Any]] = []
    seen_roots = set()
    for raw_root in project_roots or []:
        if not raw_root:
            continue
        root = normalize_path(raw_root)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        candidate = find_reusable_report(
            root,
            record,
            depth,
            options,
            request_signature=request_signature,
            max_report_age_days=max_report_age_days,
            reuse_policy=reuse_policy,
        )
        if not candidate:
            continue
        candidate = dict(candidate)
        candidate["project_root"] = root
        candidates.append(candidate)
    if not candidates:
        return None
    reusable = [row for row in candidates if row.get("reusable")]
    pool = reusable or candidates
    pool.sort(key=_report_reuse_score)
    return pool[0]


