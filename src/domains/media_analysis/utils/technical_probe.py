"""ffprobe/ffmpeg technical metadata, scene/cut/loudness/interlace detection."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.core.proc import sanitized_spawn_env

from src.domains.media_analysis.utils.caps_gating import COMMAND_TIMEOUT_SECONDS


def _run_command(args: List[str], timeout: int = COMMAND_TIMEOUT_SECONDS) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=sanitized_spawn_env(),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        stderr_tail = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        return 124, stdout, f"Command timed out after {timeout}s. {stderr_tail}".strip()
    except OSError as exc:
        return 127, "", str(exc)
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    return proc.returncode, stdout, stderr


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _ingest_report_into_db(project_root: str, report: Dict[str, Any], clip_dir: Optional[str]) -> Dict[str, Any]:
    """C1 — write a report into the DB-canonical store (rows in a transaction).

    Best-effort by design: a DB failure must never break the analysis run,
    because the JSON export still lands on disk and every reader falls back
    to it. The failure is surfaced in the result for the caller to report.
    """
    try:
        from src.domains.media_analysis.utils import analysis_store

        return analysis_store.ingest_report(project_root, report, clip_dir=clip_dir)
    except Exception as exc:  # noqa: BLE001 — DB trouble must not kill analysis
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def _fraction_to_float(value: Any) -> Optional[float]:
    if value in (None, "", "0/0"):
        return None
    raw = str(value)
    if "/" in raw:
        num, den = raw.split("/", 1)
        try:
            den_f = float(den)
            if den_f == 0:
                return None
            return float(num) / den_f
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ffprobe(path: str) -> Dict[str, Any]:
    code, stdout, stderr = _run_command([
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        path,
    ])
    if code != 0:
        return {"success": False, "error": stderr.strip() or "ffprobe failed"}
    try:
        raw = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"ffprobe returned invalid JSON: {exc}"}
    return {"success": True, "raw": raw, "summary": _ffprobe_summary(raw)}


def _ffprobe_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    streams = raw.get("streams") or []
    fmt = raw.get("format") or {}
    video = []
    audio = []
    warnings = []
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            r_fps = _fraction_to_float(stream.get("r_frame_rate"))
            avg_fps = _fraction_to_float(stream.get("avg_frame_rate"))
            is_vfr = bool(r_fps and avg_fps and abs(r_fps - avg_fps) > 0.01)
            if is_vfr:
                warnings.append("Container frame rate and average frame rate differ; possible VFR media")
            video.append({
                "index": stream.get("index"),
                "codec": stream.get("codec_name"),
                "codec_long": stream.get("codec_long_name"),
                "profile": stream.get("profile"),
                "pixel_format": stream.get("pix_fmt"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "r_frame_rate": stream.get("r_frame_rate"),
                "avg_frame_rate": stream.get("avg_frame_rate"),
                "frame_rate": avg_fps or r_fps,
                "is_vfr": is_vfr,
                "color_primaries": stream.get("color_primaries"),
                "transfer_characteristics": stream.get("color_transfer"),
                "matrix_coefficients": stream.get("color_space"),
                "field_order": stream.get("field_order"),
                "duration_seconds": _parse_float(stream.get("duration")),
                "frame_count": int(stream["nb_frames"]) if str(stream.get("nb_frames", "")).isdigit() else None,
            })
        elif codec_type == "audio":
            audio.append({
                "index": stream.get("index"),
                "codec": stream.get("codec_name"),
                "codec_long": stream.get("codec_long_name"),
                "sample_rate": int(stream["sample_rate"]) if str(stream.get("sample_rate", "")).isdigit() else None,
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "duration_seconds": _parse_float(stream.get("duration")),
            })
    return {
        "format": {
            "filename": fmt.get("filename"),
            "format_name": fmt.get("format_name"),
            "duration_seconds": _parse_float(fmt.get("duration")),
            "size_bytes": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else None,
            "bit_rate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
            "tags": fmt.get("tags") or {},
        },
        "video": video,
        "audio": audio,
        "chapters": raw.get("chapters") or [],
        "warnings": warnings,
    }


def _media_duration_seconds(record: Dict[str, Any], technical: Dict[str, Any]) -> Optional[float]:
    summary = technical.get("summary") or {}
    duration = ((summary.get("format") or {}).get("duration_seconds"))
    if duration:
        return duration
    videos = summary.get("video") or []
    for video in videos:
        if video.get("duration_seconds"):
            return video["duration_seconds"]
    return None


def _ffmpeg_stderr_filter(path: str, video_filter: Optional[str] = None, audio_filter: Optional[str] = None, frames: Optional[int] = None) -> Tuple[int, str]:
    args = ["ffmpeg", "-hide_banner", "-nostats", "-i", path]
    if video_filter:
        args.extend(["-vf", video_filter])
    if audio_filter:
        args.extend(["-af", audio_filter])
    if frames is not None:
        args.extend(["-frames:v", str(frames)])
    args.extend(["-f", "null", "-"])
    code, _, stderr = _run_command(args)
    return code, stderr


def _parse_loudness(stderr: str) -> Dict[str, Any]:
    def latest(pattern: str) -> Optional[float]:
        matches = re.findall(pattern, stderr)
        if not matches:
            return None
        return _parse_float(matches[-1])

    return {
        "integrated_lufs": latest(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS"),
        "loudness_range_lu": latest(r"LRA:\s*(-?\d+(?:\.\d+)?)\s*LU"),
        "true_peak_dbtp": latest(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS"),
    }


def _parse_scene_changes(stderr: str) -> List[Dict[str, Any]]:
    scenes = []
    for match in re.finditer(r"pts_time:([0-9.]+)", stderr):
        t = _parse_float(match.group(1))
        if t is not None:
            scenes.append({"time_seconds": t})
    return scenes


def _parse_scene_score_pairs(stderr: str) -> List[Tuple[float, float]]:
    """Pair (pts_time, lavfi.scene_score) from showinfo + metadata=print output.

    The filtergraph ``select='gt(scene,0)',metadata=print:key=lavfi.scene_score,showinfo``
    emits, per qualifying frame, a showinfo line carrying ``pts_time:...`` followed by a
    metadata line carrying ``lavfi.scene_score=...``. Pair them in stream order.
    """
    pairs: List[Tuple[float, float]] = []
    current_time: Optional[float] = None
    for line in stderr.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            current_time = _parse_float(m.group(1))
            continue
        m = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if m and current_time is not None:
            score = _parse_float(m.group(1))
            if score is not None:
                pairs.append((current_time, score))
                current_time = None
    return pairs


def _adaptive_scene_threshold(
    scores: List[Tuple[float, float]],
    *,
    min_floor: float = 0.15,
    k_sd: float = 2.5,
    threshold_cap: float = 0.40,
    fallback: float = 0.30,
) -> Tuple[float, Dict[str, Any]]:
    """Pick a content-aware scene-change threshold from the score distribution.

    ``threshold = clamp(mean + k_sd*sd, [min_floor, threshold_cap])``. The floor protects
    low-motion footage (interview / locked-off) where ``mean+sd`` is tiny; the cap guards
    against a few extreme flashes inflating SD. Falls back to ``fallback`` (the legacy
    0.30) if the distribution is empty.
    """
    values = [s for _, s in scores if s is not None]
    if not values:
        return fallback, {"reason": "no_scores", "chosen": fallback, "source": "fallback"}

    n = len(values)
    mean = sum(values) / n
    if n > 1:
        var = sum((v - mean) * (v - mean) for v in values) / (n - 1)
        sd = math.sqrt(var)
    else:
        sd = 0.0

    candidate = mean + k_sd * sd
    chosen = max(min_floor, min(candidate, threshold_cap))

    sorted_vals = sorted(values)
    def _pctl(p: float) -> float:
        idx = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
        return sorted_vals[idx]

    return chosen, {
        "n": n,
        "mean": round(mean, 5),
        "sd": round(sd, 5),
        "p95": round(_pctl(95), 5),
        "p99": round(_pctl(99), 5),
        "candidate": round(candidate, 5),
        "min_floor": min_floor,
        "k_sd": k_sd,
        "threshold_cap": threshold_cap,
        "chosen": round(chosen, 5),
        "source": "adaptive",
    }


def _parse_blackdetect(stderr: str) -> List[Dict[str, Any]]:
    out = []
    pattern = r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)"
    for start, end, duration in re.findall(pattern, stderr):
        out.append({
            "start": _parse_float(start),
            "end": _parse_float(end),
            "duration": _parse_float(duration),
        })
    return out


def _parse_silencedetect(stderr: str) -> List[Dict[str, Any]]:
    starts = [_parse_float(v) for v in re.findall(r"silence_start:\s*([0-9.]+)", stderr)]
    ends = [(_parse_float(end), _parse_float(duration)) for end, duration in re.findall(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", stderr)]
    intervals = []
    for index, start in enumerate(starts):
        end = ends[index][0] if index < len(ends) else None
        duration = ends[index][1] if index < len(ends) else None
        intervals.append({"start": start, "end": end, "duration": duration})
    return intervals


def _parse_idet(stderr: str) -> Dict[str, Any]:
    match = re.search(
        r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)",
        stderr,
    )
    if not match:
        return {}
    tff, bff, progressive, undetermined = [int(v) for v in match.groups()]
    dominant = max(
        [("tff", tff), ("bff", bff), ("progressive", progressive), ("undetermined", undetermined)],
        key=lambda row: row[1],
    )[0]
    return {
        "tff": tff,
        "bff": bff,
        "progressive": progressive,
        "undetermined": undetermined,
        "dominant": dominant,
    }


def _readthrough_analysis(path: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"success": True}

    loud_code, loud_stderr = _ffmpeg_stderr_filter(path, audio_filter="ebur128=peak=true")
    result["loudness"] = {
        "success": loud_code == 0,
        "metrics": _parse_loudness(loud_stderr),
    }

    # Adaptive scene detection. One ffmpeg pass dumps a (pts_time, scene_score) pair
    # for every frame whose scene score is > 0 (i.e. every non-first frame); we then
    # compute a content-aware threshold from that distribution and keep peaks above it.
    # Replaces the legacy hardcoded ``gt(scene,0.3)``, which was too coarse on
    # high-motion content (missed real cuts) and too sensitive on locked-off content.
    scene_code, scene_stderr = _ffmpeg_stderr_filter(
        path,
        video_filter="select='gt(scene,0)',metadata=print:key=lavfi.scene_score,showinfo",
    )
    scene_score_pairs = _parse_scene_score_pairs(scene_stderr)
    scene_threshold, scene_threshold_stats = _adaptive_scene_threshold(scene_score_pairs)
    adaptive_scene_items = [
        {"time_seconds": pts, "score": score}
        for pts, score in scene_score_pairs
        if score is not None and score > scene_threshold
    ]
    result["scenes"] = {
        "success": scene_code == 0,
        "items": adaptive_scene_items,
        "threshold": scene_threshold,
        "threshold_stats": scene_threshold_stats,
    }

    black_code, black_stderr = _ffmpeg_stderr_filter(path, video_filter="blackdetect=d=0.5:pix_th=0.10")
    result["black_frames"] = {
        "success": black_code == 0,
        "items": _parse_blackdetect(black_stderr),
    }

    silence_code, silence_stderr = _ffmpeg_stderr_filter(path, audio_filter="silencedetect=noise=-50dB:d=1")
    result["silence"] = {
        "success": silence_code == 0,
        "items": _parse_silencedetect(silence_stderr),
    }

    idet_code, idet_stderr = _ffmpeg_stderr_filter(path, video_filter="idet", frames=500)
    result["interlace"] = {
        "success": idet_code == 0,
        "metrics": _parse_idet(idet_stderr),
    }

    return result


def _frame_number_for_time(seconds: Optional[float], fps: Optional[float]) -> Optional[int]:
    if seconds is None:
        return None
    try:
        return int(round(max(0.0, float(seconds)) * max(float(fps or 24.0), 1.0)))
    except (TypeError, ValueError):
        return None


def _frame_step_seconds(fps: Optional[float]) -> float:
    try:
        parsed = float(fps or 24.0)
    except (TypeError, ValueError):
        parsed = 24.0
    return 1.0 / max(parsed, 1.0)


def _clamp_sample_time(value: float, duration: Optional[float]) -> float:
    if duration is None or duration <= 0:
        return max(0.0, value)
    return min(max(0.0, value), max(0.0, duration - 0.001))


def _cut_boundary_analysis(
    duration: Optional[float],
    scene_items: List[Dict[str, Any]],
    fps: Optional[float],
    *,
    min_shot_duration_seconds: float = 0.75,
    flash_frame_max_duration_seconds: float = 0.25,
) -> Dict[str, Any]:
    frame_step = _frame_step_seconds(fps)
    scene_times = []
    for item in scene_items or []:
        if not isinstance(item, dict):
            continue
        t = _parse_float(item.get("time_seconds"))
        if t is None or t <= 0:
            continue
        if duration is not None and t >= duration:
            continue
        scene_times.append(round(t, 3))
    scene_times = sorted(set(scene_times))

    cut_points = []
    for index, t in enumerate(scene_times, 1):
        before_time = _clamp_sample_time(t - frame_step, duration)
        after_time = _clamp_sample_time(t + frame_step, duration)
        cut_points.append({
            "index": index,
            "time_seconds": t,
            "frame": _frame_number_for_time(t, fps),
            "before_time_seconds": before_time,
            "before_frame": _frame_number_for_time(before_time, fps),
            "after_time_seconds": after_time,
            "after_frame": _frame_number_for_time(after_time, fps),
            "needs_visual_confirmation": True,
            "source": "ffmpeg_scene_detection",
        })

    raw_shot_ranges = []
    boundaries: List[float] = [0.0]
    boundaries.extend(scene_times)
    if duration is not None and duration > 0:
        boundaries.append(float(duration))
    for index in range(max(0, len(boundaries) - 1)):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end <= start:
            continue
        raw_shot_ranges.append({
            "index": index + 1,
            "start": start,
            "end": end,
            "duration": end - start,
            "start_frame": _frame_number_for_time(start, fps),
            "end_frame": _frame_number_for_time(end, fps),
        })

    shot_ranges = []
    flash_candidates = []
    short_shot_candidates = []
    flash_keys = set()
    short_keys = set()
    for raw_shot in raw_shot_ranges:
        shot_duration = _parse_float(raw_shot.get("duration"))
        start = _parse_float(raw_shot.get("start"))
        end = _parse_float(raw_shot.get("end"))
        if shot_duration is not None and shot_duration <= float(min_shot_duration_seconds):
            short_keys.add((round(start or 0.0, 3), round(end or 0.0, 3)))
            short_shot_candidates.append(dict(raw_shot))
        if (
            shot_duration is not None
            and shot_duration <= float(flash_frame_max_duration_seconds)
            and start not in (None, 0.0)
            and end is not None
            and duration is not None
            and end < duration
        ):
            flash_keys.add((round(start, 3), round(end, 3)))
            flash_candidates.append({
                **raw_shot,
                "mid_sample_time_seconds": _clamp_sample_time(start + shot_duration / 2.0, duration),
                "reason": "adjacent scene detections bound a very short segment",
                "needs_visual_confirmation": True,
            })

    from src.domains.media_analysis.utils.marker_plan import _shot_ranges_from_scenes
    for shot in _shot_ranges_from_scenes(
        duration,
        [{"time_seconds": t} for t in scene_times],
        min_duration_seconds=float(min_shot_duration_seconds),
    ):
        start = _parse_float(shot.get("start"))
        end = _parse_float(shot.get("end"))
        shot_duration = (end - start) if start is not None and end is not None else None
        # 2*frame_step inset (~66ms at 30fps) keeps boundary samples clear of
        # cut-detector imprecision — a single-frame margin can land ON the cut.
        boundary_inset = frame_step * 2
        first_sample = _clamp_sample_time((start or 0.0) + boundary_inset, duration)
        if end is not None:
            last_sample = _clamp_sample_time(max(start or 0.0, end - boundary_inset), duration)
        else:
            last_sample = first_sample
        row = {
            "index": shot.get("index"),
            "start": start,
            "end": end,
            "duration": shot_duration,
            "start_frame": _frame_number_for_time(start, fps),
            "end_frame": _frame_number_for_time(end, fps),
            "first_sample_time_seconds": first_sample,
            "last_sample_time_seconds": last_sample,
            "first_sample_frame": _frame_number_for_time(first_sample, fps),
            "last_sample_frame": _frame_number_for_time(last_sample, fps),
        }
        shot_ranges.append(row)
        short_key = (round(start or 0.0, 3), round(end or 0.0, 3))
        if shot_duration is not None and shot_duration <= float(min_shot_duration_seconds) and short_key not in short_keys:
            short_keys.add(short_key)
            short_shot_candidates.append(row)
        if (
            shot_duration is not None
            and shot_duration <= float(flash_frame_max_duration_seconds)
            and start not in (None, 0.0)
            and end is not None
            and duration is not None
            and end < duration
            and (round(start, 3), round(end, 3)) not in flash_keys
        ):
            flash_candidates.append({
                **row,
                "mid_sample_time_seconds": _clamp_sample_time(start + shot_duration / 2.0, duration),
                "reason": "scene-bounded shot shorter than flash frame threshold",
                "needs_visual_confirmation": True,
            })

    cut_density_per_minute = (len(cut_points) / max(float(duration or 0.0), 1.0)) * 60.0 if duration else 0.0
    return {
        "success": True,
        "source": "ffmpeg_scene_detection",
        "threshold": 0.3,
        "fps": fps,
        "frame_step_seconds": frame_step,
        "duration_seconds": duration,
        "cut_count": len(cut_points),
        "cut_density_per_minute": cut_density_per_minute,
        "likely_edited_sequence": bool(len(cut_points) >= 2 or cut_density_per_minute >= 3.0),
        "cut_points": cut_points,
        "raw_shot_ranges": raw_shot_ranges,
        "shot_ranges": shot_ranges,
        "short_shot_candidates": short_shot_candidates,
        "flash_frame_candidates": flash_candidates,
        "notes": [
            "FFmpeg scene detection reads the full video stream; boundary frames are sampled for visual confirmation when available.",
            "Short scene-bounded ranges are candidates only until LLM/frame review distinguishes flash frames from deliberate cuts or high motion.",
        ],
    }


