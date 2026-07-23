"""SQLite analysis-index status + FTS/LIKE query helpers."""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from src.domains.media_analysis.utils.analysis_index_build import _analysis_index_path, _index_text
from src.domains.media_analysis.utils.clip_identity_registry import normalize_path


def _sqlite_table_exists(db_path: str, table_name: str) -> bool:
    if not os.path.isfile(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1",
                (table_name,),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _analysis_index_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    counts = {}
    for table in (
        "clips",
        "technical_warnings",
        "markers",
        "transcript_segments",
        "visual_tags",
        "timeline_occurrences",
        "analysis_keyframes",
    ):
        try:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            counts[table] = 0
    return counts


def analysis_index_status(project_root: str, *, index_path: Optional[Any] = None) -> Dict[str, Any]:
    root = normalize_path(project_root)
    db_path, err = _analysis_index_path(root, index_path)
    if err or not db_path:
        return {"success": False, "error": err}
    if not os.path.isfile(db_path):
        return {
            "success": True,
            "exists": False,
            "project_root": root,
            "index_path": db_path,
            "hint": "Persisted analysis builds this automatically; run media_analysis(action='build_index') to rebuild from existing reports.",
        }
    conn = sqlite3.connect(db_path)
    try:
        metadata = {
            row[0]: row[1]
            for row in conn.execute("SELECT key, value FROM index_metadata")
        }
        counts = _analysis_index_counts(conn)
    finally:
        conn.close()
    return {
        "success": True,
        "exists": True,
        "project_root": root,
        "index_path": db_path,
        "schema_version": int(metadata.get("schema_version") or 0),
        "analysis_version": metadata.get("analysis_version"),
        "built_at": metadata.get("built_at"),
        "single_user": True,
        "image_blob_policy": metadata.get("image_blob_policy") or "excluded",
        "fts_enabled": metadata.get("fts_enabled") == "1",
        "counts": counts,
        "size_bytes": os.path.getsize(db_path),
    }


def _fts_query(value: Any) -> str:
    tokens = re.findall(r"[A-Za-z0-9_]+", str(value or ""))
    return " OR ".join(f'"{token}"' for token in tokens[:12])


def _row_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _normalize_index_result_types(result_types: Optional[Iterable[str]]) -> set:
    if result_types in (None, ""):
        return set()
    if isinstance(result_types, str):
        raw_items = [result_types]
    else:
        raw_items = list(result_types)
    allowed_values = {"clip", "marker", "transcript"}
    return {
        str(value).strip().lower()
        for value in raw_items
        if str(value).strip().lower() in allowed_values
    }


def _query_analysis_index_fts(conn: sqlite3.Connection, query: str, limit: int, result_types: Optional[Iterable[str]]) -> List[Dict[str, Any]]:
    fts = _fts_query(query)
    if not fts:
        return []
    allowed = _normalize_index_result_types(result_types)
    results: List[Dict[str, Any]] = []
    # FTS5 snippet() builds an excerpt around the match with marker tokens around
    # the matched terms. We use sentinel braces here (NOT HTML) and convert them
    # to <mark> tags on the client; this keeps the raw SQL output safe to pass
    # through escapeHtml on the way to the DOM.
    SNIP_START = "[[hi]]"
    SNIP_END = "[[/hi]]"
    SNIP_ELLIPSIS = "…"
    SNIP_TOKENS = 24
    if not allowed or "clip" in allowed:
        for row in conn.execute(
            f"""
            SELECT
                'clip' AS result_type,
                c.clip_key,
                c.clip_id,
                c.media_id,
                c.clip_name,
                c.file_path,
                c.summary,
                snippet(clips_fts, -1, '{SNIP_START}', '{SNIP_END}', '{SNIP_ELLIPSIS}', {SNIP_TOKENS}) AS snippet,
                c.report_path,
                NULL AS marker_type,
                NULL AS start_seconds,
                NULL AS end_seconds,
                bm25(clips_fts) AS rank
            FROM clips_fts
            JOIN clips c ON c.clip_key = clips_fts.clip_key
            WHERE clips_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts, limit),
        ):
            results.append(_row_dict(row))
    if not allowed or "marker" in allowed:
        for row in conn.execute(
            f"""
            SELECT
                'marker' AS result_type,
                c.clip_key,
                c.clip_id,
                c.media_id,
                c.clip_name,
                c.file_path,
                m.visual_description AS summary,
                snippet(markers_fts, -1, '{SNIP_START}', '{SNIP_END}', '{SNIP_ELLIPSIS}', {SNIP_TOKENS}) AS snippet,
                c.report_path,
                m.marker_type,
                m.start_seconds,
                m.end_seconds,
                bm25(markers_fts) AS rank
            FROM markers_fts
            JOIN markers m ON m.id = markers_fts.marker_rowid
            JOIN clips c ON c.clip_key = m.clip_key
            WHERE markers_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts, limit),
        ):
            results.append(_row_dict(row))
    if not allowed or "transcript" in allowed:
        for row in conn.execute(
            f"""
            SELECT
                'transcript' AS result_type,
                c.clip_key,
                c.clip_id,
                c.media_id,
                c.clip_name,
                c.file_path,
                s.text AS summary,
                snippet(transcripts_fts, -1, '{SNIP_START}', '{SNIP_END}', '{SNIP_ELLIPSIS}', {SNIP_TOKENS}) AS snippet,
                c.report_path,
                NULL AS marker_type,
                s.start_seconds,
                s.end_seconds,
                bm25(transcripts_fts) AS rank
            FROM transcripts_fts
            JOIN transcript_segments s ON s.id = transcripts_fts.segment_rowid
            JOIN clips c ON c.clip_key = s.clip_key
            WHERE transcripts_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts, limit),
        ):
            results.append(_row_dict(row))
    results.sort(key=lambda row: (float(row.get("rank") or 0.0), row.get("result_type") or ""))
    return results[:limit]


def _query_analysis_index_like(conn: sqlite3.Connection, query: str, limit: int, result_types: Optional[Iterable[str]]) -> List[Dict[str, Any]]:
    needle = f"%{str(query or '').lower()}%"
    allowed = _normalize_index_result_types(result_types)
    results: List[Dict[str, Any]] = []
    if not allowed or "clip" in allowed:
        for row in conn.execute(
            """
            SELECT
                'clip' AS result_type,
                clip_key, clip_id, media_id, clip_name, file_path, summary, report_path,
                NULL AS marker_type, NULL AS start_seconds, NULL AS end_seconds, 0.0 AS rank
            FROM clips
            WHERE lower(coalesce(clip_name, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(file_path, '')) LIKE ?
            LIMIT ?
            """,
            (needle, limit),
        ):
            results.append(_row_dict(row))
    if not allowed or "marker" in allowed:
        for row in conn.execute(
            """
            SELECT
                'marker' AS result_type,
                c.clip_key, c.clip_id, c.media_id, c.clip_name, c.file_path,
                m.visual_description AS summary, c.report_path, m.marker_type,
                m.start_seconds, m.end_seconds, 0.0 AS rank
            FROM markers m
            JOIN clips c ON c.clip_key = m.clip_key
            WHERE lower(
                coalesce(m.name, '') || ' ' || coalesce(m.visual_description, '') || ' ' ||
                coalesce(m.sound_note, '') || ' ' || coalesce(m.transcript_text, '')
            ) LIKE ?
            LIMIT ?
            """,
            (needle, limit),
        ):
            results.append(_row_dict(row))
    if not allowed or "transcript" in allowed:
        for row in conn.execute(
            """
            SELECT
                'transcript' AS result_type,
                c.clip_key, c.clip_id, c.media_id, c.clip_name, c.file_path,
                s.text AS summary, c.report_path, NULL AS marker_type,
                s.start_seconds, s.end_seconds, 0.0 AS rank
            FROM transcript_segments s
            JOIN clips c ON c.clip_key = s.clip_key
            WHERE lower(s.text) LIKE ?
            LIMIT ?
            """,
            (needle, limit),
        ):
            results.append(_row_dict(row))
    return results[:limit]


def query_analysis_index(
    project_root: str,
    query: Any,
    *,
    limit: Any = 20,
    result_types: Optional[Iterable[str]] = None,
    index_path: Optional[Any] = None,
) -> Dict[str, Any]:
    root = normalize_path(project_root)
    db_path, err = _analysis_index_path(root, index_path)
    if err or not db_path:
        return {"success": False, "error": err}
    if not os.path.isfile(db_path):
        return {"success": False, "error": f"Analysis index not found: {db_path}", "index_path": db_path}
    try:
        max_results = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        max_results = 20
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        has_fts = _sqlite_table_exists(db_path, "clips_fts")
        if _index_text(query):
            try:
                results = _query_analysis_index_fts(conn, str(query), max_results, result_types) if has_fts else []
            except sqlite3.Error:
                results = []
            if not results:
                results = _query_analysis_index_like(conn, str(query), max_results, result_types)
        else:
            results = [
                _row_dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        'clip' AS result_type,
                        clip_key, clip_id, media_id, clip_name, file_path, summary, report_path,
                        NULL AS marker_type, NULL AS start_seconds, NULL AS end_seconds, 0.0 AS rank
                    FROM clips
                    ORDER BY analyzed_at DESC, clip_name
                    LIMIT ?
                    """,
                    (max_results,),
                )
            ]
    finally:
        conn.close()
    return {
        "success": True,
        "project_root": root,
        "index_path": db_path,
        "query": query,
        "limit": max_results,
        "result_count": len(results),
        "results": results,
    }
