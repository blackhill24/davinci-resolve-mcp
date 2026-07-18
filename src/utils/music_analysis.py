"""Music-bed analysis helpers for auto_edit (ffmpeg-only; no librosa).

Phase 1 needs exactly one number from the music track: the gain that sits the
bed under dialogue at the checkpoint. Loudness measurement reuses the ebur128
path already proven in ``media_analysis`` (``_ffmpeg_stderr_filter`` +
``_parse_loudness``) so no new dependency is introduced — ffmpeg is the
package's existing peer dependency.

Onset/beat detection is a reserved seam only: Phase 3 (montage genre) fills it
in. Callers must treat ``detect_beats`` as honestly-unavailable until then.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from src.utils.media_analysis import _ffmpeg_stderr_filter, _parse_loudness, _run_command

# Broadcast-style dialogue programs sit near -23 LUFS; a music bed reads well
# roughly 7 dB under that. Both are overridable per call.
DEFAULT_DIALOGUE_TARGET_LUFS = -23.0
DEFAULT_BED_OFFSET_LU = -7.0
MAX_BED_GAIN_DB = 12.0  # never boost a quiet track past this
MIN_BED_GAIN_DB = -40.0


def measure_loudness(path: str) -> Dict[str, Any]:
    """EBU R128 loudness of an audio (or A/V) file via ffmpeg ebur128.

    Returns {"success", "metrics": {integrated_lufs, loudness_range_lu,
    true_peak_dbtp}} — metrics may hold None entries when ffmpeg emits no
    parseable summary (e.g. silent or corrupt input).
    """
    code, stderr = _ffmpeg_stderr_filter(path, audio_filter="ebur128=peak=true")
    metrics = _parse_loudness(stderr)
    if code != 0:
        return {"success": False, "error": "ffmpeg ebur128 pass failed", "metrics": metrics}
    return {"success": True, "metrics": metrics}


def bed_gain_db(
    integrated_lufs: Optional[float],
    *,
    dialogue_target_lufs: float = DEFAULT_DIALOGUE_TARGET_LUFS,
    bed_offset_lu: float = DEFAULT_BED_OFFSET_LU,
) -> Optional[float]:
    """Gain (dB) that moves a track from its measured loudness to bed level.

    Bed level = dialogue target + offset (offset is negative: under dialogue).
    Returns None when the measurement is unusable; the caller falls back to a
    conservative static level instead of guessing.
    """
    if not isinstance(integrated_lufs, (int, float)):
        return None
    target = float(dialogue_target_lufs) + float(bed_offset_lu)
    gain = target - float(integrated_lufs)
    return round(max(MIN_BED_GAIN_DB, min(MAX_BED_GAIN_DB, gain)), 2)


def analyze_music_bed(
    path: str,
    *,
    dialogue_target_lufs: float = DEFAULT_DIALOGUE_TARGET_LUFS,
    bed_offset_lu: float = DEFAULT_BED_OFFSET_LU,
) -> Dict[str, Any]:
    """Measure a music track and derive the bed gain for the checkpoint.

    ``gain_db`` is None when loudness could not be measured — the pipeline
    then uses a static conservative level rather than a derived one.
    """
    measured = measure_loudness(path)
    integrated = (measured.get("metrics") or {}).get("integrated_lufs")
    gain = bed_gain_db(
        integrated,
        dialogue_target_lufs=dialogue_target_lufs,
        bed_offset_lu=bed_offset_lu,
    )
    return {
        "success": measured["success"],
        "path": path,
        "metrics": measured.get("metrics") or {},
        "target_bed_lufs": round(dialogue_target_lufs + bed_offset_lu, 2),
        "gain_db": gain,
        **({"error": measured["error"]} if measured.get("error") else {}),
    }


DEFAULT_BED_FADE_SECONDS = 1.0


def render_ducked_bed(
    music_path: str,
    output_path: str,
    *,
    duration_seconds: float,
    gain_db: Optional[float] = None,
    fade_seconds: float = DEFAULT_BED_FADE_SECONDS,
    user_approved_render: bool = False,
) -> Dict[str, Any]:
    """Tier-1 ducked music bed: gain-staged, faded, trimmed — via ffmpeg.

    Produces DERIVATIVE media, so it is consent-gated: without
    ``user_approved_render`` (the ``approve_cut`` checkpoint consent) this
    refuses and the pipeline falls back to a static music level. The caller
    must point ``output_path`` under the analysis root, never beside sources.
    """
    if not user_approved_render:
        return {
            "success": False,
            "refused": True,
            "error": "Music-bed render was not approved at the checkpoint; "
                     "falling back to a static (non-ducked) music level.",
        }
    if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
        return {"success": False, "error": "duration_seconds must be positive"}
    duration = float(duration_seconds)
    fade = max(0.0, min(float(fade_seconds), duration / 2))
    filters = []
    if isinstance(gain_db, (int, float)) and gain_db:
        filters.append(f"volume={float(gain_db)}dB")
    if fade > 0:
        filters.append(f"afade=t=in:st=0:d={fade}")
        filters.append(f"afade=t=out:st={max(0.0, duration - fade)}:d={fade}")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    args = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", music_path]
    if filters:
        args.extend(["-af", ",".join(filters)])
    args.extend(["-t", f"{duration}", "-vn", output_path])
    code, _, stderr = _run_command(args)
    if code != 0 or not os.path.isfile(output_path):
        return {"success": False,
                "error": f"ffmpeg bed render failed (exit {code}): {stderr[-300:]}"}
    return {
        "success": True,
        "output_path": output_path,
        "duration_seconds": duration,
        "gain_db": gain_db,
        "fade_seconds": fade,
        "mode": "rendered_bed",
    }


def detect_beats(path: str) -> Dict[str, Any]:
    """Beat/onset detection seam — reserved for Phase 3 (montage genre).

    Honest refusal until implemented: montage-style cutting must not run on
    fabricated beat grids.
    """
    return {
        "success": False,
        "available": False,
        "path": path,
        "note": "Beat/onset detection is not implemented yet (reserved for the "
                "Phase-3 montage genre). No beat grid was produced.",
    }
