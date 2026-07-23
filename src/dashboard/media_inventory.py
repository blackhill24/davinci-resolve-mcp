"""Media-pool inventory scan + status classification for the dashboard."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from src.domains.media_analysis.utils.clip_identity_registry import (
    clip_directory_hash,
    load_clip_index,
    stable_clip_directory,
    stable_clip_match_hashes,
)
from src.domains.media_analysis.utils.media_analysis_jobs import MEDIA_EXTENSIONS
from src.dashboard.resolve_helpers import _RESOLVE_API_LOCK, _clip_props, _connect_resolve_read_only, _current_resolve_project_id, _first_prop, _safe_call, _safe_id, _safe_name


# ── File-existence probing ──────────────────────────────────────────────────
# stat() calls on mounted network storage dominate inventory time (300+ source
# clips on a Z:\ share can take tens of seconds serially). We probe paths in a
# thread pool and memoize results for a short TTL so the recurring media poll
# does not re-stat unchanged paths every few seconds.
_PATH_EXISTS_TTL = 60.0
_PATH_PROBE_WORKERS = 16
_PATH_EXISTS_CACHE: Dict[str, Tuple[float, bool]] = {}
_PATH_EXISTS_LOCK = threading.Lock()


def _cached_path_exists(path: str, now: float, ttl: float) -> Optional[bool]:
    with _PATH_EXISTS_LOCK:
        entry = _PATH_EXISTS_CACHE.get(path)
    if entry is not None and (now - entry[0]) <= ttl:
        return entry[1]
    return None


def _store_path_exists(path: str, exists: bool, now: float) -> None:
    with _PATH_EXISTS_LOCK:
        _PATH_EXISTS_CACHE[path] = (now, exists)


def _probe_paths_exist(paths: Any, *, probe: bool = True, ttl: float = _PATH_EXISTS_TTL) -> Dict[str, bool]:
    """Resolve a collection of file paths to existence booleans.

    With ``probe=True`` (first load / manual refresh) any cache entry older than
    ``ttl`` is re-stat'd, and uncached paths are probed in parallel. With
    ``probe=False`` (background poll) the filesystem is never touched: cached
    values are reused at any age and unknown paths fall back to ``True`` —
    Resolve's own online/offline Status property still flags clips it knows are
    missing, so we trust it rather than paying for a network round-trip on every
    poll.
    """
    distinct = {str(p) for p in paths if p}
    result: Dict[str, bool] = {}
    to_probe: List[str] = []
    now = time.time()
    lookup_ttl = ttl if probe else float("inf")
    for path in distinct:
        cached = _cached_path_exists(path, now, lookup_ttl)
        if cached is not None:
            result[path] = cached
        elif probe:
            to_probe.append(path)
        else:
            result[path] = True
    if to_probe:
        workers = max(1, min(_PATH_PROBE_WORKERS, len(to_probe)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for path, exists in zip(to_probe, pool.map(os.path.exists, to_probe)):
                result[path] = bool(exists)
                _store_path_exists(path, bool(exists), now)
    return result


def _media_status(props: Dict[str, Any], file_path: Optional[str], *, file_exists: Optional[bool] = None) -> str:
    status_text = str(_first_prop(props, ("Status", "Media Status", "Online Status", "Offline")) or "").strip().lower()
    if not file_path:
        return "no_path"
    if "offline" in status_text or status_text in {"true", "yes", "1"}:
        return "offline"
    if "missing" in status_text:
        return "missing_file"
    # Pass `file_exists` in to reuse a single os.path.exists() probe — stat calls on
    # network source media are slow, so the caller avoids probing the same path twice.
    if file_exists is None:
        file_exists = os.path.exists(str(file_path))
    if not file_exists:
        return "missing_file"
    return "online"


_RESOLVE_CONTAINER_TYPE_PARTS = (
    "adjustment",
    "compound",
    "fusion",
    "generator",
    "multicam",
    "multi cam",
    "sequence",
    "subclip",
    "sub clip",
    "timeline",
    "title",
)


def _truthy_resolve_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _source_clip_status(record: Dict[str, Any], props: Dict[str, Any]) -> Tuple[bool, str]:
    media_type = str(record.get("media_type") or "").strip()
    media_type_lower = media_type.lower()
    for marker in _RESOLVE_CONTAINER_TYPE_PARTS:
        if marker in media_type_lower:
            return False, f"Resolve {media_type or 'container'} item"

    subclip_flag = _first_prop(props, ("Sub Clip", "SubClip", "Is Sub Clip", "IsSubClip", "Is Subclip"))
    if _truthy_resolve_value(subclip_flag):
        return False, "Resolve subclip"

    file_path = record.get("file_path")
    if not file_path:
        return False, "No source file path exposed"

    extension = os.path.splitext(str(file_path))[1].lower()
    if extension not in MEDIA_EXTENSIONS:
        return False, f"Unsupported extension: {extension or 'none'}"

    return True, "Source media clip"


def _record_is_sequence(record: Dict[str, Any]) -> bool:
    media_type_lower = str(record.get("media_type") or "").strip().lower()
    return "sequence" in media_type_lower or "timeline" in media_type_lower


def _analyzable_clip_status(record: Dict[str, Any], props: Dict[str, Any]) -> Tuple[bool, str]:
    source_clip, reason = _source_clip_status(record, props)
    if not source_clip:
        return False, reason

    if record.get("status") != "online":
        return False, str(record.get("status") or "offline").replace("_", " ")

    return True, "Online source media"


def _resolve_clip_record(clip: Any, bin_path: str, selected_ids: set) -> Dict[str, Any]:
    props = _clip_props(clip)
    file_path = _first_prop(props, ("File Path", "FilePath"))
    media_id, _ = _safe_call(clip, "GetMediaId")
    clip_id = _safe_id(clip)
    record = {
        "clip_id": clip_id,
        "clip_name": _safe_name(clip),
        "bin_path": bin_path,
        "file_path": str(file_path) if file_path else None,
        "media_id": str(media_id) if media_id else None,
        "duration": _first_prop(props, ("Duration",)),
        "fps": _first_prop(props, ("FPS", "Frame Rate")),
        "resolution": _first_prop(props, ("Resolution",)),
        "media_type": _first_prop(props, ("Type", "Media Type")),
        "proxy": _first_prop(props, ("Proxy", "Proxy Media Path")),
        "resolve_status": _first_prop(props, ("Status", "Media Status", "Online Status", "Offline")),
        "selected": bool(clip_id and clip_id in selected_ids),
    }
    # File-existence is resolved in a single parallel batch after every clip's
    # Resolve properties are gathered (see resolve_media_inventory), so we stash
    # the props and defer existence-dependent fields to _finalize_clip_record.
    record["clip_key"] = stable_clip_directory(record)
    record["_props"] = props
    return record


def _finalize_clip_record(record: Dict[str, Any], file_exists: bool) -> Dict[str, Any]:
    props = record.pop("_props", {}) or {}
    record["file_exists"] = file_exists
    record["status"] = _media_status(props, record["file_path"], file_exists=file_exists)
    record["source_clip"], record["source_clip_reason"] = _source_clip_status(record, props)
    record["analyzable"], record["analyzable_reason"] = _analyzable_clip_status(record, props)
    return record


def _append_folder_media(
    folder: Any,
    *,
    bin_path: str,
    recursive: bool,
    selected_ids: set,
    records: List[Dict[str, Any]],
    warnings: List[str],
    limit: int,
    exclude_bins: Optional[set] = None,
) -> bool:
    clips, clip_err = _safe_call(folder, "GetClipList")
    if clip_err:
        warnings.append(f"GetClipList failed for {bin_path}: {clip_err}")
        clips = []
    for clip in clips or []:
        if len(records) >= limit:
            return True
        records.append(_resolve_clip_record(clip, bin_path, selected_ids))

    if not recursive:
        return False
    subfolders, folder_err = _safe_call(folder, "GetSubFolderList")
    if folder_err:
        warnings.append(f"GetSubFolderList failed for {bin_path}: {folder_err}")
        return False
    for subfolder in subfolders or []:
        if len(records) >= limit:
            return True
        child_name = _safe_name(subfolder, "Unnamed")
        if exclude_bins and child_name in exclude_bins:
            continue
        truncated = _append_folder_media(
            subfolder,
            bin_path=f"{bin_path}/{child_name}",
            recursive=recursive,
            selected_ids=selected_ids,
            records=records,
            warnings=warnings,
            limit=limit,
            exclude_bins=exclude_bins,
        )
        if truncated:
            return True
    return False


def _empty_media_counts() -> Dict[str, int]:
    return {
        "total": 0,
        "source_clips": 0,
        "sequences": 0,
        "analyzable": 0,
        "not_analyzable": 0,
        "online": 0,
        "offline": 0,
        "missing_file": 0,
        "no_path": 0,
        "analyzed": 0,
        "selected": 0,
    }


def _analysis_status_by_clip(project_root: str, records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    keys = [record.get("clip_key") for record in records if record.get("clip_key")]
    status_by_key: Dict[str, Dict[str, Any]] = {}
    if not keys:
        return status_by_key

    # Resolve each clip to its report via the persisted clip index, which maps
    # every stable id found in a report (normalized + raw file path, clip_id,
    # media_id) to its folder. This survives a Media Pool rename, a legacy hash
    # basis, AND an offline clip that no longer reports a file path but still
    # carries its clip_id — none of which a folder-name scan can match. See #51.
    clips_root = os.path.join(project_root, "clips")
    hash_to_folder = load_clip_index(project_root).get("hash_to_folder") or {}

    for record in records:
        clip_key = record.get("clip_key")
        if not clip_key:
            continue
        report_path = os.path.join(project_root, "clips", str(clip_key), "analysis.json")
        if not os.path.isfile(report_path):
            # Renamed/legacy/offline clip: the recomputed clip_key no longer
            # matches the folder on disk. Fall back to any of the clip's stable
            # hashes via the index.
            for clip_hash in stable_clip_match_hashes(record):
                folder = hash_to_folder.get(clip_hash)
                if not folder:
                    continue
                candidate = os.path.join(clips_root, folder, "analysis.json")
                if os.path.isfile(candidate):
                    report_path = candidate
                    break
        if os.path.isfile(report_path):
            status_by_key[str(clip_key)] = {
                "analysis_status": "analyzed",
                "analysis_report_path": report_path,
            }

    db_path = os.path.join(project_root, "jobs.sqlite")
    if not os.path.isfile(db_path):
        return status_by_key

    # The jobs DB stores each clip under the clip_key it had when analyzed. A
    # clip renamed afterwards produces a new clip_key, so an exact-key match
    # misses its job row. Index unresolved records by their rename-stable hash
    # so a DB row recorded under the old name (e.g. a reused batch report living
    # outside the local clips/ dir) still maps back to the current clip. #51.
    key_set = {str(k) for k in keys}
    pending_hash_to_key: Dict[str, str] = {}
    for record in records:
        clip_key = record.get("clip_key")
        if not clip_key or str(clip_key) in status_by_key:
            continue
        for folder_hash in stable_clip_match_hashes(record):
            pending_hash_to_key.setdefault(folder_hash, str(clip_key))

    def _apply_row(row: sqlite3.Row, target_key: str) -> None:
        if status_by_key.get(target_key, {}).get("analysis_status") == "analyzed":
            return
        db_status = row["status"]
        report_path = row["report_path"]
        # In media_analysis_jobs, 'succeeded' = fresh analysis written this run,
        # 'skipped' = an existing analysis report was reused. Both indicate the
        # clip has been analyzed (a report exists on disk). Normalize them to
        # "analyzed" for callers that just want to know "is there a report?",
        # but preserve the raw job state under job_status for diagnostics.
        report_resolves = False
        if isinstance(report_path, str) and report_path:
            try:
                resolved = os.path.realpath(os.path.abspath(os.path.expanduser(report_path)))
                report_resolves = os.path.isfile(resolved)
            except Exception:
                report_resolves = False
        normalized = db_status
        if db_status in ("succeeded", "skipped") and report_resolves:
            normalized = "analyzed"
        status_by_key[target_key] = {
            "analysis_status": normalized,
            "job_status": db_status,
            "cache_status": row["cache_status"],
            "analysis_report_path": report_path,
            "analysis_error": row["error"],
            "job_id": row["job_id"],
            "job_name": row["job_name"],
            "job_updated_at": row["updated_at"],
        }

    select_cols = (
        "SELECT jc.clip_key, jc.status, jc.cache_status, jc.report_path, jc.error, "
        "j.job_id, j.name AS job_name, j.updated_at "
        "FROM job_clips jc JOIN jobs j ON j.job_id = jc.job_id"
    )
    placeholders = ",".join("?" for _ in keys)
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"{select_cols} WHERE jc.clip_key IN ({placeholders}) ORDER BY jc.updated_at DESC",
            keys,
        ).fetchall()
        for row in rows:
            _apply_row(row, str(row["clip_key"]))
        # Only pay for the unfiltered scan when a rename actually left a record
        # unresolved by the disk pass and exact-key match above.
        unresolved = {
            h: k for h, k in pending_hash_to_key.items() if k not in status_by_key
        }
        if unresolved:
            for row in conn.execute(
                f"{select_cols} ORDER BY jc.updated_at DESC"
            ).fetchall():
                raw_key = str(row["clip_key"])
                if raw_key in key_set:
                    continue
                row_hash = clip_directory_hash(raw_key)
                target_key = unresolved.get(row_hash) if row_hash else None
                if target_key:
                    _apply_row(row, target_key)
    except Exception:
        return status_by_key
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return status_by_key


# Last full Resolve walk per project_root, kept overlay-free so the analysis
# status can be re-applied cheaply on every background poll without re-walking
# the Media Pool (the expensive, non-parallelizable GetClipProperty pass).
_INVENTORY_CACHE: Dict[str, Dict[str, Any]] = {}
_INVENTORY_LOCK = threading.Lock()


def _get_cached_inventory(project_root: str) -> Optional[Dict[str, Any]]:
    with _INVENTORY_LOCK:
        return _INVENTORY_CACHE.get(project_root)


def _store_cached_inventory(project_root: str, entry: Dict[str, Any]) -> None:
    with _INVENTORY_LOCK:
        _INVENTORY_CACHE[project_root] = entry


def _assemble_inventory_payload(project_root: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the (local, cheap) analysis-status overlay onto cached base records.

    Base records hold the Resolve-derived fields plus file existence; the analysis
    overlay (queued/running/analyzed, report paths, job ids) is re-read from disk
    every call so a background poll reflects job progress without touching Resolve.
    Records are copied so the cached base stays overlay-free across polls.
    """
    records = [dict(record) for record in entry["base_records"]]
    status_by_key = _analysis_status_by_clip(project_root, records)
    counts = _empty_media_counts()
    counts["total"] = len(records)
    counts["selected"] = entry.get("selected_count", sum(1 for r in records if r.get("selected")))
    for record in records:
        status = record.get("status") or "unknown"
        if status in counts:
            counts[status] += 1
        if _record_is_sequence(record):
            counts["sequences"] += 1
        if record.get("source_clip"):
            counts["source_clips"] += 1
        if record.get("analyzable"):
            counts["analyzable"] += 1
        else:
            counts["not_analyzable"] += 1
        analysis = status_by_key.get(str(record.get("clip_key") or ""), {})
        record.update(analysis)
        record.setdefault("analysis_status", "not analyzed")
        if record["analysis_status"] in {"analyzed", "succeeded", "skipped"}:
            counts["analyzed"] += 1

    return {
        "success": True,
        "resolve_available": True,
        "status": "Resolve connected",
        "project": entry["project"],
        "project_root": project_root,
        "clips": records,
        "counts": counts,
        "truncated": bool(entry.get("truncated")),
        "limit": entry.get("limit"),
        "warnings": entry.get("warnings", []),
    }


