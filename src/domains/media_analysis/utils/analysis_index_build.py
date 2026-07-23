"""SQLite analysis-index schema + build (ingests analysis reports into searchable rows)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.domains.media_analysis.utils.caps_gating import ANALYSIS_INDEX_FILENAME, ANALYSIS_INDEX_SCHEMA_VERSION, ANALYSIS_VERSION
from src.domains.media_analysis.utils.clip_identity_registry import _is_relative_to, normalize_path, stable_clip_directory, update_analysis_registry
from src.domains.media_analysis.utils.marker_plan import _analysis_fps
from src.domains.media_analysis.utils.technical_probe import _parse_float, _read_json


def _analysis_index_path(project_root: str, index_path: Optional[Any] = None) -> Tuple[Optional[str], Optional[str]]:
    root = normalize_path(project_root)
    candidate = normalize_path(index_path) if index_path else os.path.join(root, ANALYSIS_INDEX_FILENAME)
    if not _is_relative_to(candidate, root):
        return None, "index_path must be under the project analysis root"
    return candidate, None


def _iter_analysis_report_files(project_root: str) -> Iterable[str]:
    root = normalize_path(project_root)
    seen: set = set()
    clips_root = os.path.join(root, "clips")
    if not os.path.isdir(clips_root):
        clips_root = ""
    if clips_root:
        for dirpath, _, filenames in os.walk(clips_root):
            if "analysis.json" in filenames:
                path = os.path.join(dirpath, "analysis.json")
                real_path = os.path.realpath(path)
                seen.add(real_path)
                yield path

    db_path = os.path.join(root, "jobs.sqlite")
    if not os.path.isfile(db_path):
        return
    base_root = os.path.dirname(root)
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                """
                SELECT report_path, status
                FROM job_clips
                WHERE report_path IS NOT NULL
                  AND status IN ('succeeded', 'skipped', 'analyzed')
                ORDER BY updated_at DESC
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return
    for row in rows:
        report_path = str(row[0] or "")
        if not report_path:
            continue
        path = normalize_path(report_path)
        real_path = os.path.realpath(path)
        if real_path in seen:
            continue
        if os.path.basename(real_path) != "analysis.json" or not os.path.isfile(real_path):
            continue
        try:
            if os.path.commonpath([real_path, base_root]) != base_root:
                continue
        except ValueError:
            continue
        seen.add(real_path)
        yield path


