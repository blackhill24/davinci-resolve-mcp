"""Clip identity (slug/hash/directory) + the analysis-report registry (staleness, dedup)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.domains.media_analysis.utils.caps_gating import ANALYSIS_DIR_NAME, ANALYSIS_REGISTRY_FILENAME, ANALYSIS_REGISTRY_SCHEMA_VERSION, ANALYSIS_VERSION, DEFAULT_FRAMES_PER_MINUTE, DEFAULT_FRAME_CEILING, DEFAULT_FRAME_FLOOR, DEFAULT_MAX_RELATED_PROJECT_ROOTS, DEFAULT_SAMPLING_MODE, DEFAULT_SOURCE_TRUST, _SAMPLING_MODE_ALIASES
from src.domains.media_analysis.utils.technical_probe import _read_json, _write_json


def normalize_sampling_mode(value: Any, default: Optional[str] = None) -> Optional[str]:
    """Resolve a user-supplied mode string (label or key) to a canonical mode."""
    raw = str(value or "").strip().lower().replace("_", "_")
    return _SAMPLING_MODE_ALIASES.get(raw, default)


def _resolve_sampling_config(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Read sampling mode + tunables from analysis params, applying defaults."""
    params = params or {}

    def _first(*keys: str) -> Any:
        for key in keys:
            if key in params and params[key] is not None:
                return params[key]
        return None

    mode = normalize_sampling_mode(
        _first("sampling_mode", "samplingMode"), default=DEFAULT_SAMPLING_MODE
    ) or DEFAULT_SAMPLING_MODE

    def _pos_float(value: Any, fallback: float) -> float:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return fallback
        return f if f > 0 else fallback

    rate = _pos_float(_first("frames_per_minute", "framesPerMinute"), DEFAULT_FRAMES_PER_MINUTE)
    floor = int(_pos_float(_first("frame_floor", "frameFloor"), DEFAULT_FRAME_FLOOR))
    ceiling = int(_pos_float(_first("frame_ceiling", "frameCeiling"), DEFAULT_FRAME_CEILING))
    if ceiling < floor:
        ceiling = floor
    return {
        "mode": mode,
        "frames_per_minute": rate,
        "frame_floor": floor,
        "frame_ceiling": ceiling,
    }


def _clamp_int(value: Any, low: int, high: int) -> int:
    if high < low:
        high = low
    v = int(value)
    if v < low:
        return low
    if v > high:
        return high
    return v


def slugify(value: Any, fallback: str = "untitled") -> str:
    raw = str(value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw)
    slug = re.sub(r"-+", "-", slug).strip("-._")
    return slug or fallback


def short_hash(value: Any, length: int = 10) -> str:
    raw = str(value or "").encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()[:length]


def project_directory_name(project_name: Any, project_id: Any = None) -> str:
    basis = project_id or project_name or "project"
    return f"{slugify(project_name, 'project')}-{short_hash(basis)}"


def stable_clip_basis(record: Dict[str, Any]) -> str:
    """Return the canonical rename-stable identity used to hash a report folder.

    The canonical basis is the *normalized file path*: it is present on both
    Resolve-derived and path-based batch records, it survives a Media Pool
    rename, and a genuine relink to a different file is handled separately as a
    superseded source. Resolve-internal ids (clip_id/media_id) are absent from
    path-based records and not portable across project copies, so they are only
    used when no file path is available; the display name is the last resort.

    Folder *resolution* (matching an existing report) must tolerate the legacy
    bases too — see :func:`stable_clip_match_hashes`.
    """
    file_path = record.get("file_path")
    if file_path:
        return normalize_path(file_path)
    return str(
        record.get("clip_id")
        or record.get("media_id")
        or record.get("clip_name")
        or "clip"
    )


def stable_clip_hash(record: Dict[str, Any]) -> str:
    """Return the canonical 12-char hash that anchors a clip's report folder."""
    return short_hash(stable_clip_basis(record), 12)


