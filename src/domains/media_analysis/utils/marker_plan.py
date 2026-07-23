"""Shot/marker description synthesis from vision + transcript + cut analysis."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from src.domains.media_analysis.utils.caps_gating import ANALYSIS_VERSION, MARKER_PLAN_DEFAULT_COLORS
from src.domains.media_analysis.utils.technical_probe import _fraction_to_float, _media_duration_seconds, _parse_float


def _analysis_fps(record: Dict[str, Any], technical: Dict[str, Any]) -> float:
    raw = record.get("fps") or record.get("frame_rate") or record.get("frameRate")
    if raw not in (None, ""):
        if isinstance(raw, str):
            fraction = _fraction_to_float(raw)
            if fraction:
                return fraction
            match = re.search(r"\d+(?:\.\d+)?", raw)
            if match:
                parsed = _parse_float(match.group(0))
                if parsed:
                    return parsed
        parsed = _parse_float(raw)
        if parsed:
            return parsed
    summary = technical.get("summary") if isinstance(technical.get("summary"), dict) else {}
    for video in summary.get("video") or []:
        parsed = _parse_float(video.get("frame_rate"))
        if parsed:
            return parsed
    return 24.0


def _seconds_to_frame(seconds: Optional[float], fps: float) -> Optional[int]:
    if seconds is None:
        return None
    try:
        return int(round(max(0.0, float(seconds)) * max(float(fps), 1.0)))
    except (TypeError, ValueError):
        return None


def _duration_frames(start_seconds: Optional[float], end_seconds: Optional[float], fps: float, *, fallback: int = 1) -> int:
    if start_seconds is None or end_seconds is None:
        return fallback
    start_frame = _seconds_to_frame(start_seconds, fps)
    end_frame = _seconds_to_frame(end_seconds, fps)
    if start_frame is None or end_frame is None:
        return fallback
    return max(1, end_frame - start_frame)


def _time_seconds_from_text(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        for key in ("time_seconds", "timeSeconds", "start", "start_seconds", "startSeconds"):
            parsed = _parse_float(value.get(key))
            if parsed is not None:
                return parsed
        value = value.get("text") or value.get("note") or value.get("description")
    raw = str(value or "")
    colon = re.search(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})([.,]\d+)?\b", raw)
    if colon:
        hours = int(colon.group(1) or 0)
        minutes = int(colon.group(2))
        seconds = int(colon.group(3))
        fraction = float((colon.group(4) or "0").replace(",", "."))
        return hours * 3600 + minutes * 60 + seconds + fraction
    seconds_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)\b", raw, flags=re.IGNORECASE)
    if seconds_match:
        return _parse_float(seconds_match.group(1))
    return None


def _trim_text(value: Any, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _ranges_overlap(
    start_a: Optional[float],
    end_a: Optional[float],
    start_b: Optional[float],
    end_b: Optional[float],
) -> bool:
    if start_a is None:
        start_a = 0.0
    if start_b is None:
        start_b = 0.0
    if end_a is None:
        end_a = start_a
    if end_b is None:
        end_b = start_b
    return max(start_a, start_b) <= min(end_a, end_b)


def _transcript_words_from_payload(transcript: Dict[str, Any]) -> List[Dict[str, Any]]:
    words = transcript.get("words") if isinstance(transcript.get("words"), list) else []
    if words:
        return [word for word in words if isinstance(word, dict)]
    out: List[Dict[str, Any]] = []
    segments = transcript.get("segments") if isinstance(transcript.get("segments"), list) else []
    for segment in segments:
        if isinstance(segment, dict) and isinstance(segment.get("words"), list):
            out.extend(word for word in segment["words"] if isinstance(word, dict))
    return out


def _transcript_excerpt_for_range(transcript: Dict[str, Any], start: Optional[float], end: Optional[float]) -> str:
    words = _transcript_words_from_payload(transcript)
    if words:
        selected_words = []
        for word in words:
            if not isinstance(word, dict):
                continue
            word_start = _parse_float(word.get("start"))
            word_end = _parse_float(word.get("end"))
            if _ranges_overlap(start, end, word_start, word_end):
                selected_words.append(str(word.get("word") or "").strip())
        if selected_words:
            return _trim_text(" ".join(word for word in selected_words if word), 280)

    segments = transcript.get("segments") if isinstance(transcript.get("segments"), list) else []
    selected_segments = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        seg_start = _parse_float(segment.get("start"))
        seg_end = _parse_float(segment.get("end"))
        if _ranges_overlap(start, end, seg_start, seg_end):
            selected_segments.append(str(segment.get("text") or "").strip())
    return _trim_text(" ".join(text for text in selected_segments if text), 280)


_VISUAL_DESCRIPTION_UNAVAILABLE = "Visual description unavailable from this analysis pass."


def _shot_description_entry(
    vision: Dict[str, Any], shot_index: Optional[int], start: Optional[float], end: Optional[float]
) -> Optional[Dict[str, Any]]:
    """Return the matching shot_descriptions entry by index, or by time-range overlap."""
    rows = vision.get("shot_descriptions") if isinstance(vision.get("shot_descriptions"), list) else []
    if not rows:
        return None
    target_index = None
    try:
        if shot_index is not None:
            target_index = int(shot_index)
    except (TypeError, ValueError):
        target_index = None
    if target_index is not None:
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get("shot_index")) == target_index:
                    return row
            except (TypeError, ValueError):
                continue
    if start is None or end is None:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        r_start = _parse_float(row.get("time_seconds_start"))
        r_end = _parse_float(row.get("time_seconds_end"))
        if r_start is None or r_end is None:
            continue
        if abs(r_start - float(start)) <= 0.05 and abs(r_end - float(end)) <= 0.05:
            return row
    return None


def _keyframe_description_in_range(
    vision: Dict[str, Any], start: Optional[float], end: Optional[float]
) -> Optional[str]:
    """Return the first analysis_keyframe description whose time falls inside [start, end]."""
    if start is None or end is None:
        return None
    keyframes = vision.get("analysis_keyframes") if isinstance(vision.get("analysis_keyframes"), list) else []
    for keyframe in keyframes:
        if not isinstance(keyframe, dict):
            continue
        description = keyframe.get("description") or keyframe.get("visual_description")
        if not description:
            continue
        frame_time = _parse_float(keyframe.get("time_seconds"))
        if frame_time is None:
            continue
        if float(start) <= frame_time <= float(end):
            return description
    return None


def _visual_description_for_shot(
    vision: Dict[str, Any],
    shot_index: Optional[int],
    start: Optional[float],
    end: Optional[float],
) -> str:
    """Layered shot-description lookup.

    1. Exact match in vision.shot_descriptions (by shot_index, then by [start,end]).
    2. analysis_keyframe whose time falls inside [start, end].
    3. clip_summary as a clearly-marked fallback.
    4. Sentinel placeholder if nothing usable exists.
    """
    entry = _shot_description_entry(vision, shot_index, start, end)
    if entry:
        description = entry.get("description") or entry.get("visual_description")
        if description:
            return _trim_text(description, 360)
    in_range = _keyframe_description_in_range(vision, start, end)
    if in_range:
        return _trim_text(in_range, 360)
    summary = vision.get("clip_summary")
    if summary:
        return _trim_text(f"[shot description unavailable — falling back to clip summary] {summary}", 360)
    return _VISUAL_DESCRIPTION_UNAVAILABLE


def _visual_description_for_time(vision: Dict[str, Any], start: Optional[float], end: Optional[float]) -> str:
    """Used by point-in-time markers (best_moments, qc_warnings).

    Picks the nearest analysis_keyframe whose time is within roughly the marker's
    own range. Outside that window, falls back to clip_summary or the sentinel —
    never copies a far-away keyframe's description.
    """
    keyframes = vision.get("analysis_keyframes") if isinstance(vision.get("analysis_keyframes"), list) else []
    midpoint = None
    if start is not None and end is not None:
        midpoint = (float(start) + float(end)) / 2.0
    elif start is not None:
        midpoint = float(start)
    if midpoint is not None:
        in_range = _keyframe_description_in_range(vision, start, end)
        if in_range:
            return _trim_text(in_range, 360)
        best = None
        best_distance = None
        for keyframe in keyframes:
            if not isinstance(keyframe, dict):
                continue
            description = keyframe.get("description") or keyframe.get("visual_description")
            if not description:
                continue
            frame_time = _parse_float(keyframe.get("time_seconds"))
            if frame_time is None:
                continue
            distance = abs(frame_time - midpoint)
            if distance > 2.0:
                continue
            if best_distance is None or distance < best_distance:
                best = description
                best_distance = distance
        if best:
            return _trim_text(best, 360)
    if vision.get("clip_summary"):
        return _trim_text(vision.get("clip_summary"), 360)
    return _VISUAL_DESCRIPTION_UNAVAILABLE


def _shot_ranges_from_scenes(
    duration: Optional[float],
    scene_items: List[Dict[str, Any]],
    *,
    min_duration_seconds: float = 0.75,
) -> List[Dict[str, Any]]:
    scene_times = []
    for item in scene_items:
        if not isinstance(item, dict):
            continue
        t = _parse_float(item.get("time_seconds"))
        if t is None or t <= 0:
            continue
        if duration is not None and t >= duration:
            continue
        scene_times.append(t)
    scene_times = sorted(set(round(t, 3) for t in scene_times))

    if duration is not None and duration > 0:
        boundaries = [0.0]
        for t in scene_times:
            if t - boundaries[-1] >= min_duration_seconds:
                boundaries.append(t)
        if duration - boundaries[-1] >= 0.05:
            boundaries.append(float(duration))
        if len(boundaries) < 2:
            boundaries = [0.0, float(duration)]
        return [
            {"index": index + 1, "start": boundaries[index], "end": boundaries[index + 1]}
            for index in range(len(boundaries) - 1)
        ]

    if scene_times:
        starts = [0.0] + scene_times
        return [
            {"index": index + 1, "start": start, "end": starts[index + 1] if index + 1 < len(starts) else None}
            for index, start in enumerate(starts)
        ]
    return [{"index": 1, "start": 0.0, "end": duration}]


def _marker_sound_note(transcript: Dict[str, Any], readthrough: Dict[str, Any], start: Optional[float], end: Optional[float]) -> Tuple[str, str]:
    transcript_text = _transcript_excerpt_for_range(transcript, start, end)
    if transcript_text:
        return f"Transcript: {transcript_text}", transcript_text
    silence_items = ((readthrough.get("silence") or {}).get("items") or []) if isinstance(readthrough.get("silence"), dict) else []
    for item in silence_items:
        if isinstance(item, dict) and _ranges_overlap(start, end, _parse_float(item.get("start")), _parse_float(item.get("end"))):
            return "Sound: detected silence or very low-level audio in this range.", ""
    return "Sound: no transcript excerpt available for this range.", ""


def _build_marker_entry(
    *,
    marker_id: str,
    marker_type: str,
    color: str,
    name: str,
    start: Optional[float],
    end: Optional[float],
    fps: float,
    visual_description: str,
    sound_note: str,
    transcript_text: str = "",
    source: str,
    confidence: str = "computed",
    subtype: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "id": marker_id,
        "type": marker_type,
        "subtype": subtype,
        "color": color,
        "name": name,
        "start_seconds": start,
        "end_seconds": end,
        "start_frame": _seconds_to_frame(start, fps),
        "duration_frames": _duration_frames(start, end, fps),
        "visual_description": visual_description,
        "sound_note": sound_note,
        "transcript_text": transcript_text,
        "source": source,
        "confidence": confidence,
        "write_to_resolve": True,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _build_clip_marker_plan(
    record: Dict[str, Any],
    technical: Dict[str, Any],
    readthrough: Dict[str, Any],
    motion: Dict[str, Any],
    transcript: Dict[str, Any],
    vision: Dict[str, Any],
    *,
    options: Dict[str, Any],
    analysis_signature: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fps = _analysis_fps(record, technical)
    duration = _media_duration_seconds(record, technical)
    marker_options = options.get("marker_plan") if isinstance(options.get("marker_plan"), dict) else {}
    min_shot_duration = _parse_float(marker_options.get("min_shot_duration_seconds"))
    if min_shot_duration is None:
        min_shot_duration = 0.75
    color_scheme = {
        **MARKER_PLAN_DEFAULT_COLORS,
        **({
            str(key): str(value)
            for key, value in marker_options.get("colors", {}).items()
            if value not in (None, "")
        } if isinstance(marker_options.get("colors"), dict) else {}),
    }
    markers: List[Dict[str, Any]] = []
    untimed_notes: List[Dict[str, Any]] = []
    scene_items = ((readthrough.get("scenes") or {}).get("items") or []) if isinstance(readthrough.get("scenes"), dict) else []
    cut_analysis = readthrough.get("cut_analysis") if isinstance(readthrough.get("cut_analysis"), dict) else {}
    shot_ranges = cut_analysis.get("shot_ranges") if isinstance(cut_analysis.get("shot_ranges"), list) else None
    if not shot_ranges:
        shot_ranges = _shot_ranges_from_scenes(duration, scene_items, min_duration_seconds=float(min_shot_duration))
    for shot in shot_ranges:
        start = _parse_float(shot.get("start"))
        end = _parse_float(shot.get("end"))
        try:
            shot_index = int(shot.get("index"))
        except (TypeError, ValueError):
            shot_index = None
        sound_note, transcript_text = _marker_sound_note(transcript, readthrough, start, end)
        markers.append(_build_marker_entry(
            marker_id=f"shot-{int(shot['index']):03d}",
            marker_type="shot",
            color=color_scheme["shot"],
            name=f"Shot {int(shot['index']):03d}",
            start=start,
            end=end,
            fps=fps,
            visual_description=_visual_description_for_shot(vision, shot_index, start, end),
            sound_note=sound_note,
            transcript_text=transcript_text,
            source="scene_detection",
        ))

    flash_candidates = cut_analysis.get("flash_frame_candidates") if isinstance(cut_analysis.get("flash_frame_candidates"), list) else []
    for index, item in enumerate(flash_candidates, 1):
        if not isinstance(item, dict):
            continue
        start = _parse_float(item.get("start"))
        end = _parse_float(item.get("end"))
        sound_note, transcript_text = _marker_sound_note(transcript, readthrough, start, end)
        markers.append(_build_marker_entry(
            marker_id=f"flash-frame-candidate-{index:03d}",
            marker_type="qc_warning",
            subtype="flash_frame_candidate",
            color=color_scheme["qc_warning"],
            name="QC: Flash Frame Candidate",
            start=start,
            end=end,
            fps=fps,
            visual_description=(
                "FFmpeg detected a very short scene-bounded range. Review boundary frames to distinguish "
                "a flash frame, title/black insertion, or deliberate rapid cut from a high-motion moment."
            ),
            sound_note=sound_note,
            transcript_text=transcript_text,
            source="cut_boundary_analysis",
            confidence="computed_needs_visual_confirmation",
        ))

    black_items = ((readthrough.get("black_frames") or {}).get("items") or []) if isinstance(readthrough.get("black_frames"), dict) else []
    for index, item in enumerate(black_items, 1):
        if not isinstance(item, dict):
            continue
        start = _parse_float(item.get("start"))
        end = _parse_float(item.get("end"))
        sound_note, transcript_text = _marker_sound_note(transcript, readthrough, start, end)
        markers.append(_build_marker_entry(
            marker_id=f"black-or-title-{index:03d}",
            marker_type="qc_warning",
            subtype="black_or_title",
            color=color_scheme["black_or_title"],
            name="QC: Black/Very Dark Range",
            start=start,
            end=end,
            fps=fps,
            visual_description=(
                "Detected black or very dark picture. Review as true black, scanned tape black, "
                "dropout, or title fade before using as an edit point."
            ),
            sound_note=sound_note,
            transcript_text=transcript_text,
            source="blackdetect",
            confidence="computed",
        ))

    editing_notes = vision.get("editing_notes") if isinstance(vision.get("editing_notes"), dict) else {}
    for index, item in enumerate(editing_notes.get("best_moments") or [], 1):
        start = _time_seconds_from_text(item)
        if start is None:
            untimed_notes.append({"type": "best_moment", "note": _trim_text(item), "reason": "missing_time"})
            continue
        end = min(start + 1.0, duration) if duration else start + 1.0
        sound_note, transcript_text = _marker_sound_note(transcript, readthrough, start, end)
        markers.append(_build_marker_entry(
            marker_id=f"best-moment-{index:03d}",
            marker_type="best_moment",
            color=color_scheme["best_moment"],
            name="Best Moment",
            start=start,
            end=end,
            fps=fps,
            visual_description=_visual_description_for_time(vision, start, end),
            sound_note=sound_note or _trim_text(item),
            transcript_text=transcript_text,
            source="visual_editing_notes",
            confidence="model_suggested",
        ))

    qc_sources = list(technical.get("summary", {}).get("warnings") or []) + list(editing_notes.get("qc_flags") or [])
    for index, item in enumerate(qc_sources, 1):
        start = _time_seconds_from_text(item)
        if start is None:
            untimed_notes.append({"type": "qc_warning", "note": _trim_text(item), "reason": "missing_time"})
            continue
        end = min(start + 1.0, duration) if duration else start + 1.0
        sound_note, transcript_text = _marker_sound_note(transcript, readthrough, start, end)
        markers.append(_build_marker_entry(
            marker_id=f"qc-warning-{index:03d}",
            marker_type="qc_warning",
            color=color_scheme["qc_warning"],
            name="QC Warning",
            start=start,
            end=end,
            fps=fps,
            visual_description=_visual_description_for_time(vision, start, end),
            sound_note=sound_note,
            transcript_text=transcript_text,
            source="analysis_warning",
            confidence="model_suggested",
        ))

    markers.sort(key=lambda row: (float(row.get("start_seconds") or 0.0), row.get("type") or "", row.get("id") or ""))
    words = _transcript_words_from_payload(transcript)
    return {
        "success": True,
        "schema": "davinci_resolve_mcp.clip_analysis_markers.v1",
        "analysis_version": ANALYSIS_VERSION,
        "analysis_signature": analysis_signature or {},
        "clip": record,
        "fps": fps,
        "duration_seconds": duration,
        "color_scheme": color_scheme,
        "write_to_resolve_default": True,
        "resolve_marker_writeback": {
            "optional": True,
            "enabled": True,
            "default_behavior": (
                "Written during executed Resolve-target analysis and metadata publish unless "
                "timed_markers=no or dry_run=true."
            ),
            "write_action": "publish_clip_metadata",
            "disable_flags": {"timed_markers": "no", "dry_run": True, "publish_metadata": False},
        },
        "transcript_index": {
            "available": bool(transcript.get("text") or transcript.get("segments")),
            "segments": len(transcript.get("segments") or []),
            "word_timestamps": bool(words),
            "words": len(words),
        },
        "timeline_occurrences": record.get("timeline_occurrences") or [],
        "cut_analysis": {
            "cut_count": cut_analysis.get("cut_count", 0),
            "likely_edited_sequence": bool(cut_analysis.get("likely_edited_sequence")),
            "flash_frame_candidates": len(flash_candidates),
        },
        "marker_count": len(markers),
        "markers": markers,
        "untimed_notes": untimed_notes,
        "motion_summary": {
            "overall_motion_level": motion.get("overall_motion_level"),
            "average_frame_delta": motion.get("average_frame_delta"),
            "max_frame_delta": motion.get("max_frame_delta"),
        },
    }


