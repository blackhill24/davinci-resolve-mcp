"""Music-bed analysis helpers for auto_edit (ffmpeg-only; no librosa).

Phase 1 needs exactly one number from the music track: the gain that sits the
bed under dialogue at the checkpoint. Loudness measurement reuses the ebur128
path already proven in ``media_analysis`` (``_ffmpeg_stderr_filter`` +
``_parse_loudness``) so no new dependency is introduced — ffmpeg is the
package's existing peer dependency.

Onset/beat detection (Phase 3, montage genre) is implemented ffmpeg-only: ffmpeg
decodes the track to mono PCM and a small time-domain energy-novelty picker finds
onsets and estimates tempo in pure Python — still no librosa/numpy.
"""
from __future__ import annotations

import array
import math
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Tuple

from src.domains.media_analysis.utils.media_analysis import _ffmpeg_stderr_filter, _parse_loudness, _run_command
from src.core.proc import safe_run

# Broadcast-style dialogue programs sit near -23 LUFS; a music bed reads well
# roughly 7 dB under that. Both are overridable per call.
DEFAULT_DIALOGUE_TARGET_LUFS = -23.0
DEFAULT_BED_OFFSET_LU = -7.0
MAX_BED_GAIN_DB = 12.0  # never boost a quiet track past this
MIN_BED_GAIN_DB = -40.0

# Ducking mode vocabulary lives with the CutList schema (cut_ir) so the
# validator and the mode strings can never drift; re-exported here for callers.
from src.domains.auto_edit.utils.cut_ir import (  # noqa: F401  (re-export)
    DUCKING_STATIC,
    DUCKING_RENDERED_BED,
    DUCKING_DRT_AUTOMATION,
    DUCKING_XMEML_KEYFRAMES,
    DUCKING_MODES_IMPLEMENTED,
    DUCKING_MODES_ALL,
)


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
        "mode": DUCKING_RENDERED_BED,
    }


# ── Beat / onset detection (Phase 3, montage genre) ──────────────────────────
#
# ffmpeg-only, no librosa/numpy: ffmpeg decodes to mono PCM, then a time-domain
# energy-novelty picker finds onsets. This is an honest *estimator* — good enough
# to hang montage cuts on a real musical grid, not a claim of sample-accurate
# beat tracking. The DSP is factored into pure functions so it unit-tests against
# synthetic signals without invoking ffmpeg.

BEAT_SAMPLE_RATE = 22050
_BEAT_FRAME = 1024
_BEAT_HOP = 512
DEFAULT_ONSET_SENSITIVITY = 1.5      # novelty must exceed local mean × this
DEFAULT_MIN_ONSET_GAP_SECONDS = 0.12  # refractory period (~200 BPM ceiling on onsets)


def _decode_pcm_mono(
    path: str, sample_rate: int = BEAT_SAMPLE_RATE
) -> Tuple[Optional["array.array"], int]:
    """Decode any audio/AV file to mono float32 PCM via ffmpeg.

    Returns ``(samples, sample_rate)`` or ``(None, sample_rate)`` when ffmpeg
    fails. Samples are normalized floats in roughly [-1, 1].
    """
    args = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", path,
        "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-",
    ]
    try:
        proc = safe_run(args, capture_output=True)
    except OSError:
        return None, sample_rate
    if proc.returncode != 0:
        return None, sample_rate
    raw = proc.stdout
    usable = len(raw) - (len(raw) % 4)
    samples = array.array("f")
    samples.frombytes(raw[:usable])
    if sys.byteorder != "little":  # ffmpeg emits little-endian; match the host
        samples.byteswap()
    return samples, sample_rate