def stable_clip_match_hashes(record: Dict[str, Any]) -> List[str]:
    """All folder hashes that could identify this clip's existing report.

    Returns the canonical hash first, followed by legacy bases so reports
    written before the canonical file-path scheme (clip_id-first, or a raw
    un-normalized path) still resolve without an on-disk migration. The display
    name is only used when nothing more unique is available, so two different
    clips that merely share a name are never matched to the same report.
    """
    hashes: List[str] = []
    seen: set = set()

    def add(value: Any) -> None:
        if not value:
            return
        digest = short_hash(value, 12)
        if digest not in seen:
            seen.add(digest)
            hashes.append(digest)

    file_path = record.get("file_path")
    if file_path:
        add(normalize_path(file_path))  # canonical
        add(str(file_path))             # legacy: raw, un-normalized path
    add(record.get("clip_id"))          # legacy: clip_id-first scheme
    add(record.get("media_id"))
    if not hashes:
        add(record.get("clip_name") or "clip")
    return hashes


def clip_directory_hash(name: Any) -> Optional[str]:
    """Extract the trailing stable hash from a clip report folder name.

    Folder names are ``<label>-<hash>`` where ``<label>`` is the (rename-prone)
    display slug and ``<hash>`` is :func:`stable_clip_hash`. A bare ``<hash>``
    folder (no slug) is also accepted. Returns the hash, or ``None`` if the
    trailing token is not a 12-char hex hash.
    """
    base = os.path.basename(str(name or "").rstrip("/\\"))
    suffix = base.rsplit("-", 1)[-1]
    if re.fullmatch(r"[0-9a-f]{12}", suffix):
        return suffix
    return None


def stable_clip_directory(record: Dict[str, Any]) -> str:
    label = slugify(record.get("clip_name") or Path(str(record.get("file_path") or "clip")).stem, "clip")
    return f"{label}-{stable_clip_hash(record)}"


def resolve_clip_directory(project_root: str, record: Dict[str, Any]) -> str:
    """Return the report directory for a clip, reusing an existing one if found.

    Writes go through here so a clip that was renamed, or analyzed under a legacy
    hash basis (e.g. clip_id-first, or a path-based batch report), reuses its
    existing folder instead of orphaning it under a freshly minted name. Matches
    by canonical hash first, then any legacy hash; falls back to the canonical
    new path when nothing exists yet.
    """
    clips_root = os.path.join(project_root, "clips")
    # Fast path: the canonical folder already exists by exact name. This is the
    # steady state (re-analysis of an already-canonical clip) and avoids a full
    # directory scan per clip on a batch run.
    canonical_dir = os.path.join(clips_root, stable_clip_directory(record))
    if os.path.isdir(canonical_dir):
        return normalize_path(canonical_dir)
    match = stable_clip_match_hashes(record)
    if match and os.path.isdir(clips_root):
        canonical = match[0]
        match_set = set(match)
        legacy_hit: Optional[str] = None
        try:
            entries = sorted(os.listdir(clips_root))
        except OSError:
            entries = []
        for entry in entries:
            candidate = os.path.join(clips_root, entry)
            if not os.path.isdir(candidate):
                continue
            folder_hash = clip_directory_hash(entry)
            if not folder_hash:
                continue
            if folder_hash == canonical:
                return normalize_path(candidate)
            if folder_hash in match_set and legacy_hit is None:
                legacy_hit = candidate
        if legacy_hit:
            return normalize_path(legacy_hit)
    return normalize_path(os.path.join(clips_root, stable_clip_directory(record)))


CLIP_INDEX_SCHEMA_VERSION = 1


def clip_index_path(project_root: str) -> str:
    """Path of the per-project clip index (a sidecar under clips/)."""
    return os.path.join(project_root, "clips", "index.json")


def _clip_dir_signature(clips_root: str) -> str:
    """Cheap fingerprint of the analyzed clip dirs (each analysis.json's name,
    mtime, and size) so the persisted index can be reused until a report is
    added, removed, or rewritten — without reparsing every report each poll."""
    parts: List[str] = []
    try:
        entries = sorted(os.listdir(clips_root))
    except OSError:
        return "0:none"
    for entry in entries:
        report = os.path.join(clips_root, entry, "analysis.json")
        try:
            stat = os.stat(report)
        except OSError:
            continue
        parts.append(f"{entry}:{stat.st_mtime_ns}:{stat.st_size}")
    return f"{len(parts)}:{short_hash('|'.join(parts), 16)}"


