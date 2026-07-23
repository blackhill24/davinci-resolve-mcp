"""C6 timeline-version-chain and edit-plan endpoints."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from src.core import brain_edits as _brain_edits
from src.core import timeline_brain_db as _timeline_brain_db
from src.core import timeline_versioning as _timeline_versioning
from src.dashboard.clip_review import _v2_find_clip_dir, _v2_load_analysis, _v2_pick_representative_frame_index, _v2_read_corrections_for_dir


def _v2_create_timeline_from_clips(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/resolve/create_timeline_from_clips → proxies media_pool
    action="create_timeline_from_clips". Body: {name?, clip_ids: [str, ...]}.
    """
    clip_ids = body.get("clip_ids")
    if not isinstance(clip_ids, list) or not clip_ids:
        return {"success": False, "error": "clip_ids must be a non-empty list"}
    name = str(body.get("name") or "Review Selection").strip() or "Review Selection"
    try:
        from src.server import media_pool
        return media_pool("create_timeline_from_clips", params={"name": name, "clip_ids": list(clip_ids)})
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def _v2_open_clip_in_resolve(body: Dict[str, Any]) -> Dict[str, Any]:
    """Proxy POST /api/resolve/open_clip → media_pool_item(open_in_viewer) /
    resolve_control(save_state|restore_state). Used by the shot-detail
    'Open in Resolve' button and the save-before-preview workflow.

    Body shape:
      {action: "open_in_viewer" (default), clip_id, mark_in_seconds?,
       mark_out_seconds?, clear_marks?, mark_type?, page?}
      {action: "save_state"}
      {action: "restore_state", state_token}
    """
    requested_action = (body.get("action") or "open_in_viewer").strip().lower()
    try:
        if requested_action in {"save_state", "restore_state"}:
            from src.server import resolve_control
            return resolve_control(requested_action, params=body)
        if requested_action == "open_in_viewer":
            from src.server import media_pool_item
            params = dict(body)
            params.pop("action", None)
            params.setdefault("page", "media")
            return media_pool_item("open_in_viewer", params=params)
        return {"success": False, "error": f"Unsupported action {requested_action!r}"}
    except Exception as exc:  # noqa: BLE001 — dashboard surface should never crash
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


# ─── C6: timeline version chain + brain-edit history helpers ─────────────────


def list_timelines_with_versions(project_root: str) -> Dict[str, Any]:
    """Every timeline that has at least one archived version, with counts."""
    try:
        conn = _timeline_brain_db.connect(project_root)
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}", "timelines": []}
    rows = conn.execute(
        """
        SELECT timeline_name,
               COUNT(*) AS version_count,
               MAX(version) AS latest_version,
               MAX(created_at) AS most_recent
        FROM timeline_versions
        GROUP BY timeline_name
        ORDER BY most_recent DESC NULLS LAST
        """
    ).fetchall()
    return {
        "success": True,
        "timelines": [dict(r) for r in rows],
    }


def get_timeline_history_payload(
    project_root: str, timeline_name: str, *, history_limit: int = 200,
) -> Dict[str, Any]:
    """Combined payload: version chain + brain edits for a single timeline."""
    versions = _timeline_versioning.list_timeline_versions(
        project_root=project_root, timeline_name=timeline_name,
    )
    edits = _brain_edits.get_brain_edit_history(
        project_root=project_root, timeline_name=timeline_name, limit=history_limit,
    )
    return {
        "success": True,
        "timeline_name": timeline_name,
        "versions": versions,
        "edits": edits,
    }


def list_edit_plans_payload(project_root: str) -> Dict[str, Any]:
    """Edit-engine plan list for the panel browser (DB/file only, no Resolve).

    Fingerprint-corrupt plans surface as {"plan_id", "corrupt": True} warning
    rows rather than being silently hidden.
    """
    try:
        from src.domains.auto_edit.utils import edit_engine as _edit_engine
        return _edit_engine.list_plans(project_root, limit=50, include_corrupt=True)
    except Exception as exc:  # noqa: BLE001 — panel reads fail soft
        return {"success": False, "error": f"{type(exc).__name__}: {exc}", "plans": []}


def get_edit_plan_payload(project_root: str, plan_id: str) -> Dict[str, Any]:
    """Full plan detail for the panel, enriched for rendering: selects
    decisions and swap alternates gain a `thumb_frame_index` (the shot's first
    sampled frame, for the existing /api/clips/<id>/frames/<idx> route) and a
    `resolve_clip_id` fallback mapped from clip_uuid. Enrichment is best-effort
    — the plan still renders without thumbnails when the DB is unavailable.
    """
    try:
        from src.domains.auto_edit.utils import edit_engine as _edit_engine
        plan = _edit_engine.load_plan(project_root, plan_id)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    if plan is None:
        return {"success": False, "error": f"Plan {plan_id} not found"}
    if plan.get("_corrupt"):
        return {"success": True, "plan_id": plan_id, "corrupt": True}
    plan = json.loads(json.dumps(plan, default=str))  # detach a plain copy
    try:
        conn = _timeline_brain_db.connect(project_root)
        clip_id_cache: Dict[str, Any] = {}

        def _enrich(row: Dict[str, Any]) -> None:
            clip_uuid = str(row.get("clip_uuid") or "")
            if not row.get("resolve_clip_id") and clip_uuid:
                if clip_uuid not in clip_id_cache:
                    hit = conn.execute(
                        "SELECT resolve_clip_id FROM clips WHERE clip_uuid = ?",
                        (clip_uuid,),
                    ).fetchone()
                    clip_id_cache[clip_uuid] = hit["resolve_clip_id"] if hit else None
                if clip_id_cache[clip_uuid]:
                    row["resolve_clip_id"] = clip_id_cache[clip_uuid]
            shot_uuid = row.get("shot_uuid")
            if shot_uuid and row.get("thumb_frame_index") is None:
                hit = conn.execute(
                    "SELECT MIN(frame_index) AS frame_index FROM frames WHERE shot_uuid = ?",
                    (str(shot_uuid),),
                ).fetchone()
                if hit and hit["frame_index"] is not None:
                    row["thumb_frame_index"] = int(hit["frame_index"])

        for decision in plan.get("decisions") or []:
            if isinstance(decision, dict):
                _enrich(decision)
        for alternate in plan.get("alternates") or []:
            if isinstance(alternate, dict):
                _enrich(alternate)
    except Exception:  # noqa: BLE001 — thumbnails are progressive enhancement
        pass
    return {"success": True, "corrupt": False, "plan": plan}


