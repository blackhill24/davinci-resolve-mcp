"""Demand-driven frame-sample-time selection, raw frame extraction, and motion scoring."""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional

from src.domains.media_analysis.utils.caps_gating import DEFAULT_FRAMES_PER_MINUTE, DEFAULT_FRAME_CEILING, DEFAULT_FRAME_FLOOR, DEFAULT_SAMPLING_MODE, HARD_FRAME_CAP
from src.domains.media_analysis.utils.clip_identity_registry import _clamp_int, normalize_sampling_mode
from src.domains.media_analysis.utils.technical_probe import _clamp_sample_time, _frame_step_seconds, _parse_float, _run_command


def _demand_frame_count(
    cut_analysis: Dict[str, Any],
    duration_seconds: Optional[float],
) -> int:
    """Frames the content *demands* so vision can populate the per-shot schema.

    Demand sources:
      - Per shot: 1 representative (midpoint) + 2 boundary frames + duration-scaled extras
        (+1 for shots >5s, +1 for shots >15s, +1 per additional 15s beyond 30s)
      - Per flash_candidate: 1 mid-frame for vision adjudication (preserve all)
      - Per cut_point: a small buffer for cuts not covered by shot boundaries
      - Clip-level: first_usable, last_usable, midpoint
    """
    per_shot_demand = 0
    for shot in cut_analysis.get("shot_ranges") or []:
        if not isinstance(shot, dict):
            continue
        start = _parse_float(shot.get("start"))
        end = _parse_float(shot.get("end"))
        if start is None or end is None or end <= start:
            continue
        d = end - start
        # Base: representative + 2 boundaries
        per_shot_demand += 3
        if d > 5.0:
            per_shot_demand += 1
        if d > 15.0:
            per_shot_demand += 1
        if d > 30.0:
            per_shot_demand += int((d - 30.0) / 15.0)

    flash_count = len(cut_analysis.get("flash_frame_candidates") or [])
    cut_count = len(cut_analysis.get("cut_points") or [])

    # Cut points mostly overlap with shot boundaries; add a small buffer
    cut_buffer = min(cut_count, 8)
    # Clip-level frames (first_usable, last_usable, midpoint)
    clip_buffer = 4

    return per_shot_demand + flash_count + cut_buffer + clip_buffer


def _compute_demand_driven_budget(
    requested_budget: int,
    cut_analysis: Optional[Dict[str, Any]],
    duration_seconds: Optional[float],
    sampling: Optional[Dict[str, Any]] = None,
) -> int:
    """Resolve the effective frame-sampling budget for the active sampling mode.

    Modes (see SAMPLING_MODES):
      - fixed:           flat `requested_budget`, duration-independent.
      - per_minute:      clamp(minutes * frames_per_minute, floor, ceiling); content-blind.
      - adaptive_capped: content demand (see _demand_frame_count), clamped to [floor, ceiling].
      - adaptive:        content demand, clamped only by a generous duration-scaled HARD_FRAME_CAP
                         (legacy behaviour; the default when no sampling config is threaded).

    `requested_budget` (depth-derived / max_analysis_frames) acts as a floor for the
    adaptive modes so an explicit request is never undercut.
    """
    sampling = sampling or {}
    mode = normalize_sampling_mode(sampling.get("mode"), default=DEFAULT_SAMPLING_MODE) or DEFAULT_SAMPLING_MODE
    rate = sampling.get("frames_per_minute") or DEFAULT_FRAMES_PER_MINUTE
    floor = int(sampling.get("frame_floor") or DEFAULT_FRAME_FLOOR)
    ceiling = int(sampling.get("frame_ceiling") or DEFAULT_FRAME_CEILING)
    if ceiling < floor:
        ceiling = floor
    requested = max(int(requested_budget or 0), 0)
    minutes = max(0.0, float(duration_seconds or 0) / 60.0)
    per_minute_count = int(round(minutes * float(rate)))

    if mode == "fixed":
        return min(requested, HARD_FRAME_CAP)

    if mode == "per_minute":
        return _clamp_int(per_minute_count, floor, min(ceiling, HARD_FRAME_CAP))

    # Adaptive modes need shot/cut analysis. Without it, fall back to a duration
    # estimate (adaptive_capped) or the legacy requested-only budget (adaptive).
    if not isinstance(cut_analysis, dict):
        if mode == "adaptive_capped":
            return _clamp_int(max(requested, per_minute_count), floor, min(ceiling, HARD_FRAME_CAP))
        return min(max(requested, 0), HARD_FRAME_CAP)

    demand = _demand_frame_count(cut_analysis, duration_seconds)
    target = max(requested, demand, floor)

    if mode == "adaptive_capped":
        return _clamp_int(target, floor, min(ceiling, HARD_FRAME_CAP))

    # adaptive (uncapped): only the absolute hard cap, scaled by duration so a
    # 10s clip cannot request 500 frames. Floor at 64 for short-clip headroom.
    duration_cap = max(64, min(HARD_FRAME_CAP, int(float(duration_seconds or 0) * 2)))
    return _clamp_int(target, floor, duration_cap)