def build_clip_index(project_root: str) -> Dict[str, Any]:
    """Build and persist a hash -> folder index for the project's reports.

    Unlike a folder-name scan (which only knows the single hash baked into each
    directory name), this reads each report's ``clip`` block and indexes ALL of
    its stable ids (normalized + raw file path, clip_id, media_id). That lets the
    analyzed-count match a clip by any id it still carries — e.g. an offline clip
    that no longer reports a file path but still has its clip_id. See #51.
    """
    clips_root = os.path.join(project_root, "clips")
    hash_to_folder: Dict[str, str] = {}
    if os.path.isdir(clips_root):
        try:
            entries = sorted(os.listdir(clips_root))
        except OSError:
            entries = []
        for entry in entries:
            report_path = os.path.join(clips_root, entry, "analysis.json")
            if not os.path.isfile(report_path):
                continue
            try:
                report = _read_json(report_path)
            except (OSError, json.JSONDecodeError):
                continue
            clip_block = report.get("clip") if isinstance(report.get("clip"), dict) else {}
            hashes = set(stable_clip_match_hashes(clip_block))
            folder_hash = clip_directory_hash(entry)  # the hash baked into the name
            if folder_hash:
                hashes.add(folder_hash)
            for digest in hashes:
                hash_to_folder.setdefault(digest, entry)
    payload = {
        "schema_version": CLIP_INDEX_SCHEMA_VERSION,
        "signature": _clip_dir_signature(clips_root),
        "hash_to_folder": hash_to_folder,
    }
    if os.path.isdir(clips_root):
        try:
            _write_json(clip_index_path(project_root), payload)
        except OSError:
            pass
    return payload


def load_clip_index(project_root: str, *, rebuild_if_stale: bool = True) -> Dict[str, Any]:
    """Load the persisted clip index, rebuilding it if missing or stale.

    Freshness is decided by the cheap directory signature, so the common poll
    pays a stat-per-report instead of a full JSON reparse; a rebuild only happens
    when a report is added, removed, or rewritten.
    """
    clips_root = os.path.join(project_root, "clips")
    current_sig = _clip_dir_signature(clips_root)
    try:
        data = _read_json(clip_index_path(project_root))
    except (OSError, json.JSONDecodeError):
        data = None
    if (
        isinstance(data, dict)
        and data.get("schema_version") == CLIP_INDEX_SCHEMA_VERSION
        and data.get("signature") == current_sig
        and isinstance(data.get("hash_to_folder"), dict)
    ):
        return data
    if rebuild_if_stale:
        return build_clip_index(project_root)
    return {
        "schema_version": CLIP_INDEX_SCHEMA_VERSION,
        "signature": current_sig,
        "hash_to_folder": {},
    }


def normalize_path(path: Any) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def _is_relative_to(path: str, parent: str) -> bool:
    try:
        common = os.path.commonpath([path, parent])
    except ValueError:
        return False
    return common == parent


def _non_empty_source_paths(source_paths: Optional[Iterable[Any]]) -> List[str]:
    out = []
    for source in source_paths or []:
        if source:
            out.append(normalize_path(source))
    return out


def validate_output_root(output_root: Any, source_paths: Optional[Iterable[Any]] = None) -> Tuple[bool, List[str]]:
    """Validate that an analysis output root is not adjacent to source media."""
    errors: List[str] = []
    root = normalize_path(output_root)

    for source in _non_empty_source_paths(source_paths):
        if root == source:
            errors.append(f"analysis root cannot equal a source file path: {source}")
            continue
        parent = os.path.dirname(source)
        if parent and _is_relative_to(root, parent):
            errors.append(
                "analysis root cannot be inside a source media directory: "
                f"{root} is under {parent}"
            )

    return not errors, errors


def _analysis_root_contains_reports(project_root: str) -> bool:
    clips_root = os.path.join(project_root, "clips")
    if not os.path.isdir(clips_root):
        return False
    for _, _, filenames in os.walk(clips_root):
        if "analysis.json" in filenames:
            return True
    return False