def _index_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _index_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _index_as_list(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _first_video_summary(technical: Dict[str, Any]) -> Dict[str, Any]:
    videos = technical.get("video") if isinstance(technical.get("video"), list) else []
    return videos[0] if videos and isinstance(videos[0], dict) else {}


def _index_report_duration(report: Dict[str, Any]) -> Optional[float]:
    marker_plan = report.get("clip_analysis_markers") if isinstance(report.get("clip_analysis_markers"), dict) else {}
    duration = _parse_float(marker_plan.get("duration_seconds"))
    if duration is not None:
        return duration
    technical = report.get("technical") if isinstance(report.get("technical"), dict) else {}
    fmt = technical.get("format") if isinstance(technical.get("format"), dict) else {}
    duration = _parse_float(fmt.get("duration_seconds"))
    if duration is not None:
        return duration
    return _parse_float(_first_video_summary(technical).get("duration_seconds"))


def _index_report_fps(report: Dict[str, Any]) -> Optional[float]:
    marker_plan = report.get("clip_analysis_markers") if isinstance(report.get("clip_analysis_markers"), dict) else {}
    fps = _parse_float(marker_plan.get("fps"))
    if fps is not None:
        return fps
    clip = report.get("clip") if isinstance(report.get("clip"), dict) else {}
    technical = report.get("technical") if isinstance(report.get("technical"), dict) else {}
    return _parse_float(_analysis_fps(clip, {"summary": technical}))


def _index_visual_tags(report: Dict[str, Any]) -> List[Tuple[str, str]]:
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    tags: List[Tuple[str, str]] = []
    editing_notes = visual.get("editing_notes") if isinstance(visual.get("editing_notes"), dict) else {}
    for tag in _index_as_list(editing_notes.get("search_tags")):
        text = _index_text(tag)
        if text:
            tags.append((text, "visual.search_tags"))
    content = visual.get("content") if isinstance(visual.get("content"), dict) else {}
    for key in ("locations", "actions", "objects", "visible_text", "notable_audio_context"):
        for item in _index_as_list(content.get(key)):
            text = _index_text(item)
            if text:
                tags.append((text, f"visual.content.{key}"))
    slate = visual.get("slate") if isinstance(visual.get("slate"), dict) else {}
    for key in ("scene", "shot", "take", "camera", "roll", "production"):
        text = _index_text(slate.get(key))
        if text:
            tags.append((text, f"visual.slate.{key}"))
    for item in _index_as_list(slate.get("visible_text")):
        text = _index_text(item)
        if text:
            tags.append((text, "visual.slate.visible_text"))
    classification = visual.get("editorial_classification") if isinstance(visual.get("editorial_classification"), dict) else {}
    for key in ("primary_use", "select_potential", "energy_arc", "style"):
        text = _index_text(classification.get(key))
        if text:
            tags.append((text, f"visual.editorial_classification.{key}"))
    for item in _index_as_list(classification.get("genre_indicators")):
        text = _index_text(item)
        if text:
            tags.append((text, "visual.editorial_classification.genre_indicators"))
    shot_and_style = visual.get("shot_and_style") if isinstance(visual.get("shot_and_style"), dict) else {}
    for key in ("shot_sizes", "camera_motion"):
        for item in _index_as_list(shot_and_style.get(key)):
            text = _index_text(item)
            if text:
                tags.append((text, f"visual.shot_and_style.{key}"))
    for row in visual.get("shot_descriptions") or []:
        if not isinstance(row, dict):
            continue
        text = _index_text(row.get("description"))
        if text:
            tags.append((text, "visual.shot_descriptions"))
    seen = set()
    unique: List[Tuple[str, str]] = []
    for tag, source in tags:
        key = (tag.lower(), source)
        if key in seen:
            continue
        seen.add(key)
        unique.append((tag, source))
    return unique


def _index_editorial_corpus(report: Dict[str, Any]) -> str:
    """Concatenate every long-form editorial text field from the V2 visual layer
    into a single searchable string. Used to populate the FTS `summary` column
    so the Review page search box can find clips by their editorial content,
    not just by chips and slate metadata.
    """
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    parts: List[str] = []

    def push(value: Any) -> None:
        text = _index_text(value)
        if text:
            parts.append(text)

    push(visual.get("clip_summary"))
    push(visual.get("clip_summary_oneliner"))

    classification = visual.get("editorial_classification") if isinstance(visual.get("editorial_classification"), dict) else {}
    push(classification.get("reason"))
    for item in _index_as_list(classification.get("genre_indicators")):
        push(item)

    shot_and_style = visual.get("shot_and_style") if isinstance(visual.get("shot_and_style"), dict) else {}
    for key in ("composition_notes", "lighting_mood", "color_mood"):
        push(shot_and_style.get(key))

    cut_understanding = visual.get("cut_understanding") if isinstance(visual.get("cut_understanding"), dict) else {}
    for item in _index_as_list(cut_understanding.get("notes")):
        push(item)
    for item in _index_as_list(cut_understanding.get("flash_frame_candidates")):
        push(item)

    editing_notes = visual.get("editing_notes") if isinstance(visual.get("editing_notes"), dict) else {}
    for key in ("best_moments", "continuity_flags", "qc_flags"):
        for item in _index_as_list(editing_notes.get(key)):
            push(item)

    qc = visual.get("qc") if isinstance(visual.get("qc"), dict) else {}
    for key in ("warnings", "continuity_observations", "coverage_gaps"):
        for item in _index_as_list(qc.get(key)):
            push(item)

    motion = visual.get("motion") if isinstance(visual.get("motion"), dict) else {}
    for key in ("motion_events", "quiet_regions"):
        for item in _index_as_list(motion.get(key)):
            push(item)

    for row in visual.get("shot_descriptions") or []:
        if not isinstance(row, dict):
            continue
        push(row.get("description"))

    return " ".join(parts)


def _index_report_key(report_path: str, report: Dict[str, Any]) -> str:
    clip = report.get("clip") if isinstance(report.get("clip"), dict) else {}
    parent = os.path.basename(os.path.dirname(report_path))
    if parent and parent != "clips":
        return parent
    return stable_clip_directory(clip)


def _create_analysis_index_schema(conn: sqlite3.Connection) -> bool:
    conn.executescript(
        """
        CREATE TABLE index_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE clips (
            clip_key TEXT PRIMARY KEY,
            clip_id TEXT,
            media_id TEXT,
            clip_name TEXT,
            file_path TEXT,
            bin_path TEXT,
            media_type TEXT,
            duration_seconds REAL,
            fps REAL,
            summary TEXT,
            analyzed_at TEXT,
            report_path TEXT NOT NULL,
            marker_plan_path TEXT,
            technical_warning_count INTEGER NOT NULL DEFAULT 0,
            motion_level TEXT,
            transcript_available INTEGER NOT NULL DEFAULT 0,
            visual_available INTEGER NOT NULL DEFAULT 0,
            source_size_bytes INTEGER,
            source_mtime_ns INTEGER,
            signature_hash TEXT
        );

        CREATE INDEX idx_clips_file_path ON clips(file_path);
        CREATE INDEX idx_clips_clip_id ON clips(clip_id);
        CREATE INDEX idx_clips_motion_level ON clips(motion_level);

        CREATE TABLE technical_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_key TEXT NOT NULL,
            warning TEXT NOT NULL,
            FOREIGN KEY (clip_key) REFERENCES clips(clip_key) ON DELETE CASCADE
        );

        CREATE TABLE markers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_key TEXT NOT NULL,
            marker_id TEXT,
            marker_type TEXT,
            subtype TEXT,
            color TEXT,
            name TEXT,
            start_seconds REAL,
            end_seconds REAL,
            start_frame INTEGER,
            duration_frames INTEGER,
            visual_description TEXT,
            sound_note TEXT,
            transcript_text TEXT,
            source TEXT,
            confidence TEXT,
            FOREIGN KEY (clip_key) REFERENCES clips(clip_key) ON DELETE CASCADE
        );

        CREATE INDEX idx_markers_clip_key ON markers(clip_key);
        CREATE INDEX idx_markers_type ON markers(marker_type);
        CREATE INDEX idx_markers_start_seconds ON markers(start_seconds);

        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_key TEXT NOT NULL,
            segment_index INTEGER NOT NULL,
            start_seconds REAL,
            end_seconds REAL,
            text TEXT NOT NULL,
            FOREIGN KEY (clip_key) REFERENCES clips(clip_key) ON DELETE CASCADE
        );

        CREATE INDEX idx_transcript_segments_clip_key ON transcript_segments(clip_key);
        CREATE INDEX idx_transcript_segments_start_seconds ON transcript_segments(start_seconds);

        CREATE TABLE visual_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_key TEXT NOT NULL,
            tag TEXT NOT NULL,
            source TEXT,
            FOREIGN KEY (clip_key) REFERENCES clips(clip_key) ON DELETE CASCADE
        );

        CREATE INDEX idx_visual_tags_tag ON visual_tags(tag);
        CREATE INDEX idx_visual_tags_clip_key ON visual_tags(clip_key);

        CREATE TABLE timeline_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_key TEXT NOT NULL,
            timeline_id TEXT,
            timeline_name TEXT,
            track_type TEXT,
            track_index INTEGER,
            item_index INTEGER,
            start_frame INTEGER,
            end_frame INTEGER,
            record_frame INTEGER,
            occurrence_json TEXT NOT NULL,
            FOREIGN KEY (clip_key) REFERENCES clips(clip_key) ON DELETE CASCADE
        );

        CREATE INDEX idx_timeline_occurrences_clip_key ON timeline_occurrences(clip_key);
        CREATE INDEX idx_timeline_occurrences_timeline ON timeline_occurrences(timeline_id, timeline_name);

        CREATE TABLE analysis_keyframes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_key TEXT NOT NULL,
            keyframe_index INTEGER,
            time_seconds REAL,
            selection_reason TEXT,
            mean_luma REAL,
            delta_from_previous REAL,
            FOREIGN KEY (clip_key) REFERENCES clips(clip_key) ON DELETE CASCADE
        );

        CREATE INDEX idx_analysis_keyframes_clip_key ON analysis_keyframes(clip_key);
        CREATE INDEX idx_analysis_keyframes_time_seconds ON analysis_keyframes(time_seconds);
        """
    )
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE clips_fts USING fts5(
                clip_key UNINDEXED,
                clip_name,
                summary,
                file_path,
                tags,
                warnings
            );
            CREATE VIRTUAL TABLE markers_fts USING fts5(
                marker_rowid UNINDEXED,
                clip_key UNINDEXED,
                name,
                visual_description,
                sound_note,
                transcript_text
            );
            CREATE VIRTUAL TABLE transcripts_fts USING fts5(
                segment_rowid UNINDEXED,
                clip_key UNINDEXED,
                text
            );
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


def _insert_analysis_report_into_index(conn: sqlite3.Connection, report_path: str, report: Dict[str, Any], *, fts_enabled: bool) -> Dict[str, int]:
    clip = report.get("clip") if isinstance(report.get("clip"), dict) else {}
    technical = report.get("technical") if isinstance(report.get("technical"), dict) else {}
    motion = report.get("motion") if isinstance(report.get("motion"), dict) else {}
    transcription = report.get("transcription") if isinstance(report.get("transcription"), dict) else {}
    visual = report.get("visual") if isinstance(report.get("visual"), dict) else {}
    marker_plan = report.get("clip_analysis_markers") if isinstance(report.get("clip_analysis_markers"), dict) else {}
    signature = report.get("analysis_signature") if isinstance(report.get("analysis_signature"), dict) else {}
    source_signature = signature.get("source_file") if isinstance(signature.get("source_file"), dict) else {}

    clip_key = _index_report_key(report_path, report)
    source_file = report.get("source_file") or clip.get("file_path")
    marker_plan_path = os.path.join(os.path.dirname(report_path), "clip_analysis_markers.json")
    if not os.path.isfile(marker_plan_path):
        marker_plan_path = None

    warnings = [_index_text(item) for item in _index_as_list(report.get("technical_warnings")) if _index_text(item)]
    warnings.extend(
        _index_text(item)
        for item in _index_as_list(technical.get("warnings") if isinstance(technical, dict) else None)
        if _index_text(item)
    )
    warnings = list(dict.fromkeys(warnings))
    visual_tags = _index_visual_tags(report)
    # If the user has saved transcript corrections, index those instead of the
    # raw transcription. Keeps the search box in sync with edits.
    transcript_segments = transcription.get("segments") if isinstance(transcription.get("segments"), list) else []
    if report_path:
        corrections_path = os.path.join(os.path.dirname(report_path), "transcript-corrections.json")
        if os.path.isfile(corrections_path):
            try:
                with open(corrections_path, "r", encoding="utf-8") as handle:
                    corr = json.load(handle)
                if isinstance(corr, dict) and isinstance(corr.get("segments"), list):
                    transcript_segments = [s for s in corr["segments"] if isinstance(s, dict) and not s.get("deleted")]
            except Exception:
                pass
    transcript_text = _index_text(transcription.get("text"))
    transcript_available = bool(transcript_text or transcript_segments)
    visual_available = bool(
        visual.get("success")
        and (
            visual.get("clip_summary")
            or visual_tags
            or visual.get("analysis_keyframes")
            or visual.get("shot_descriptions")
        )
    )

    conn.execute(
        """
        INSERT INTO clips (
            clip_key, clip_id, media_id, clip_name, file_path, bin_path, media_type,
            duration_seconds, fps, summary, analyzed_at, report_path, marker_plan_path,
            technical_warning_count, motion_level, transcript_available, visual_available,
            source_size_bytes, source_mtime_ns, signature_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clip_key,
            clip.get("clip_id"),
            clip.get("media_id"),
            clip.get("clip_name") or (os.path.basename(str(source_file)) if source_file else None),
            source_file,
            clip.get("bin_path"),
            clip.get("media_type"),
            _index_report_duration(report),
            _index_report_fps(report),
            report.get("summary"),
            report.get("analyzed_at"),
            report_path,
            marker_plan_path,
            len(warnings),
            motion.get("overall_motion_level"),
            int(transcript_available),
            int(visual_available),
            source_signature.get("size_bytes"),
            source_signature.get("mtime_ns"),
            signature.get("signature_hash"),
        ),
    )

    for warning in warnings:
        conn.execute("INSERT INTO technical_warnings (clip_key, warning) VALUES (?, ?)", (clip_key, warning))

    for tag, source in visual_tags:
        conn.execute("INSERT INTO visual_tags (clip_key, tag, source) VALUES (?, ?, ?)", (clip_key, tag, source))

    marker_count = 0
    for marker in marker_plan.get("markers") or []:
        if not isinstance(marker, dict):
            continue
        cur = conn.execute(
            """
            INSERT INTO markers (
                clip_key, marker_id, marker_type, subtype, color, name, start_seconds,
                end_seconds, start_frame, duration_frames, visual_description, sound_note,
                transcript_text, source, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clip_key,
                marker.get("id"),
                marker.get("type"),
                marker.get("subtype"),
                marker.get("color"),
                marker.get("name"),
                _parse_float(marker.get("start_seconds")),
                _parse_float(marker.get("end_seconds")),
                marker.get("start_frame"),
                marker.get("duration_frames"),
                marker.get("visual_description"),
                marker.get("sound_note"),
                marker.get("transcript_text"),
                marker.get("source"),
                marker.get("confidence"),
            ),
        )
        marker_count += 1
        if fts_enabled:
            conn.execute(
                """
                INSERT INTO markers_fts (
                    marker_rowid, clip_key, name, visual_description, sound_note, transcript_text
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cur.lastrowid,
                    clip_key,
                    marker.get("name"),
                    marker.get("visual_description"),
                    marker.get("sound_note"),
                    marker.get("transcript_text"),
                ),
            )

    segment_count = 0
    if transcript_segments:
        for index, segment in enumerate(transcript_segments):
            if not isinstance(segment, dict):
                continue
            text = _index_text(segment.get("text"))
            if not text:
                continue
            cur = conn.execute(
                """
                INSERT INTO transcript_segments (
                    clip_key, segment_index, start_seconds, end_seconds, text
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    clip_key,
                    index,
                    _parse_float(segment.get("start")),
                    _parse_float(segment.get("end")),
                    text,
                ),
            )
            segment_count += 1
            if fts_enabled:
                conn.execute(
                    "INSERT INTO transcripts_fts (segment_rowid, clip_key, text) VALUES (?, ?, ?)",
                    (cur.lastrowid, clip_key, text),
                )
    elif transcript_text:
        cur = conn.execute(
            """
            INSERT INTO transcript_segments (
                clip_key, segment_index, start_seconds, end_seconds, text
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (clip_key, 0, None, None, transcript_text),
        )
        segment_count += 1
        if fts_enabled:
            conn.execute(
                "INSERT INTO transcripts_fts (segment_rowid, clip_key, text) VALUES (?, ?, ?)",
                (cur.lastrowid, clip_key, transcript_text),
            )

    occurrence_count = 0
    occurrences = marker_plan.get("timeline_occurrences") or clip.get("timeline_occurrences") or []
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        conn.execute(
            """
            INSERT INTO timeline_occurrences (
                clip_key, timeline_id, timeline_name, track_type, track_index,
                item_index, start_frame, end_frame, record_frame, occurrence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clip_key,
                occurrence.get("timeline_id") or occurrence.get("timelineId"),
                occurrence.get("timeline_name") or occurrence.get("timelineName"),
                occurrence.get("track_type") or occurrence.get("trackType"),
                occurrence.get("track_index") or occurrence.get("trackIndex"),
                occurrence.get("item_index") or occurrence.get("itemIndex"),
                occurrence.get("start_frame") or occurrence.get("startFrame"),
                occurrence.get("end_frame") or occurrence.get("endFrame"),
                occurrence.get("record_frame") or occurrence.get("recordFrame"),
                _index_json(occurrence),
            ),
        )
        occurrence_count += 1

    keyframe_count = 0
    for index, keyframe in enumerate(report.get("analysis_keyframes") or []):
        if not isinstance(keyframe, dict):
            continue
        metrics = keyframe.get("metrics") if isinstance(keyframe.get("metrics"), dict) else {}
        conn.execute(
            """
            INSERT INTO analysis_keyframes (
                clip_key, keyframe_index, time_seconds, selection_reason, mean_luma, delta_from_previous
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                clip_key,
                keyframe.get("index", index + 1),
                _parse_float(keyframe.get("time_seconds")),
                keyframe.get("selection_reason"),
                _parse_float(metrics.get("mean_luma")),
                _parse_float(keyframe.get("delta_from_previous")),
            ),
        )
        keyframe_count += 1

    if fts_enabled:
        editorial_corpus = _index_editorial_corpus(report)
        technical_summary = report.get("summary") or ""
        # Stuff both into the FTS `summary` column so the search box on the
        # Review page can find clips by any editorial text (summaries,
        # composition notes, qc observations, motion events, shot
        # descriptions) in addition to the technical pass.
        combined_summary = " ".join(part for part in (technical_summary, editorial_corpus) if part).strip() or None
        conn.execute(
            """
            INSERT INTO clips_fts (clip_key, clip_name, summary, file_path, tags, warnings)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                clip_key,
                clip.get("clip_name") or (os.path.basename(str(source_file)) if source_file else None),
                combined_summary,
                source_file,
                " ".join(tag for tag, _ in visual_tags),
                " ".join(warnings),
            ),
        )

    return {
        "warnings": len(warnings),
        "markers": marker_count,
        "transcript_segments": segment_count,
        "visual_tags": len(visual_tags),
        "timeline_occurrences": occurrence_count,
        "analysis_keyframes": keyframe_count,
    }