def onset_novelty(
    samples: "array.array",
    sample_rate: int,
    *,
    frame: int = _BEAT_FRAME,
    hop: int = _BEAT_HOP,
) -> Tuple[List[float], List[float]]:
    """Per-frame onset novelty: half-wave-rectified rise in log RMS energy.

    Returns ``(times, novelty)`` — frame centers (seconds) and a non-negative
    novelty value per frame (0 for the first frame). Pure Python; no ffmpeg.
    """
    n = len(samples)
    if n < frame or hop <= 0:
        return [], []
    # Prefix sums of squares: each window's energy by subtraction — one O(n)
    # pass instead of re-squaring every sample per overlapping frame.
    prefix = [0.0] * (n + 1)
    acc = 0.0
    for idx, v in enumerate(samples):
        acc += v * v
        prefix[idx + 1] = acc
    times: List[float] = []
    energies: List[float] = []
    i = 0
    while i + frame <= n:
        window = prefix[i + frame] - prefix[i]
        energies.append(math.sqrt(max(window, 0.0) / frame))
        times.append((i + frame / 2) / sample_rate)
        i += hop
    novelty = [0.0]
    for k in range(1, len(energies)):
        rise = math.log1p(energies[k]) - math.log1p(energies[k - 1])
        novelty.append(rise if rise > 0.0 else 0.0)
    return times, novelty


def pick_onsets(
    times: List[float],
    novelty: List[float],
    *,
    sensitivity: float = DEFAULT_ONSET_SENSITIVITY,
    min_gap_seconds: float = DEFAULT_MIN_ONSET_GAP_SECONDS,
    window_seconds: float = 0.3,
) -> List[float]:
    """Adaptive-threshold peak picking over an onset-novelty curve.

    An onset is a local maximum whose novelty exceeds ``sensitivity`` × the local
    mean, respecting a ``min_gap_seconds`` refractory period. Pure Python.
    """
    if len(novelty) < 3:
        return []
    # Frame period from the time axis; fall back to a sane default.
    step = (times[1] - times[0]) if len(times) > 1 and times[1] > times[0] else 0.023
    win = max(1, int(window_seconds / step))
    onsets: List[float] = []
    last_t = -1e9
    for k in range(1, len(novelty) - 1):
        nk = novelty[k]
        if nk <= 0.0 or nk < novelty[k - 1] or nk < novelty[k + 1]:
            continue
        lo = max(0, k - win)
        hi = min(len(novelty), k + win + 1)
        local = novelty[lo:hi]
        threshold = (sum(local) / len(local)) * sensitivity
        if nk > threshold and (times[k] - last_t) >= min_gap_seconds:
            onsets.append(round(times[k], 3))
            last_t = times[k]
    return onsets


def estimate_tempo_bpm(onset_times: List[float]) -> Optional[float]:
    """Median-inter-onset-interval tempo, folded into a musical 60–180 BPM range."""
    if len(onset_times) < 2:
        return None
    iois = [b - a for a, b in zip(onset_times, onset_times[1:]) if b > a]
    if not iois:
        return None
    median = statistics.median(iois)
    if median <= 0:
        return None
    bpm = 60.0 / median
    while bpm < 60.0:
        bpm *= 2.0
    while bpm > 180.0:
        bpm /= 2.0
    return round(bpm, 1)


def detect_beats(
    path: str,
    *,
    sample_rate: int = BEAT_SAMPLE_RATE,
    sensitivity: float = DEFAULT_ONSET_SENSITIVITY,
    min_gap_seconds: float = DEFAULT_MIN_ONSET_GAP_SECONDS,
) -> Dict[str, Any]:
    """Beat/onset detection for the montage genre — ffmpeg-only, no librosa.

    Returns ``{success, available, onsets: [seconds], onset_count, tempo_bpm,
    duration_seconds, sample_rate, method}``. Honest failure (``available``
    False) when the file is missing or ffmpeg cannot decode it — montage cutting
    must never run on a fabricated grid.
    """
    if not os.path.isfile(path):
        return {"success": False, "available": False, "path": path,
                "error": "file not found"}
    samples, sr = _decode_pcm_mono(path, sample_rate)
    if not samples:
        return {"success": False, "available": False, "path": path,
                "error": "ffmpeg could not decode audio (missing ffmpeg, or an empty/corrupt track)"}
    times, novelty = onset_novelty(samples, sr)
    onsets = pick_onsets(times, novelty, sensitivity=sensitivity,
                         min_gap_seconds=min_gap_seconds)
    return {
        "success": True,
        "available": True,
        "path": path,
        "duration_seconds": round(len(samples) / sr, 3),
        "sample_rate": sr,
        "onsets": onsets,
        "onset_count": len(onsets),
        "tempo_bpm": estimate_tempo_bpm(onsets),
        "method": "ffmpeg mono-PCM decode + time-domain energy-novelty onset picking (no librosa)",
    }