def related_analysis_project_roots(project_root: Any, *, limit: int = DEFAULT_MAX_RELATED_PROJECT_ROOTS) -> List[str]:
    """Return sibling project analysis roots that contain reports.

    Published projects can be duplicated or renamed in Resolve, which changes
    the active project root while the source media and prior reports remain
    valid. This bounded sibling scan lets reuse find those reports by signature.
    """
    if not project_root:
        return []
    active = normalize_path(project_root)
    base_root = os.path.dirname(active)
    if not os.path.isdir(base_root):
        return []

    candidates: List[Tuple[float, str]] = []
    try:
        entries = os.listdir(base_root)
    except OSError:
        return []
    for entry in entries:
        candidate = normalize_path(os.path.join(base_root, entry))
        if candidate == active or not os.path.isdir(candidate):
            continue
        if not _analysis_root_contains_reports(candidate):
            continue
        try:
            mtime = os.path.getmtime(os.path.join(candidate, "clips"))
        except OSError:
            mtime = 0.0
        candidates.append((mtime, candidate))

    candidates.sort(key=lambda row: (-row[0], row[1]))
    return [candidate for _, candidate in candidates[: max(0, int(limit or 0))]]


def _analysis_base_root_for_project_root(project_root: Any) -> Optional[str]:
    if not project_root:
        return None
    return os.path.dirname(normalize_path(project_root))


def analysis_registry_path(project_root: Any) -> Optional[str]:
    base_root = _analysis_base_root_for_project_root(project_root)
    if not base_root:
        return None
    return os.path.join(base_root, ANALYSIS_REGISTRY_FILENAME)


def _analysis_report_project_root(path: Any) -> Optional[str]:
    candidate = normalize_path(path)
    if os.path.basename(candidate) != "analysis.json":
        return None
    clip_dir = os.path.dirname(candidate)
    clips_dir = os.path.dirname(clip_dir)
    if os.path.basename(clips_dir) != "clips":
        return None
    return os.path.dirname(clips_dir)