def resolve_media_inventory(
    project_root: str,
    *,
    limit: Any = 500,
    exclude_bins: Optional[set] = None,
    recursive: bool = True,
    probe_paths: bool = True,
    reuse_cached: bool = False,
) -> Dict[str, Any]:
    try:
        max_items = max(1, min(int(limit), 10000))
    except (TypeError, ValueError):
        max_items = 500

    # Background polls only need to surface analysis progress (a local, disk-backed
    # signal), so they reuse the last Resolve walk instead of paying for ~N serial
    # GetClipProperty round-trips again. A cheap project-id check still catches a
    # project switch made directly in Resolve (a handful of API calls vs a full
    # walk); we rebuild only on a confirmed mismatch. If the current project can't
    # be determined (Resolve down / no project open), we keep serving the cache —
    # a transient blip shouldn't trigger an expensive rebuild on every poll.
    if reuse_cached:
        cached = _get_cached_inventory(project_root)
        if cached is not None:
            current_id, id_error = _current_resolve_project_id()
            cached_id = (cached.get("project") or {}).get("id")
            project_changed = (
                id_error is None
                and current_id is not None
                and str(current_id) != str(cached_id)
            )
            if not project_changed:
                return _assemble_inventory_payload(project_root, cached)

    # Everything that touches the Resolve scripting API stays under the lock; the
    # parallel path probe and the disk overlay run outside it.
    with _RESOLVE_API_LOCK:
        resolve, resolve_error = _connect_resolve_read_only()
        if resolve_error:
            return {
                "success": True,
                "resolve_available": False,
                "status": "Resolve unavailable",
                "error": resolve_error,
                "clips": [],
                "counts": _empty_media_counts(),
            }
        pm, pm_error = _safe_call(resolve, "GetProjectManager")
        project = None
        if pm and not pm_error:
            project, _ = _safe_call(pm, "GetCurrentProject")
        if not project:
            return {
                "success": True,
                "resolve_available": False,
                "status": "No Resolve project",
                "error": "DaVinci Resolve is connected, but no project is open.",
                "clips": [],
                "counts": _empty_media_counts(),
            }
        media_pool, mp_error = _safe_call(project, "GetMediaPool")
        if not media_pool or mp_error:
            return {
                "success": True,
                "resolve_available": False,
                "status": "Media Pool unavailable",
                "error": mp_error or "Failed to get Resolve Media Pool",
                "clips": [],
                "counts": _empty_media_counts(),
            }
        root_folder, root_error = _safe_call(media_pool, "GetRootFolder")
        if not root_folder or root_error:
            return {
                "success": True,
                "resolve_available": False,
                "status": "Root folder unavailable",
                "error": root_error or "Failed to get Resolve root folder",
                "clips": [],
                "counts": _empty_media_counts(),
            }

        selected_ids = set()
        selected_clips, _ = _safe_call(media_pool, "GetSelectedClips")
        for clip in selected_clips or []:
            clip_id = _safe_id(clip)
            if clip_id:
                selected_ids.add(clip_id)

        warnings: List[str] = []
        records: List[Dict[str, Any]] = []
        truncated = _append_folder_media(
            root_folder,
            bin_path="Master",
            recursive=recursive,
            selected_ids=selected_ids,
            records=records,
            warnings=warnings,
            limit=max_items,
            exclude_bins=exclude_bins,
        )
        project_info = {
            "name": _safe_name(project, "Resolve Project"),
            "id": _safe_id(project),
        }
        selected_count = len(selected_ids)

    # Resolve every clip's file path in one parallel, cache-backed batch, then
    # finalize existence-dependent fields (status / analyzable).
    existence = _probe_paths_exist(
        (record.get("file_path") for record in records),
        probe=probe_paths,
    )
    for record in records:
        file_path = record.get("file_path")
        _finalize_clip_record(record, bool(file_path) and existence.get(str(file_path), False))

    entry = {
        "base_records": records,
        "project": project_info,
        "selected_count": selected_count,
        "truncated": bool(truncated),
        "limit": max_items,
        "warnings": warnings,
    }
    _store_cached_inventory(project_root, entry)
    return _assemble_inventory_payload(project_root, entry)