def _even_interval_samples(
    duration: float,
    count: int,
    frame_step: float,
) -> List[Dict[str, Any]]:
    """Content-blind evenly-spaced samples (Economy / Balanced modes).

    Returns exactly `count` frames at the midpoints of `count` equal slices of
    [0, duration], so cost is a clean function of `count` and never inflated by
    shot/cut demand. Used when the user has chosen a predictable, content-blind mode.
    """
    if count <= 0 or duration <= 0:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(count):
        t = duration * (i + 0.5) / count
        out.append({
            "time_seconds": _clamp_sample_time(float(t), duration),
            "selection_reason": "interval",
        })
    return out


def _sample_times(
    duration: Optional[float],
    scene_items: List[Dict[str, Any]],
    budget: int,
    *,
    fps: Optional[float] = None,
    cut_analysis: Optional[Dict[str, Any]] = None,
    sampling: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Frame allocation. Content-blind for Economy/Balanced; demand-driven otherwise.

    Economy (fixed) / Balanced (per_minute): exactly `budget` evenly-spaced frames
    (see _even_interval_samples) — predictable cost, ignores shot structure.

    Thorough (adaptive / adaptive_capped) — two-pass demand-driven allocation:
      Pass 1 (reservations, always allocated — demand-driven, not budget-bounded):
        - Per shot: shot_representative (midpoint), shot_start, shot_end boundaries,
          duration-scaled progress samples (+1 for shots >5s, +1 for shots >15s,
          +1 per 15s beyond 30s).
        - Per flash_candidate: mid-frame for vision adjudication.
      Pass 2 (priority fill, consumes remaining budget):
        - cut_before/cut_after pairs (for cuts not covered by shot boundaries)
        - first_usable, last_usable, scene_change, midpoint, interval fillers

      The caller passes `budget` as the soft target. Reservations always land
      (demand-driven); priority fill is what `budget` constrains.

    Returns a time-sorted list of sample candidates.
    """
    if budget <= 0:
        return []
    duration = duration or 0
    cut_analysis = cut_analysis if isinstance(cut_analysis, dict) else {}
    frame_step = _frame_step_seconds(fps)

    # Content-blind modes: even-interval sampling of exactly `budget` frames so
    # cost stays predictable and is not inflated by per-shot reservations.
    mode = normalize_sampling_mode((sampling or {}).get("mode"), default=DEFAULT_SAMPLING_MODE) or DEFAULT_SAMPLING_MODE
    if mode in {"fixed", "per_minute"}:
        return _even_interval_samples(duration, budget, frame_step)

    # ===================== Pass 1: Reservations =====================
    reserved: List[Dict[str, Any]] = []

    def add_reserved(time_seconds: Optional[float], reason: str, **extra: Any) -> None:
        if time_seconds is None:
            return
        reserved.append({
            "time_seconds": _clamp_sample_time(float(time_seconds), duration),
            "selection_reason": reason,
            **extra,
        })

    # Per-shot reservations
    for shot in cut_analysis.get("shot_ranges") or []:
        if not isinstance(shot, dict):
            continue
        shot_index = shot.get("index")
        start = _parse_float(shot.get("start"))
        end = _parse_float(shot.get("end"))
        if start is None or end is None or end <= start:
            continue
        d = end - start
        common = {"shot_index": shot_index, "shot_start": start, "shot_end": end}
        # Always: mid-shot representative
        add_reserved((start + end) / 2.0, "shot_representative", **common)
        # Boundary frames if shot is long enough to distinguish them from the midpoint.
        # Use 2*frame_step inset (~66ms at 30fps) instead of 1*frame_step to clear
        # cut-detector imprecision — a single-frame margin can land ON the cut.
        boundary_inset = frame_step * 2
        if d >= boundary_inset * 4:
            add_reserved(
                shot.get("first_sample_time_seconds") or _clamp_sample_time(start + boundary_inset, duration),
                "shot_start",
                boundary_role="first_frame_in_shot",
                **common,
            )
            add_reserved(
                shot.get("last_sample_time_seconds") or _clamp_sample_time(end - boundary_inset, duration),
                "shot_end",
                boundary_role="last_frame_in_shot",
                **common,
            )
        # Duration-scaled progress samples
        if d > 5.0:
            add_reserved(start + d * (1.0 / 3.0), "shot_progress", **common)
        if d > 15.0:
            add_reserved(start + d * (2.0 / 3.0), "shot_progress", **common)
        if d > 30.0:
            extras = int((d - 30.0) / 15.0)
            for i in range(extras):
                frac = (i + 0.5) / max(extras, 1)
                add_reserved(start + 30.0 + (d - 30.0) * frac, "shot_progress", **common)

    # Per-flash-candidate reservations (preserved for vision adjudication)
    for flash in cut_analysis.get("flash_frame_candidates") or []:
        if not isinstance(flash, dict):
            continue
        add_reserved(
            flash.get("mid_sample_time_seconds"),
            "flash_candidate",
            shot_index=flash.get("index"),
            shot_start=flash.get("start"),
            shot_end=flash.get("end"),
        )

    # ===================== Pass 2: Priority fill =====================
    candidates: List[Dict[str, Any]] = []

    def add_candidate(time_seconds: Optional[float], reason: str, priority: int, **extra: Any) -> None:
        if time_seconds is None:
            return
        candidates.append({
            "time_seconds": _clamp_sample_time(float(time_seconds), duration),
            "selection_reason": reason,
            "priority": priority,
            **extra,
        })

    # Cut boundary pairs (for cuts not already covered by shot boundaries)
    for cut in cut_analysis.get("cut_points") or []:
        if not isinstance(cut, dict):
            continue
        cut_index = cut.get("index")
        add_candidate(
            cut.get("before_time_seconds"), "cut_before", 5,
            cut_index=cut_index, cut_time_seconds=cut.get("time_seconds"),
            boundary_role="last_frame_before_cut",
        )
        add_candidate(
            cut.get("after_time_seconds"), "cut_after", 5,
            cut_index=cut_index, cut_time_seconds=cut.get("time_seconds"),
            boundary_role="first_frame_after_cut",
        )

    # Clip-level usable frames
    if duration > 0:
        add_candidate(min(duration * 0.05, max(duration - 0.05, 0)), "first_usable", 6)
        add_candidate(max(duration - min(duration * 0.05, 0.5), 0), "last_usable", 6)
        add_candidate(duration * 0.5, "midpoint", 70)

    # Scene change candidates
    for scene in scene_items[: max(budget, 1)]:
        t = scene.get("time_seconds")
        if isinstance(t, (int, float)) and t >= 0:
            add_candidate(float(t), "scene_change", 15)

    # Interval filler (low priority)
    if duration > 0:
        interval_count = max(0, min(budget, 6) - 3)
        for index in range(interval_count):
            add_candidate(duration * ((index + 1) / (interval_count + 1)), "interval", 80)

    # ===================== Assemble: reservations first, then priority fill =====================
    unique: List[Dict[str, Any]] = []
    seen = set()

    def maybe_add(row: Dict[str, Any]) -> bool:
        rounded = round(max(float(row.get("time_seconds") or 0.0), 0), 3)
        key = round(rounded / max(frame_step, 0.001))
        if key in seen:
            return False
        seen.add(key)
        r = dict(row)
        r["time_seconds"] = rounded
        r.pop("priority", None)
        unique.append(r)
        return True

    # Reservations always land (demand-driven); budget bounds only priority fill.
    for r in sorted(reserved, key=lambda row: float(row.get("time_seconds") or 0.0)):
        maybe_add(r)

    # Effective budget for priority fill: max(budget, len(reservations))
    fill_budget = max(int(budget or 0), len(unique))
    for candidate in sorted(candidates, key=lambda row: (int(row.get("priority", 99)), float(row.get("time_seconds") or 0.0))):
        if len(unique) >= fill_budget:
            break
        maybe_add(candidate)

    return sorted(unique, key=lambda row: float(row.get("time_seconds") or 0.0))


def _raw_frame(path: str, time_seconds: float, width: int = 96, height: int = 54) -> Optional[bytes]:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{time_seconds:.3f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=rgb24",
        "-f",
        "rawvideo",
        "-",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=180, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    expected = width * height * 3
    if proc.returncode != 0 or len(proc.stdout) < expected:
        return None
    return proc.stdout[:expected]


def _frame_metrics(raw: bytes) -> Dict[str, Any]:
    count = max(1, len(raw) // 3)
    lum_sum = 0.0
    bins = [0] * 16
    r_sum = g_sum = b_sum = 0
    for idx in range(0, len(raw), 3):
        r, g, b = raw[idx], raw[idx + 1], raw[idx + 2]
        r_sum += r
        g_sum += g
        b_sum += b
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        lum_sum += lum
        bins[min(15, int(lum // 16))] += 1
    return {
        "mean_luma": lum_sum / count,
        "mean_rgb": [r_sum / count, g_sum / count, b_sum / count],
        "luma_histogram_16": bins,
    }


def _frame_delta(raw_a: Optional[bytes], raw_b: Optional[bytes]) -> Optional[float]:
    if not raw_a or not raw_b:
        return None
    total = 0
    n = min(len(raw_a), len(raw_b))
    for idx in range(n):
        total += abs(raw_a[idx] - raw_b[idx])
    return total / max(1, n) / 255.0


def _export_analysis_frame(path: str, time_seconds: float, output_path: str) -> bool:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    code, _, _ = _run_command([
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{time_seconds:.3f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-y",
        output_path,
    ], timeout=180)
    return code == 0 and os.path.isfile(output_path)


def _motion_and_keyframes(
    path: str,
    duration: Optional[float],
    scene_items: List[Dict[str, Any]],
    artifacts: Dict[str, Any],
    budget: int,
    *,
    fps: Optional[float] = None,
    cut_analysis: Optional[Dict[str, Any]] = None,
    write_frames: bool = True,
    sampling: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sampled = []
    previous_raw = None
    required_boundary_frames = 0
    if isinstance(cut_analysis, dict):
        required_boundary_frames += len(cut_analysis.get("cut_points") or []) * 2
        required_boundary_frames += len(cut_analysis.get("flash_frame_candidates") or [])
    effective_budget = _compute_demand_driven_budget(budget, cut_analysis, duration, sampling=sampling)
    times = _sample_times(duration, scene_items, effective_budget, fps=fps, cut_analysis=cut_analysis, sampling=sampling)
    frames_dir = artifacts.get("frames_dir")
    for index, sample in enumerate(times, 1):
        time_seconds = float(sample.get("time_seconds") or 0.0)
        raw = _raw_frame(path, time_seconds)
        if not raw:
            continue
        metrics = _frame_metrics(raw)
        delta = _frame_delta(previous_raw, raw)
        previous_raw = raw
        frame_path = None
        if write_frames and frames_dir:
            candidate = os.path.join(frames_dir, f"sampled_{index:04d}.jpg")
            if _export_analysis_frame(path, time_seconds, candidate):
                frame_path = candidate
        sampled_row = {
            "index": index,
            "time_seconds": time_seconds,
            "selection_reason": sample.get("selection_reason") or "interval",
            "frame_path": frame_path,
            "metrics": metrics,
            "delta_from_previous": delta,
        }
        for key in ("cut_index", "cut_time_seconds", "boundary_role", "shot_index", "shot_start", "shot_end", "motion_peak"):
            if sample.get(key) not in (None, ""):
                sampled_row[key] = sample.get(key)
        sampled.append(sampled_row)
    deltas = [row["delta_from_previous"] for row in sampled if row.get("delta_from_previous") is not None]
    avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
    max_delta = max(deltas) if deltas else 0.0
    if max_delta >= 0.08:
        for row in sampled:
            if row.get("delta_from_previous") == max_delta:
                row["motion_peak"] = True
                row["motion_peak_source_reason"] = row.get("selection_reason")
    if max_delta >= 0.2 or avg_delta >= 0.1:
        level = "high"
    elif max_delta >= 0.08 or avg_delta >= 0.035:
        level = "medium"
    else:
        level = "low"
    total_cut_points = len(cut_analysis.get("cut_points") or []) if isinstance(cut_analysis, dict) else 0
    cut_roles: Dict[Any, set] = {}
    for row in sampled:
        cut_index = row.get("cut_index")
        boundary_role = row.get("boundary_role")
        if cut_index in (None, "") or boundary_role in (None, ""):
            continue
        cut_roles.setdefault(cut_index, set()).add(boundary_role)
    paired_cut_boundaries = sum(
        1
        for roles in cut_roles.values()
        if {"last_frame_before_cut", "first_frame_after_cut"}.issubset(roles)
    )
    return {
        "success": True,
        "requested_sample_budget": int(budget or 0),
        "effective_sample_budget": effective_budget,
        "hard_frame_cap": HARD_FRAME_CAP,
        "cut_boundary_frames_requested": required_boundary_frames,
        "cut_boundary_sampling_capped": required_boundary_frames + 3 > HARD_FRAME_CAP,
        "cut_boundary_pairs_total": total_cut_points,
        "cut_boundary_pairs_sampled": paired_cut_boundaries,
        "cut_boundary_pair_coverage": paired_cut_boundaries / total_cut_points if total_cut_points else 1.0,
        "sample_count": len(sampled),
        "overall_motion_level": level,
        "average_frame_delta": avg_delta,
        "max_frame_delta": max_delta,
        "analysis_keyframes": sampled,
        "cut_analysis": cut_analysis or {},
    }


