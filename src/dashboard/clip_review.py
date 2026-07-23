"""V2 Review API: per-clip analysis, transcript, and correction endpoints."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from src.core import timeline_brain_db as _timeline_brain_db
from src.domains.media_analysis.utils.media_analysis import _write_json as _atomic_write_json


def _v2_iter_clip_dirs(project_root: str) -> List[Tuple[str, str]]:
    """Return (clip_slug, clip_dir_path) for each analysis.json under {project_root}/clips/."""
    clips_root = os.path.join(project_root, "clips")
    if not os.path.isdir(clips_root):
        return []
    rows: List[Tuple[str, str]] = []
    for entry in sorted(os.listdir(clips_root)):
        candidate = os.path.join(clips_root, entry)
        if not os.path.isdir(candidate):
            continue
        if not os.path.isfile(os.path.join(candidate, "analysis.json")):
            continue
        rows.append((entry, candidate))
    return rows


def _v2_iter_job_report_dirs(project_root: str) -> List[Tuple[str, str]]:
    """Return reusable analysis report dirs referenced by this project's jobs DB."""
    db_path = os.path.join(project_root, "jobs.sqlite")
    if not os.path.isfile(db_path):
        return []
    base_root = os.path.dirname(os.path.realpath(project_root))
    rows: List[Tuple[str, str]] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        job_rows = conn.execute(
            """
            SELECT clip_key, report_path, status
            FROM job_clips
            WHERE report_path IS NOT NULL
              AND status IN ('succeeded', 'skipped', 'analyzed')
            ORDER BY updated_at DESC
            """
        ).fetchall()
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    for row in job_rows:
        report_path = str(row["report_path"] or "")
        if not report_path:
            continue
        real_report = os.path.realpath(os.path.abspath(os.path.expanduser(report_path)))
        if os.path.basename(real_report) != "analysis.json" or not os.path.isfile(real_report):
            continue
        try:
            if os.path.commonpath([real_report, base_root]) != base_root:
                continue
        except ValueError:
            continue
        clip_dir = os.path.dirname(real_report)
        slug = str(row["clip_key"] or os.path.basename(clip_dir))
        rows.append((slug, clip_dir))
    return rows


def _v2_iter_analysis_dirs(project_root: str) -> List[Tuple[str, str]]:
    """Return local reports plus reusable report dirs linked from batch jobs."""
    rows: List[Tuple[str, str]] = []
    seen: set = set()
    for slug, clip_dir in _v2_iter_clip_dirs(project_root) + _v2_iter_job_report_dirs(project_root):
        real_dir = os.path.realpath(clip_dir)
        if real_dir in seen:
            continue
        seen.add(real_dir)
        rows.append((slug, clip_dir))
    return rows