def _registry_entry_from_report(report_path: str, report: Dict[str, Any]) -> Dict[str, Any]:
    clip = report.get("clip") if isinstance(report.get("clip"), dict) else {}
    signature = report.get("analysis_signature") if isinstance(report.get("analysis_signature"), dict) else {}
    profile = report.get("analysis_profile") if isinstance(report.get("analysis_profile"), dict) else {}
    project_root = _analysis_report_project_root(report_path)
    return {
        "analysis_json": normalize_path(report_path),
        "project_root": project_root,
        "source_file": normalize_path(report.get("source_file") or clip.get("file_path")) if (report.get("source_file") or clip.get("file_path")) else "",
        "clip_id": str(clip.get("clip_id") or ""),
        "media_id": str(clip.get("media_id") or ""),
        "clip_name": str(clip.get("clip_name") or ""),
        "analysis_version": str(report.get("analysis_version") or ""),
        "analysis_signature": signature,
        "signature_hash": str(signature.get("signature_hash") or ""),
        "depth": profile.get("depth", ""),
        "source_trust": str(profile.get("source_trust") or "") or DEFAULT_SOURCE_TRUST,
        "vision_enabled": bool(profile.get("vision_enabled", False)),
        "transcription_enabled": bool(profile.get("transcription_enabled", False)),
        "analyzed_at": report.get("analyzed_at"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _read_analysis_registry(project_root: Any) -> Dict[str, Any]:
    path = analysis_registry_path(project_root)
    if not path or not os.path.isfile(path):
        return {"entries": []}
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {"entries": []}
    if not isinstance(payload, dict):
        return {"entries": []}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        payload["entries"] = []
    return payload


def _registry_entry_matches_record(entry: Dict[str, Any], record: Dict[str, Any]) -> bool:
    from src.domains.media_analysis.utils.subtitles_and_reuse import _normalized_report_match_value
    entry_source = _normalized_report_match_value(entry.get("source_file"), path_like=True)
    record_source = _normalized_report_match_value(record.get("file_path"), path_like=True)
    if entry_source and record_source and entry_source == record_source:
        return True
    for key in ("clip_id", "media_id"):
        entry_value = _normalized_report_match_value(entry.get(key))
        record_value = _normalized_report_match_value(record.get(key))
        if entry_value and record_value and entry_value == record_value:
            return True
    return False


REGISTRY_PRESERVED_FIELDS = ("superseded_by_relink", "superseded_at", "superseded_reason")


def update_analysis_registry(project_root: str, report_paths: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Update the per-analysis-root report registry from known analysis reports.

    Preserves relink-invalidation flags (superseded_by_relink, superseded_at,
    superseded_reason) across rebuilds so re-running analysis writeback does
    not silently clear a stale-mark applied by a prior replace_clip event.
    """
    root = normalize_path(project_root)
    base_root = _analysis_base_root_for_project_root(root)
    registry_path = analysis_registry_path(root)
    if not base_root or not registry_path:
        return {"success": False, "error": "Invalid analysis project root for registry"}

    existing = _read_analysis_registry(root)
    entries_by_path: Dict[str, Dict[str, Any]] = {}
    preserved_flags_by_path: Dict[str, Dict[str, Any]] = {}
    for entry in existing.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        report_path = entry.get("analysis_json")
        if not report_path:
            continue
        normalized_path = normalize_path(report_path)
        preserved = {key: entry[key] for key in REGISTRY_PRESERVED_FIELDS if entry.get(key) not in (None, "", False)}
        if preserved:
            preserved_flags_by_path[normalized_path] = preserved
        if os.path.isfile(normalized_path):
            entries_by_path[normalized_path] = dict(entry, analysis_json=normalized_path)

    if report_paths is None:
        from src.domains.media_analysis.utils.analysis_index_build import _iter_analysis_report_files
        candidate_paths = list(_iter_analysis_report_files(root))
    else:
        candidate_paths = [normalize_path(path) for path in report_paths if path]

    failed_reports: List[Dict[str, str]] = []
    updated_count = 0
    for report_path in candidate_paths:
        normalized_path = normalize_path(report_path)
        report_project_root = _analysis_report_project_root(normalized_path)
        if not report_project_root or not os.path.isfile(normalized_path):
            continue
        try:
            if os.path.commonpath([normalize_path(report_project_root), base_root]) != base_root:
                continue
        except ValueError:
            continue
        try:
            report = _read_json(normalized_path)
        except (OSError, json.JSONDecodeError) as exc:
            failed_reports.append({"path": normalized_path, "error": str(exc)})
            continue
        new_entry = _registry_entry_from_report(normalized_path, report)
        preserved = preserved_flags_by_path.get(normalized_path)
        if preserved:
            new_entry.update(preserved)
        entries_by_path[normalized_path] = new_entry
        updated_count += 1

    payload = {
        "success": True,
        "schema_version": ANALYSIS_REGISTRY_SCHEMA_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "base_root": base_root,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entry_count": len(entries_by_path),
        "updated_count": updated_count,
        "failed_report_count": len(failed_reports),
        "failed_reports": failed_reports[:50],
        "entries": sorted(entries_by_path.values(), key=lambda row: (str(row.get("source_file") or ""), str(row.get("analysis_json") or ""))),
    }
    try:
        _write_json(registry_path, payload)
    except OSError as exc:
        return {"success": False, "error": str(exc), "registry_path": registry_path}
    return {k: v for k, v in payload.items() if k != "entries"} | {"registry_path": registry_path}


def mark_registry_stale_for_clip(
    *,
    project_name: Any = None,
    project_id: Any = None,
    project_root: Any = None,
    analysis_root: Any = None,
    clip_id: Any = None,
    media_id: Any = None,
    source_file: Any = None,
    reason: str = "source_relinked",
) -> Dict[str, Any]:
    """Mark analysis_registry entries matching this clip as superseded by relink.

    Called from Resolve clip-replacement operations (replace_clip and friends)
    after a successful mutation so coverage_report and the reuse pipeline stop
    silently reusing the prior analysis for what is now a different underlying
    media file.

    Either `project_root` OR `project_name` (with optional `project_id`) must
    be supplied so the active analysis registry can be located.

    Matches entries by clip_id, media_id, or source_file (any match flags the
    entry). Does NOT delete the report file on disk — colorists and editors
    may still want the prior context. Sets `superseded_by_relink=true`,
    `superseded_at`, and `superseded_reason` on the registry entry; these
    flags are preserved across future `update_analysis_registry` rebuilds.

    Returns {"success": bool, "matched": int, "registry_path": str, ...}.
    """
    if not (clip_id or media_id or source_file):
        return {
            "success": False,
            "error": "mark_registry_stale_for_clip requires at least one of clip_id, media_id, or source_file",
        }

    resolved_root: Optional[str] = None
    if project_root:
        resolved_root = normalize_path(project_root)
    else:
        if project_name is None:
            return {
                "success": False,
                "error": "mark_registry_stale_for_clip requires project_root or project_name",
            }
        resolved = resolve_output_root(
            project_name=project_name,
            project_id=project_id,
            analysis_root=analysis_root,
            source_paths=[source_file] if source_file else [],
            create=False,
        )
        if not resolved.get("success"):
            return {
                "success": False,
                "error": "Could not resolve analysis project root for registry invalidation",
                "details": resolved,
            }
        resolved_root = resolved["project_root"]

    registry_path = analysis_registry_path(resolved_root)
    if not registry_path:
        return {"success": False, "error": "No registry path available for project root", "project_root": resolved_root}
    if not os.path.isfile(registry_path):
        return {
            "success": True,
            "matched": 0,
            "registry_path": registry_path,
            "project_root": resolved_root,
            "note": "No registry on disk yet; nothing to invalidate.",
        }

    payload = _read_analysis_registry(resolved_root)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return {"success": False, "error": "Registry corrupted: entries is not a list", "registry_path": registry_path}

    record_like: Dict[str, Any] = {}
    if clip_id:
        record_like["clip_id"] = str(clip_id)
    if media_id:
        record_like["media_id"] = str(media_id)
    if source_file:
        record_like["file_path"] = str(source_file)

    superseded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    matched_entries: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not _registry_entry_matches_record(entry, record_like):
            continue
        entry["superseded_by_relink"] = True
        entry["superseded_at"] = superseded_at
        entry["superseded_reason"] = str(reason or "source_relinked")
        matched_entries.append(str(entry.get("analysis_json") or ""))

    if not matched_entries:
        return {
            "success": True,
            "matched": 0,
            "registry_path": registry_path,
            "project_root": resolved_root,
            "note": "No registry entries matched the supplied clip identifiers.",
        }

    payload["entries"] = entries
    payload["updated_at"] = superseded_at
    try:
        _write_json(registry_path, payload)
    except OSError as exc:
        return {"success": False, "error": str(exc), "registry_path": registry_path}
    return {
        "success": True,
        "matched": len(matched_entries),
        "matched_report_paths": matched_entries,
        "registry_path": registry_path,
        "project_root": resolved_root,
        "reason": str(reason or "source_relinked"),
        "superseded_at": superseded_at,
    }


def registry_entry_superseded_info(project_root: Any, report_path: Any) -> Optional[Dict[str, Any]]:
    """Return the superseded-by-relink metadata for a report path, if any.

    Used by reuse-check and coverage_report to surface relink staleness even
    when the on-disk report still passes signature checks.
    """
    if not project_root or not report_path:
        return None
    normalized_path = normalize_path(report_path)
    payload = _read_analysis_registry(project_root)
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if normalize_path(entry.get("analysis_json") or "") != normalized_path:
            continue
        if entry.get("superseded_by_relink"):
            return {
                "superseded_by_relink": True,
                "superseded_at": entry.get("superseded_at"),
                "superseded_reason": entry.get("superseded_reason") or "source_relinked",
            }
        return None
    return None


def resolve_output_root(
    *,
    project_name: Any,
    project_id: Any = None,
    analysis_root: Any = None,
    source_paths: Optional[Iterable[Any]] = None,
    create: bool = False,
) -> Dict[str, Any]:
    """Resolve a project-scoped analysis root and validate source separation."""
    project_dir = project_directory_name(project_name, project_id)
    if analysis_root:
        base_root = normalize_path(analysis_root)
    else:
        base_root = normalize_path(Path.home() / "Documents" / ANALYSIS_DIR_NAME)

    # V2 P13: Don't double-append project_dir when the caller passed an
    # analysis_root that already terminates in the project slug (e.g. when
    # a previous call's project_root is re-used as the new analysis_root).
    # Previous behavior created nested {base}/{slug}/{slug}/ trees on disk.
    base_basename = os.path.basename(base_root.rstrip("/"))
    if base_basename == project_dir:
        output_root = base_root
    else:
        # Treat the provided root as a base by default so every project remains
        # isolated even when users choose a shared custom analysis location.
        output_root = normalize_path(os.path.join(base_root, project_dir))
    ok, errors = validate_output_root(output_root, source_paths)

    if ok and create:
        os.makedirs(output_root, exist_ok=True)

    return {
        "success": ok,
        "analysis_version": ANALYSIS_VERSION,
        "base_root": base_root,
        "project_root": output_root,
        "project_directory": project_dir,
        "project_name": project_name,
        "project_id": project_id,
        "errors": errors,
    }