def proxy_timeline_versioning_action(body: Dict[str, Any]) -> Dict[str, Any]:
    """Bridge dashboard → MCP server timeline_versioning tool.

    Body shape: {action, ...params}. Used for write actions (archive, rollback,
    prune) that need a live Resolve connection.
    """
    action = (body.get("action") or "").strip()
    if not action:
        return {"success": False, "error": "action required"}
    try:
        from src.server import timeline_versioning as _tv_tool
        params = {k: v for k, v in body.items() if k != "action"}
        return _tv_tool(action, params=params)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}



def _v2_enrich_search_results(project_root: str, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Augment /api/index/query results with fps, shot_index, and a thumbnail frame.

    These come from the clip's analysis.json on disk so the UI can render SMPTE
    timecode (HH:MM:SS:FF) and a clickable card that opens the deep-link review
    page for the matching shot.
    """
    if not results:
        return results
    analyses_by_clip: Dict[str, Optional[Dict[str, Any]]] = {}
    for row in results:
        clip_id = row.get("clip_id") or row.get("clip_key")
        if not clip_id:
            continue
        if clip_id not in analyses_by_clip:
            clip_dir = _v2_find_clip_dir(project_root, clip_id)
            analyses_by_clip[clip_id] = _v2_load_analysis(clip_dir) if clip_dir else None
        report = analyses_by_clip[clip_id]
        if not report:
            continue
        # fps from technical block
        technical = report.get("technical") if isinstance(report.get("technical"), dict) else {}
        videos = technical.get("video") if isinstance(technical.get("video"), list) else []
        fps = None
        if videos and isinstance(videos[0], dict):
            raw_fps = videos[0].get("frame_rate")
            try:
                fps = float(str(raw_fps).split("/")[0]) / (float(str(raw_fps).split("/")[1]) if "/" in str(raw_fps) else 1.0) if raw_fps else None
            except (TypeError, ValueError, ZeroDivisionError):
                fps = None
        if not fps:
            marker_plan = report.get("clip_analysis_markers") if isinstance(report.get("clip_analysis_markers"), dict) else {}
            try:
                fps = float(marker_plan.get("fps")) if marker_plan.get("fps") else None
            except (TypeError, ValueError):
                fps = None
        row["fps"] = fps
        # Resolve shot_index from start_seconds against shot_descriptions
        visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
        shots = visual.get("shot_descriptions") if isinstance(visual.get("shot_descriptions"), list) else []
        start_seconds = row.get("start_seconds")
        matched_shot: Optional[Dict[str, Any]] = None
        if start_seconds is not None:
            try:
                ts = float(start_seconds)
                for shot in shots:
                    if not isinstance(shot, dict):
                        continue
                    s = shot.get("time_seconds_start")
                    e = shot.get("time_seconds_end")
                    if s is None or e is None:
                        continue
                    if float(s) <= ts < float(e):
                        matched_shot = shot
                        break
            except (TypeError, ValueError):
                matched_shot = None
        if matched_shot is None and shots:
            # Clip-level result or no time anchor — point at the middle shot.
            matched_shot = shots[len(shots) // 2] if isinstance(shots[len(shots) // 2], dict) else None
        if matched_shot is not None:
            row["shot_index"] = matched_shot.get("shot_index")
            frame_indices = matched_shot.get("frame_indices_used") or matched_shot.get("frame_indices") or []
            if isinstance(frame_indices, list) and frame_indices:
                row["thumbnail_frame_index"] = frame_indices[0]
        if "thumbnail_frame_index" not in row:
            row["thumbnail_frame_index"] = _v2_pick_representative_frame_index(report)
    return results


def read_clip_corrections(project_root: str, clip_id: str) -> Dict[str, Any]:
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return {"success": False, "error": f"No analyzed clip found for id={clip_id}"}
    data = _v2_read_corrections_for_dir(clip_dir)
    return {
        "success": True,
        "clip_id": clip_id,
        "corrections_path": os.path.join(clip_dir, "corrections.json"),
        "current": data.get("current", {}),
        "changelog": data.get("changelog", []),
        "current_field_count": len(data.get("current", {})),
        "changelog_count": len(data.get("changelog", [])),
    }