def _v2_load_analysis(clip_dir: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(clip_dir, "analysis.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _v2_load_analysis_db_first(project_root: str, clip_dir: str) -> Optional[Dict[str, Any]]:
    """C1 — DB-canonical reader with JSON fallback.

    Falls back to analysis.json for reports that predate schema v9 and for
    job-linked report dirs whose rows live under another project's DB.
    """
    try:
        from src.domains.media_analysis.utils import analysis_store

        report = analysis_store.load_db_report(
            project_root, clip_dir=os.path.basename(clip_dir.rstrip("/\\"))
        )
        if report is not None:
            return report
    except Exception:
        pass
    return _v2_load_analysis(clip_dir)


def _v2_semantic_search(project_root: str, q: str, *, limit: int = 20) -> Dict[str, Any]:
    """Semantic search over the embeddings index, shaped like /api/index/query
    rows so the existing search-card renderer works unchanged."""
    text = (q or "").strip()
    if not text:
        return {"success": True, "results": []}
    try:
        from src.core import timeline_brain_db as tbd
        from src.dashboard.timeline_versions import _v2_enrich_search_results
        from src.domains.media_analysis.utils import embeddings

        found = embeddings.find_similar(project_root, text=text, kind="text", limit=limit)
        if not found.get("success"):
            return found
        conn = tbd.connect(project_root)
        resolve_ids: Dict[str, Optional[str]] = {}

        def resolve_clip_id(clip_uuid: Optional[str]) -> Optional[str]:
            if not clip_uuid:
                return None
            if clip_uuid not in resolve_ids:
                row = conn.execute(
                    "SELECT resolve_clip_id, clip_dir FROM clips WHERE clip_uuid = ?",
                    (clip_uuid,),
                ).fetchone()
                resolve_ids[clip_uuid] = (row["resolve_clip_id"] or row["clip_dir"]) if row else None
            return resolve_ids[clip_uuid]

        rows: List[Dict[str, Any]] = []
        for hit in found.get("results") or []:
            entity_type = hit.get("entity_type")
            clip_uuid = hit.get("clip_uuid") or (hit.get("entity_uuid") if entity_type == "clip" else None)
            row: Dict[str, Any] = {
                "result_type": "transcript" if entity_type == "segment" else "semantic",
                "score": hit.get("score"),
                "clip_id": resolve_clip_id(clip_uuid),
                "clip_name": hit.get("clip_name"),
            }
            if entity_type == "shot":
                row["start_seconds"] = hit.get("time_seconds_start")
                row["summary"] = hit.get("description")
            elif entity_type == "segment":
                row["start_seconds"] = hit.get("start_seconds")
                row["summary"] = hit.get("text")
            else:
                row["summary"] = hit.get("summary")
            rows.append(row)
        rows = _v2_enrich_search_results(project_root, rows)
        return {"success": True, "query": text, "model": found.get("model"), "results": rows}
    except Exception as exc:  # noqa: BLE001 — search must fail soft in the panel
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def _v2_clip_duration(report: Dict[str, Any]) -> Optional[float]:
    marker_plan = report.get("clip_analysis_markers") if isinstance(report.get("clip_analysis_markers"), dict) else {}
    duration = marker_plan.get("duration_seconds")
    if isinstance(duration, (int, float)):
        return float(duration)
    technical = report.get("technical") if isinstance(report.get("technical"), dict) else {}
    fmt = technical.get("format") if isinstance(technical.get("format"), dict) else {}
    if isinstance(fmt.get("duration_seconds"), (int, float)):
        return float(fmt["duration_seconds"])
    videos = technical.get("video") if isinstance(technical.get("video"), list) else []
    first = videos[0] if videos and isinstance(videos[0], dict) else {}
    if isinstance(first.get("duration_seconds"), (int, float)):
        return float(first["duration_seconds"])
    return None


def _v2_pick_representative_frame_index(report: Dict[str, Any]) -> Optional[int]:
    """Pick the frame index (1-based, matching sampled_NNNN.jpg) for a clip thumbnail.

    Strategy: middle shot's first frame_index_used, falling back to the middle
    analysis_keyframe, falling back to frame 1.
    """
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    shots = visual.get("shot_descriptions") if isinstance(visual.get("shot_descriptions"), list) else []
    if shots:
        mid_shot = shots[len(shots) // 2]
        if isinstance(mid_shot, dict):
            indices = mid_shot.get("frame_indices_used") or mid_shot.get("frame_indices")
            if isinstance(indices, list) and indices:
                first = indices[0]
                if isinstance(first, (int, float)):
                    return int(first)
    motion = report.get("motion") if isinstance(report.get("motion"), dict) else {}
    keyframes = motion.get("analysis_keyframes") if isinstance(motion.get("analysis_keyframes"), list) else []
    if keyframes:
        mid_kf = keyframes[len(keyframes) // 2]
        if isinstance(mid_kf, dict) and isinstance(mid_kf.get("index"), (int, float)):
            return int(mid_kf["index"])
    return 1


def _v2_clip_summary_card(clip_slug: str, clip_dir: str, report: Dict[str, Any]) -> Dict[str, Any]:
    clip_block = report.get("clip") if isinstance(report.get("clip"), dict) else {}
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    classification = visual.get("editorial_classification") if isinstance(visual.get("editorial_classification"), dict) else {}
    shots = visual.get("shot_descriptions") if isinstance(visual.get("shot_descriptions"), list) else []
    editing_notes = visual.get("editing_notes") if isinstance(visual.get("editing_notes"), dict) else {}
    summary = visual.get("clip_summary")
    if not isinstance(summary, str):
        summary = ""
    oneliner = visual.get("clip_summary_oneliner")
    if not isinstance(oneliner, str) or not oneliner:
        oneliner = summary[:140] + ("…" if len(summary) > 140 else "")
    rep_index = _v2_pick_representative_frame_index(report)
    return {
        "clip_id": clip_block.get("clip_id"),
        "clip_slug": clip_slug,
        "clip_dir": clip_dir,
        "clip_name": clip_block.get("clip_name") or clip_block.get("file_path") or clip_slug,
        "file_path": clip_block.get("file_path"),
        "bin_path": clip_block.get("bin_path"),
        "fps": clip_block.get("fps"),
        "duration_seconds": _v2_clip_duration(report),
        "shot_count": len(shots),
        "analyzed_at": report.get("analyzed_at"),
        "vision_committed_at": report.get("vision_committed_at"),
        "primary_use": classification.get("primary_use"),
        "select_potential": classification.get("select_potential"),
        "clip_summary": summary,
        "clip_summary_oneliner": oneliner,
        "search_tags": list(editing_notes.get("search_tags") or []) if isinstance(editing_notes.get("search_tags"), list) else [],
        "representative_frame_index": rep_index,
        "vision_status": report.get("vision_status") or visual.get("status"),
    }


def list_analyzed_clips(project_root: str) -> Dict[str, Any]:
    """List analyzed clips for the bin grid. One row per analysis.json found."""
    rows: List[Dict[str, Any]] = []
    for slug, clip_dir in _v2_iter_analysis_dirs(project_root):
        report = _v2_load_analysis_db_first(project_root, clip_dir)
        if report is None:
            continue
        rows.append(_v2_clip_summary_card(slug, clip_dir, report))
    rows.sort(key=lambda r: (r.get("clip_name") or "").lower())
    return {
        "success": True,
        "project_root": project_root,
        "clips": rows,
        "count": len(rows),
    }


def _v2_find_clip_dir(project_root: str, clip_id: str) -> Optional[str]:
    """Locate the clip directory for a given clip_id under {project_root}/clips/."""
    for slug, clip_dir in _v2_iter_analysis_dirs(project_root):
        if slug == clip_id:
            return clip_dir
        report = _v2_load_analysis(clip_dir)
        if not report:
            continue
        clip_block = report.get("clip") if isinstance(report.get("clip"), dict) else {}
        if str(clip_block.get("clip_id") or "") == clip_id:
            return clip_dir
    return None


def get_analyzed_clip(project_root: str, clip_id: str) -> Dict[str, Any]:
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return {"success": False, "error": f"No analyzed clip found for id={clip_id}"}
    report = _v2_load_analysis_db_first(project_root, clip_dir)
    if report is None:
        return {"success": False, "error": "analysis.json unreadable"}
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    shots = visual.get("shot_descriptions") if isinstance(visual.get("shot_descriptions"), list) else []
    corrections = _v2_read_corrections_for_dir(clip_dir)
    return {
        "success": True,
        "card": _v2_clip_summary_card(os.path.basename(clip_dir), clip_dir, report),
        "clip": report.get("clip") or {},
        "clip_summary": visual.get("clip_summary"),
        "clip_summary_oneliner": visual.get("clip_summary_oneliner"),
        "editorial_classification": visual.get("editorial_classification") or {},
        "shot_and_style": visual.get("shot_and_style") or {},
        "content": visual.get("content") or {},
        "slate": visual.get("slate") or {},
        "motion": visual.get("motion") or {},
        "cut_understanding": visual.get("cut_understanding") or {},
        "editing_notes": visual.get("editing_notes") or {},
        "cross_shot": visual.get("cross_shot") or {},
        "coverage_groups": visual.get("coverage_groups") or [],
        "continuity_chains": visual.get("continuity_chains") or [],
        "qc": visual.get("qc") or {},
        "confidence": visual.get("confidence") or {},
        "shots": shots,
        "shot_count": len(shots),
        "corrections": corrections,
        "analyzed_at": report.get("analyzed_at"),
        "vision_committed_at": report.get("vision_committed_at"),
    }


def get_analyzed_clip_shots(project_root: str, clip_id: str) -> Dict[str, Any]:
    """Lighter endpoint: just the shots array."""
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return {"success": False, "error": f"No analyzed clip found for id={clip_id}"}
    report = _v2_load_analysis_db_first(project_root, clip_dir)
    if report is None:
        return {"success": False, "error": "analysis.json unreadable"}
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    shots = visual.get("shot_descriptions") if isinstance(visual.get("shot_descriptions"), list) else []
    return {
        "success": True,
        "clip_id": clip_id,
        "shots": shots,
        "shot_count": len(shots),
    }


def get_analyzed_clip_shot(project_root: str, clip_id: str, shot_index: int) -> Dict[str, Any]:
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return {"success": False, "error": f"No analyzed clip found for id={clip_id}"}
    report = _v2_load_analysis_db_first(project_root, clip_dir)
    if report is None:
        return {"success": False, "error": "analysis.json unreadable"}
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    shots = visual.get("shot_descriptions") if isinstance(visual.get("shot_descriptions"), list) else []
    motion = report.get("motion") if isinstance(report.get("motion"), dict) else {}
    keyframes = motion.get("analysis_keyframes") if isinstance(motion.get("analysis_keyframes"), list) else []
    matched: Optional[Dict[str, Any]] = None
    for entry in shots:
        if isinstance(entry, dict):
            try:
                if int(entry.get("shot_index")) == int(shot_index):
                    matched = entry
                    break
            except (TypeError, ValueError):
                continue
    if matched is None:
        return {"success": False, "error": f"shot_index={shot_index} not found in clip {clip_id}"}
    frame_indices = matched.get("frame_indices_used") or matched.get("frame_indices") or []
    if not isinstance(frame_indices, list):
        frame_indices = []
    # Resolve each frame index back to its source keyframe (time_seconds, selection_reason, file path)
    kf_by_index: Dict[int, Dict[str, Any]] = {}
    for kf in keyframes:
        if not isinstance(kf, dict):
            continue
        try:
            kf_by_index[int(kf.get("index"))] = kf
        except (TypeError, ValueError):
            continue
    frame_rows: List[Dict[str, Any]] = []
    for raw_index in frame_indices:
        try:
            idx = int(raw_index)
        except (TypeError, ValueError):
            continue
        kf = kf_by_index.get(idx, {})
        frame_rows.append({
            "frame_index": idx,
            "time_seconds": kf.get("time_seconds"),
            "selection_reason": kf.get("selection_reason"),
            "delta_from_previous": kf.get("delta_from_previous"),
            "motion_peak": bool(kf.get("motion_peak")),
        })
    corrections = _v2_read_corrections_for_dir(clip_dir)
    shot_corrections = _v2_filter_corrections_for_shot(corrections, matched.get("shot_uuid"), shot_index)
    # Cross-shot relationships (spec §4) come from the DB, not the report —
    # fill the shot page's Relationships group when confirmed rows exist.
    # Exported reports don't carry shot_uuid, so derive it from the DB by
    # clip + shot_index.
    try:
        from src.domains.media_analysis.utils import analysis_store as _analysis_store
        from src.domains.media_analysis.utils import shot_relationships as _shot_rel
        conn = _timeline_brain_db.connect(project_root)
        shot_uuid = matched.get("shot_uuid")
        if not shot_uuid:
            clip_uuid = _analysis_store.resolve_clip_uuid(conn, clip_id)
            if clip_uuid:
                hit = conn.execute(
                    "SELECT shot_uuid FROM shots WHERE clip_uuid = ? AND shot_index = ?",
                    (clip_uuid, int(shot_index)),
                ).fetchone()
                shot_uuid = hit["shot_uuid"] if hit else None
        if shot_uuid:
            relationships = _shot_rel.relationships_for_shot(conn, str(shot_uuid))
            if relationships:
                matched = dict(matched)
                matched["relationships"] = relationships
    except Exception:  # noqa: BLE001 — panel reads fail soft
        pass
    return {
        "success": True,
        "clip_id": clip_id,
        "shot_index": shot_index,
        "shot": matched,
        "frames": frame_rows,
        "corrections": shot_corrections,
    }


def regenerate_clip_transcript(
    project_root: str,
    clip_id: str,
    *,
    with_words: bool = True,
    backend: Optional[str] = None,
    language: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-run transcription for a single clip, writing transcript.json (with
    word_timestamps when supported) and merging the result into analysis.json.
    Does not touch the rest of the analysis layers (visual, motion, etc.).
    """
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return {"success": False, "error": f"No analyzed clip found for id={clip_id}"}
    report = _v2_load_analysis(clip_dir)
    if report is None:
        return {"success": False, "error": "analysis.json unreadable"}
    source_file = report.get("source_file") or (report.get("clip") or {}).get("file_path")
    if not source_file or not os.path.isfile(str(source_file)):
        return {"success": False, "error": f"source file not found on disk: {source_file!r}"}
    try:
        from src.domains.media_analysis.utils.media_analysis import (
            _transcribe,
            detect_capabilities,
        )
    except Exception as exc:
        return {"success": False, "error": f"transcription helpers unavailable: {exc}"}
    artifacts = {
        "clip_dir": clip_dir,
        "analysis_json": os.path.join(clip_dir, "analysis.json"),
        "transcript_json": os.path.join(clip_dir, "transcript.json"),
        "transcript_srt": os.path.join(clip_dir, "transcript.srt"),
        "transcript_vtt": os.path.join(clip_dir, "transcript.vtt"),
    }
    capabilities = detect_capabilities()
    transcription_opts: Dict[str, Any] = {
        "enabled": True,
        "word_timestamps": bool(with_words),
        # Allow model download because this is a user-initiated re-transcribe;
        # without this flag _transcribe will skip with a guard error.
        "allow_model_download": True,
    }
    if backend:
        transcription_opts["backend"] = backend
    if language:
        transcription_opts["language"] = language
    if model:
        transcription_opts["model"] = model
    payload = _transcribe(source_file, artifacts, {"transcription": transcription_opts}, capabilities)
    if not payload.get("success"):
        return {"success": False, "error": payload.get("reason") or payload.get("error") or "transcription failed", "backend": payload.get("backend")}
    # Patch analysis.json so the in-memory snapshot stays in sync with the new
    # transcript.json artifact. We only touch the `transcription` block.
    try:
        with open(artifacts["analysis_json"], "r", encoding="utf-8") as handle:
            updated_report = json.load(handle)
    except Exception:
        updated_report = report
    updated_report["transcription"] = {
        "success": True,
        "backend": payload.get("backend"),
        "language": payload.get("language"),
        "text": payload.get("text"),
        "segments": payload.get("segments") or [],
    }
    if payload.get("words"):
        updated_report["transcription"]["words"] = payload["words"]
    try:
        _atomic_write_json(artifacts["analysis_json"], updated_report)
    except Exception as exc:
        return {"success": False, "error": f"transcript written but analysis.json patch failed: {exc}"}
    word_segment_count = sum(1 for seg in (payload.get("segments") or []) if isinstance(seg, dict) and seg.get("words"))
    return {
        "success": True,
        "clip_id": clip_id,
        "backend": payload.get("backend"),
        "language": payload.get("language"),
        "segment_count": len(payload.get("segments") or []),
        "word_segment_count": word_segment_count,
        "wrote_words": bool(payload.get("words") or word_segment_count),
    }


_TRANSCRIPT_CORRECTIONS_FILENAME = "transcript-corrections.json"


def _transcript_corrections_path(clip_dir: str) -> str:
    return os.path.join(clip_dir, _TRANSCRIPT_CORRECTIONS_FILENAME)


def _read_transcript_corrections(clip_dir: str) -> Optional[Dict[str, Any]]:
    path = _transcript_corrections_path(clip_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_transcript_segments(raw_segments: Any) -> List[Dict[str, Any]]:
    """Coerce a list of raw segment dicts (from transcript.json, analysis.json,
    or transcript-corrections.json) into the dashboard's canonical shape.

    Canonical: {index, start_seconds, end_seconds, text, words?: [...]}.
    """
    def _to_float(v: Any) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_segments, list):
        return out
    for index, seg in enumerate(raw_segments):
        if not isinstance(seg, dict):
            continue
        if seg.get("deleted"):
            continue
        start = seg.get("start_seconds") if seg.get("start_seconds") is not None else seg.get("start")
        end = seg.get("end_seconds") if seg.get("end_seconds") is not None else seg.get("end")
        text = seg.get("text")
        if text in (None, ""):
            text = seg.get("content")
        if text in (None, ""):
            continue
        normalized: Dict[str, Any] = {
            "index": int(seg.get("index", index)),
            "start_seconds": _to_float(start),
            "end_seconds": _to_float(end),
            "text": str(text).strip(),
        }
        words = seg.get("words")
        if isinstance(words, list) and words:
            word_rows: List[Dict[str, Any]] = []
            for w_index, w in enumerate(words):
                if not isinstance(w, dict):
                    continue
                w_text = w.get("word") if w.get("word") not in (None, "") else w.get("text")
                if w_text in (None, ""):
                    continue
                word_rows.append({
                    "index": int(w.get("index", w_index)),
                    "word": str(w_text),
                    "start_seconds": _to_float(w.get("start_seconds") if w.get("start_seconds") is not None else w.get("start")),
                    "end_seconds": _to_float(w.get("end_seconds") if w.get("end_seconds") is not None else w.get("end")),
                })
            if word_rows:
                normalized["words"] = word_rows
        out.append(normalized)
    return out


def get_analyzed_clip_transcript(project_root: str, clip_id: str) -> Dict[str, Any]:
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return {"success": False, "error": f"No analyzed clip found for id={clip_id}"}
    report = _v2_load_analysis(clip_dir)
    if report is None:
        return {"success": False, "error": "analysis.json unreadable"}
    transcription = report.get("transcription") if isinstance(report.get("transcription"), dict) else {}

    corrections = _read_transcript_corrections(clip_dir)
    corrected_segments: Optional[List[Dict[str, Any]]] = None
    edited_count = 0
    deleted_indices: List[int] = []
    if corrections and isinstance(corrections.get("segments"), list):
        corrected_segments = _normalize_transcript_segments(corrections["segments"])
        edited_count = int(corrections.get("edited_count") or 0)
        deleted_indices = corrections.get("deleted_indices") or []

    if corrected_segments is not None:
        segments = corrected_segments
    else:
        segments = _normalize_transcript_segments(transcription.get("segments"))

    clip_meta = report.get("clip") if isinstance(report.get("clip"), dict) else {}
    return {
        "success": True,
        "clip_id": clip_id,
        "clip_name": clip_meta.get("clip_name"),
        "backend": transcription.get("backend"),
        "language": transcription.get("language"),
        "text": transcription.get("text"),
        "segments": segments,
        "segment_count": len(segments),
        "available": bool(segments or transcription.get("text")),
        "has_corrections": corrections is not None,
        "corrections_meta": (corrections or {}).get("metadata") or {
            "edited_count": edited_count,
            "deleted_count": len(deleted_indices),
            "updated_at": (corrections or {}).get("updated_at"),
        } if corrections is not None else None,
    }


def save_clip_transcript_corrections(project_root: str, clip_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Write the shadow segments array to <clip-dir>/transcript-corrections.json.

    Expected body shape:
      {
        "segments": [ { "index": n, "start_seconds": s, "end_seconds": e,
                        "text": "...", "words": [...] }, ... ],
        "edited_count": N,
        "deleted_indices": [int, ...]
      }
    Or "clear": true to remove the corrections file entirely.
    """
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return {"success": False, "error": f"No analyzed clip found for id={clip_id}"}
    path = _transcript_corrections_path(clip_dir)
    if bool(body.get("clear")):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "cleared": True, "path": path}
    raw_segments = body.get("segments")
    if not isinstance(raw_segments, list):
        return {"success": False, "error": "body.segments must be a list"}
    normalized = _normalize_transcript_segments(raw_segments)
    payload = {
        "schema_version": "1.0",
        "updated_at": _now_iso(),
        "segments": normalized,
        "edited_count": int(body.get("edited_count") or 0),
        "deleted_indices": list(body.get("deleted_indices") or []),
        "metadata": {
            "edited_count": int(body.get("edited_count") or 0),
            "deleted_count": len(body.get("deleted_indices") or []),
            "updated_at": _now_iso(),
        },
    }
    try:
        _atomic_write_json(path, payload)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "path": path,
        "segment_count": len(normalized),
        "edited_count": payload["edited_count"],
        "deleted_count": len(payload["deleted_indices"]),
    }


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_clip_frame_path(project_root: str, clip_id: str, frame_index: int) -> Optional[str]:
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return None
    candidate = os.path.join(clip_dir, "frames", f"sampled_{int(frame_index):04d}.jpg")
    if os.path.isfile(candidate):
        return candidate
    return None


def _v2_read_corrections_for_dir(clip_dir: str) -> Dict[str, Any]:
    path = os.path.join(clip_dir, "corrections.json")
    if not os.path.isfile(path):
        return {"schema_version": "2.0", "current": {}, "changelog": []}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {"schema_version": "2.0", "current": {}, "changelog": []}
        data.setdefault("schema_version", "2.0")
        data.setdefault("current", {})
        data.setdefault("changelog", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"schema_version": "2.0", "current": {}, "changelog": []}


def _v2_filter_corrections_for_shot(
    corrections: Dict[str, Any], shot_uuid: Any, shot_index: Any
) -> Dict[str, Any]:
    current = corrections.get("current") if isinstance(corrections.get("current"), dict) else {}
    changelog = corrections.get("changelog") if isinstance(corrections.get("changelog"), list) else []
    keep_keys: set = set()
    if shot_uuid:
        keep_keys.add(str(shot_uuid))
    if shot_index is not None:
        keep_keys.add(str(shot_index))
    filtered_current = {
        key: entry
        for key, entry in current.items()
        if key.startswith("shot:") and key.split(":", 2)[1] in keep_keys
    }
    filtered_changelog = [
        row for row in changelog
        if row.get("entity_type") == "shot"
        and str(row.get("entity_uuid")) in keep_keys
    ]
    return {"current": filtered_current, "changelog": filtered_changelog}


def apply_clip_correction(project_root: str, clip_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Proxy POST /api/clips/<id>/corrections → media_analysis update_*_field helpers."""
    clip_dir = _v2_find_clip_dir(project_root, clip_id)
    if not clip_dir:
        return {"success": False, "error": f"No analyzed clip found for id={clip_id}"}
    from src.server import _v2_update_field
    entity_type = body.get("entity_type") or body.get("entityType") or "shot"
    params: Dict[str, Any] = dict(body)
    params["clip_id"] = clip_id
    params["clip_dir"] = clip_dir
    return _v2_update_field(project_root, params, entity_type=entity_type)


def export_clip_selection(project_root: str, clip_ids: List[str], fmt: str) -> Tuple[bytes, str, str]:
    """Build the export bytes for a selection. Returns (bytes, content_type, filename)."""
    fmt = (fmt or "json").strip().lower()
    payloads: List[Dict[str, Any]] = []
    for clip_id in clip_ids:
        data = get_analyzed_clip(project_root, clip_id)
        if data.get("success"):
            payloads.append(data)
    timestamp = _now_iso().replace(":", "").replace("-", "")[:13]
    if fmt == "csv":
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow([
            "clip_id", "clip_name", "bin_path", "duration_seconds", "shot_count",
            "primary_use", "select_potential", "energy_arc", "style",
            "search_tags", "qc_warnings", "clip_summary_oneliner", "clip_summary",
        ])
        for clip in payloads:
            card = clip.get("card") or {}
            classification = clip.get("editorial_classification") or {}
            editing_notes = clip.get("editing_notes") or {}
            qc = clip.get("qc") or {}
            writer.writerow([
                card.get("clip_id") or "",
                card.get("clip_name") or "",
                card.get("bin_path") or "",
                card.get("duration_seconds") if card.get("duration_seconds") is not None else "",
                clip.get("shot_count") if clip.get("shot_count") is not None else "",
                classification.get("primary_use") or "",
                classification.get("select_potential") or "",
                classification.get("energy_arc") or "",
                classification.get("style") or "",
                "|".join(str(t) for t in (editing_notes.get("search_tags") or [])),
                "|".join(str(w) for w in (qc.get("warnings") or [])),
                clip.get("clip_summary_oneliner") or "",
                clip.get("clip_summary") or "",
            ])
        return buf.getvalue().encode("utf-8"), "text/csv; charset=utf-8", f"selection-{timestamp}.csv"
    # JSON: full payload array
    text = json.dumps({"clip_count": len(payloads), "clips": payloads}, indent=2)
    return text.encode("utf-8"), "application/json", f"selection-{timestamp}.json"


def combined_clip_analysis(project_root: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Synthesize a multi-clip review payload. Body: {clip_ids: [str, ...]}.

    Returns a unified payload with: clip_count, sources (per-clip card),
    clip_summaries[] (one per clip), editorial_classification (union),
    shot_and_style (per-clip), shots[] (all shots from all clips, source-tagged
    and time-offset by accumulated clip duration so they read like one strip),
    transcript (concatenated segments with the same time offset), tags (union),
    qc (union).
    """
    clip_ids = body.get("clip_ids")
    if not isinstance(clip_ids, list) or not clip_ids:
        return {"success": False, "error": "clip_ids must be a non-empty list"}
    sources: List[Dict[str, Any]] = []
    shot_summaries: List[Dict[str, Any]] = []
    transcript_segments: List[Dict[str, Any]] = []
    clip_summaries: List[Dict[str, Any]] = []
    union_tags: List[str] = []
    union_qc: List[str] = []
    classifications: List[Dict[str, Any]] = []
    shot_and_style_blocks: List[Dict[str, Any]] = []
    cursor = 0.0
    total_duration = 0.0
    for clip_id in clip_ids:
        data = get_analyzed_clip(project_root, clip_id)
        if not data.get("success"):
            continue
        card = data.get("card") or {}
        duration = float(card.get("duration_seconds") or 0.0)
        sources.append({
            "clip_id": card.get("clip_id") or clip_id,
            "clip_name": card.get("clip_name"),
            "bin_path": card.get("bin_path"),
            "duration_seconds": duration,
            "shot_count": data.get("shot_count"),
            "thumbnail_frame_index": card.get("thumbnail_frame_index"),
            "offset_seconds": cursor,
        })
        if data.get("clip_summary"):
            clip_summaries.append({
                "clip_id": card.get("clip_id") or clip_id,
                "clip_name": card.get("clip_name"),
                "oneliner": data.get("clip_summary_oneliner"),
                "summary": data.get("clip_summary"),
            })
        if isinstance(data.get("editorial_classification"), dict):
            classifications.append({"clip_name": card.get("clip_name"), **data["editorial_classification"]})
        if isinstance(data.get("shot_and_style"), dict):
            shot_and_style_blocks.append({"clip_name": card.get("clip_name"), **data["shot_and_style"]})
        for shot in (data.get("shots") or []):
            if not isinstance(shot, dict):
                continue
            shot_summaries.append({
                "source_clip_id": card.get("clip_id") or clip_id,
                "source_clip_name": card.get("clip_name"),
                "source_offset_seconds": cursor,
                "shot_index": shot.get("shot_index"),
                "time_seconds_start": (float(shot.get("time_seconds_start") or 0.0) + cursor) if shot.get("time_seconds_start") is not None else None,
                "time_seconds_end": (float(shot.get("time_seconds_end") or 0.0) + cursor) if shot.get("time_seconds_end") is not None else None,
                "frame_indices_used": shot.get("frame_indices_used") or [],
                "description": shot.get("description"),
                "qc_flags": shot.get("qc_flags") or [],
            })
        # Transcript merge
        clip_dir = _v2_find_clip_dir(project_root, clip_id)
        if clip_dir:
            t = get_analyzed_clip_transcript(project_root, clip_id)
            if t.get("success"):
                for seg in (t.get("segments") or []):
                    if not isinstance(seg, dict):
                        continue
                    transcript_segments.append({
                        "source_clip_id": card.get("clip_id") or clip_id,
                        "source_clip_name": card.get("clip_name"),
                        "source_offset_seconds": cursor,
                        "start_seconds": (float(seg.get("start_seconds") or 0.0) + cursor) if seg.get("start_seconds") is not None else None,
                        "end_seconds": (float(seg.get("end_seconds") or 0.0) + cursor) if seg.get("end_seconds") is not None else None,
                        "text": seg.get("text"),
                    })
        for tag in ((data.get("editing_notes") or {}).get("search_tags") or []):
            text = str(tag).strip()
            if text and text not in union_tags:
                union_tags.append(text)
        for warn in ((data.get("qc") or {}).get("warnings") or []):
            text = str(warn).strip()
            if text and text not in union_qc:
                union_qc.append(text)
        cursor += duration
        total_duration += duration
    if not sources:
        return {"success": False, "error": "none of the requested clip_ids resolved to an analyzed clip"}
    return {
        "success": True,
        "clip_count": len(sources),
        "total_duration_seconds": total_duration,
        "sources": sources,
        "clip_summaries": clip_summaries,
        "editorial_classifications": classifications,
        "shot_and_style_blocks": shot_and_style_blocks,
        "shots": shot_summaries,
        "transcript_segments": transcript_segments,
        "search_tags": union_tags,
        "qc_warnings": union_qc,
    }