def build_analysis_index(project_root: str, *, index_path: Optional[Any] = None) -> Dict[str, Any]:
    """Build a single-user SQLite index derived from media analysis JSON reports."""
    from src.domains.media_analysis.utils.analysis_index_query import _sqlite_table_exists
    root = normalize_path(project_root)
    if not os.path.isdir(root):
        return {"success": False, "error": f"Project analysis root not found: {root}"}
    db_path, err = _analysis_index_path(root, index_path)
    if err or not db_path:
        return {"success": False, "error": err}

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    tmp_path = f"{db_path}.tmp"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(f"{tmp_path}{suffix}")
        except OSError:
            pass

    counts = {
        "clips": 0,
        "warnings": 0,
        "markers": 0,
        "transcript_segments": 0,
        "visual_tags": 0,
        "timeline_occurrences": 0,
        "analysis_keyframes": 0,
    }
    failed_reports: List[Dict[str, Any]] = []
    built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        fts_enabled = _create_analysis_index_schema(conn)
        conn.execute("INSERT INTO index_metadata (key, value) VALUES (?, ?)", ("schema_version", str(ANALYSIS_INDEX_SCHEMA_VERSION)))
        conn.execute("INSERT INTO index_metadata (key, value) VALUES (?, ?)", ("analysis_version", ANALYSIS_VERSION))
        conn.execute("INSERT INTO index_metadata (key, value) VALUES (?, ?)", ("built_at", built_at))
        conn.execute("INSERT INTO index_metadata (key, value) VALUES (?, ?)", ("fts_enabled", "1" if fts_enabled else "0"))
        conn.execute("INSERT INTO index_metadata (key, value) VALUES (?, ?)", ("image_blob_policy", "excluded"))

        # DB-first sourcing: local reports whose clip dir is ingested come from
        # the DB-canonical store (blob + human overlay — identical content to
        # the lockstep JSON export) instead of re-parsing every analysis.json.
        # Pre-v9 dirs and job-linked EXTERNAL report paths (their rows live
        # under another project's DB) keep the JSON read. The index schema and
        # the query surface are unchanged either way.
        clips_root_prefix = os.path.realpath(os.path.join(root, "clips")) + os.sep
        try:
            from src.core import timeline_brain_db as _brain_db

            _db_dirs = {
                str(r["clip_dir"]): str(r["clip_uuid"])
                for r in _brain_db.connect(root).execute(
                    "SELECT clip_dir, clip_uuid FROM clips WHERE clip_dir IS NOT NULL"
                ).fetchall()
            }
        except Exception:  # noqa: BLE001 — no DB (pre-v9) → JSON for everything
            _db_dirs = {}
        report_sources = {"db": 0, "json": 0}
        for report_path in sorted(_iter_analysis_report_files(root)):
            try:
                report = None
                if _db_dirs and os.path.realpath(report_path).startswith(clips_root_prefix):
                    clip_uuid = _db_dirs.get(os.path.basename(os.path.dirname(report_path)))
                    if clip_uuid:
                        try:
                            from src.domains.media_analysis.utils import analysis_store as _analysis_store

                            report = _analysis_store.export_report(root, clip_uuid)
                        except Exception:  # noqa: BLE001 — fall back per-report
                            report = None
                if isinstance(report, dict):
                    report_sources["db"] += 1
                else:
                    report = _read_json(report_path)
                    report_sources["json"] += 1
                row_counts = _insert_analysis_report_into_index(conn, report_path, report, fts_enabled=fts_enabled)
                counts["clips"] += 1
                for key, value in row_counts.items():
                    counts[key] += value
            except Exception as exc:  # pragma: no cover - defensive for arbitrary user reports
                failed_reports.append({"path": report_path, "error": str(exc)})
        for key, value in counts.items():
            conn.execute("INSERT INTO index_metadata (key, value) VALUES (?, ?)", (f"count.{key}", str(value)))
        conn.commit()
    finally:
        conn.close()

    for suffix in ("-wal", "-shm"):
        try:
            os.remove(f"{db_path}{suffix}")
        except OSError:
            pass
    os.replace(tmp_path, db_path)
    try:
        final_conn = sqlite3.connect(db_path)
        final_conn.execute("PRAGMA journal_mode=WAL")
        final_conn.close()
    except sqlite3.Error:
        pass
    try:
        registry_status = update_analysis_registry(root)
    except Exception as exc:  # pragma: no cover - registry is an auxiliary cache
        registry_status = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "success": True,
        "project_root": root,
        "index_path": db_path,
        "schema_version": ANALYSIS_INDEX_SCHEMA_VERSION,
        "built_at": built_at,
        "single_user": True,
        "image_blob_policy": "excluded",
        "fts_enabled": bool(counts["clips"]) and _sqlite_table_exists(db_path, "clips_fts"),
        "counts": counts,
        "report_sources": report_sources,  # how many reports came from the DB vs JSON
        "failed_report_count": len(failed_reports),
        "failed_reports": failed_reports[:50],
        "size_bytes": os.path.getsize(db_path) if os.path.isfile(db_path) else 0,
        "analysis_registry": registry_status,
    }


