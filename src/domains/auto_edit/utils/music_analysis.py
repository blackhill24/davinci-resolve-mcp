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
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from src.domains.media_analysis.utils.technical_probe import _ffmpeg_stderr_filter, _parse_loudness, _run_command
from src.core.proc import safe_run

# Full-file PCM decode of a music bed. Generous — a long track on a slow disk is
# legitimate — but bounded, so a wedged ffmpeg cannot hang the auto-edit run.
DECODE_TIMEOUT_SECONDS = 10 * 60

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

# Band-limited decodes for tempo/downbeat/section work (issue #176). Tempo
# tracking on the full mix locks onto whichever instrument is loudest/busiest
# (usually hats or vocal sibilance); the kick band isolates the pulse that
# actually carries the beat.
BAND_FILTERS = {
    "kick": "lowpass=f=150",
    "snare": "bandpass=f=250:width_type=o:w=2",
    "hats": "highpass=f=4000",
}

MIN_TEMPO_CONFIDENCE = 1.2  # tempogram winner must beat the next non-harmonic peak by this ratio
BAR_METER = 4
SECTION_QUANTUM_BARS = 8
_TEMPO_CONFIDENCE_CAP = 1000.0


def _decode_pcm_mono(
    path: str,
    sample_rate: int = BEAT_SAMPLE_RATE,
    audio_filter: Optional[str] = None,
) -> Tuple[Optional["array.array"], int]:
    """Decode any audio/AV file to mono float32 PCM via ffmpeg.

    ``audio_filter`` (e.g. ``"lowpass=f=150"``) is inserted as ``-af`` before
    the output args — used by :func:`band_novelty` to band-limit the decode.

    Returns ``(samples, sample_rate)`` or ``(None, sample_rate)`` when ffmpeg
    fails. Samples are normalized floats in roughly [-1, 1].
    """
    args = ["ffmpeg", "-v", "error", "-nostdin", "-i", path]
    if audio_filter:
        args += ["-af", audio_filter]
    args += ["-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]
    try:
        # #111 finding 8: this was the one safe_run() caller in src/ with no
        # timeout, so a wedged ffmpeg blocked the auto-edit run indefinitely.
        proc = safe_run(args, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
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


def band_novelty(
    path: str, band: str, sample_rate: int = BEAT_SAMPLE_RATE
) -> Tuple[List[float], List[float]]:
    """Onset novelty computed from a single frequency band (kick/snare/hats).

    Decodes ``path`` through the band's ffmpeg filter (:data:`BAND_FILTERS`)
    and runs the existing :func:`onset_novelty` on the filtered signal.
    Returns ``([], [])`` on an unknown band or decode failure — never raises.
    """
    audio_filter = BAND_FILTERS.get(band)
    if audio_filter is None:
        return [], []
    samples, sr = _decode_pcm_mono(path, sample_rate, audio_filter=audio_filter)
    if not samples:
        return [], []
    return onset_novelty(samples, sr)


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


def tempogram(
    novelty: List[float],
    step: float,
    *,
    min_bpm: float = 60.0,
    max_bpm: float = 180.0,
    prior_bpm: float = 120.0,
    sigma: float = 0.9,
) -> Dict[str, Any]:
    """Autocorrelation tempo estimate with a metrical comb and a log-normal prior.

    Scores every 0.1 BPM candidate in ``[min_bpm, max_bpm]`` by combining the
    autocorrelation of the (mean-removed) novelty curve at the candidate lag
    with half/double/quadruple that lag (the "metrical comb" — this is what
    stops the estimator locking onto a subdivision like a 16th-note grid),
    weighted by a log-normal prior centered on ``prior_bpm``.

    Returns ``{"ranked": [{"bpm", "score"}, ...], "confidence": float}``.
    ``ranked`` is sorted best-first. ``confidence`` is the winner's score
    divided by the best score among candidates that are *not* within 3% of the
    winner or its x2/÷2/x3/÷3 harmonics — a track with one clear peak family
    scores high; an ambiguous one (noise, or several competing tempos) scores
    near 1.0. Pure Python; the input novelty is caller-supplied so this never
    touches ffmpeg.
    """
    n = len(novelty)
    if n < 4 or step <= 0:
        return {"ranked": [], "confidence": 0.0}
    mean = sum(novelty) / n
    centered = [v - mean for v in novelty]
    cache: Dict[int, float] = {}

    def ac(lag: int) -> float:
        if lag < 2 or lag >= n // 2:
            return 0.0
        cached = cache.get(lag)
        if cached is not None:
            return cached
        limit = n - lag
        total = 0.0
        for i in range(limit):
            total += centered[i] * centered[i + lag]
        value = total / limit
        cache[lag] = value
        return value

    lo10 = int(round(min_bpm * 10))
    hi10 = int(round(max_bpm * 10))
    candidates: List[Dict[str, float]] = []
    for bpm10 in range(lo10, hi10 + 1):
        bpm = bpm10 / 10.0
        lag = (60.0 / bpm) / step
        score = (
            ac(int(round(lag)))
            + 0.5 * ac(int(round(2 * lag)))
            + 0.25 * ac(int(round(4 * lag)))
            + 0.5 * ac(int(round(lag / 2)))
        )
        weight = math.exp(-0.5 * (math.log2(bpm / prior_bpm) / sigma) ** 2)
        candidates.append({"bpm": bpm, "score": score * weight})

    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)
    if not ranked:
        return {"ranked": [], "confidence": 0.0}

    winner_bpm = ranked[0]["bpm"]
    winner_score = ranked[0]["score"]

    def is_harmonic_of_winner(bpm: float) -> bool:
        for mult in (1.0, 2.0, 0.5, 3.0, 1.0 / 3.0):
            target = winner_bpm * mult
            if target > 0 and abs(bpm - target) / target <= 0.03:
                return True
        return False

    second_score = None
    for cand in ranked[1:]:
        if not is_harmonic_of_winner(cand["bpm"]):
            second_score = cand["score"]
            break

    if winner_score <= 1e-12:
        confidence = 0.0
    elif second_score is None or second_score <= 1e-12:
        confidence = _TEMPO_CONFIDENCE_CAP
    else:
        confidence = min(winner_score / second_score, _TEMPO_CONFIDENCE_CAP)

    return {"ranked": ranked, "confidence": confidence}


def lock_phase(
    novelty: List[float], step: float, period: float, duration: float
) -> float:
    """Beat-zero offset (seconds) that best aligns a grid of ``period``-second
    steps to the peaks of ``novelty``.

    Sweeps the offset across exactly one beat period, sampling the novelty
    curve at every grid point for that offset, and returns the offset with the
    highest mean novelty — a single phase lock for the whole track, not a
    per-cut snap. Pure Python.
    """
    n = len(novelty)
    if period <= 0 or step <= 0 or n == 0 or duration <= 0:
        return 0.0
    covered = (n - 1) * step
    limit = min(duration, covered) if covered > 0 else duration
    steps_per_period = max(1, int(round(period / step)))

    best_offset = 0.0
    best_mean = float("-inf")
    for o in range(steps_per_period):
        offset = o * step
        total = 0.0
        count = 0
        k = 0
        while True:
            t = offset + k * period
            if t >= limit:
                break
            idx = int(round(t / step))
            if 0 <= idx < n:
                total += novelty[idx]
                count += 1
            k += 1
        if count == 0:
            continue
        mean = total / count
        if mean > best_mean:
            best_mean = mean
            best_offset = offset
    return best_offset


def beat_grid(tempo_bpm: Optional[float], beat_zero: float, duration: float) -> List[float]:
    """A strictly regular beat grid: ``beat_zero + k * 60/tempo_bpm`` while ``< duration``.

    This is the central shape change over the old per-cut nearest-onset snap —
    one phase lock for the whole track, then a regular grid from it.
    """
    if not tempo_bpm or tempo_bpm <= 0 or duration <= 0:
        return []
    period = 60.0 / tempo_bpm
    grid: List[float] = []
    k = 0
    while True:
        t = beat_zero + k * period
        if t >= duration:
            break
        if t >= 0:
            grid.append(round(t, 6))
        k += 1
    return grid


def downbeat_phase(
    kick_novelty: List[float], step: float, beat_times: List[float], *, meter: int = BAR_METER
) -> int:
    """Which of the ``meter`` beat-grid phases is the downbeat.

    Scores each candidate phase (0..meter-1) by the summed kick-band novelty
    at the beats it selects (every ``meter``-th beat starting at that phase)
    and returns the winner.
    """
    if not beat_times or not kick_novelty or step <= 0 or meter < 1:
        return 0
    n = len(kick_novelty)
    best_phase = 0
    best_score = float("-inf")
    for phase in range(meter):
        score = 0.0
        for i, t in enumerate(beat_times):
            if i % meter != phase:
                continue
            idx = int(round(t / step))
            if 0 <= idx < n:
                score += kick_novelty[idx]
        if score > best_score:
            best_score = score
            best_phase = phase
    return best_phase


def _label_section_energy(energy: float, median_energy: float, *, is_first: bool, is_rising: bool) -> str:
    if median_energy <= 0:
        return "mid"
    ratio = energy / median_energy
    if is_first and ratio < 1.0:
        return "intro"
    if is_rising and ratio < 1.0:
        return "build"
    if ratio >= 1.3:
        return "high"
    if ratio <= 0.7:
        return "low"
    return "mid"


def sections(
    bar_grid: List[float],
    band_novelties: Dict[str, List[float]],
    step: float,
    *,
    quantum_bars: int = SECTION_QUANTUM_BARS,
) -> List[Dict[str, Any]]:
    """Coarse section map from per-bar energy, quantised to phrase boundaries.

    Builds a per-bar combined-band energy vector from ``band_novelties``
    (e.g. ``{"kick": [...], "snare": [...], "hats": [...]}``, each aligned to
    ``step``-second frames), then chunks the track into ``quantum_bars``-bar
    blocks (real arrangements change on 8- or 16-bar phrase boundaries, not
    arbitrary bars). Each block is labelled relative to the track's median
    bar energy, and the single largest upward energy jump between consecutive
    blocks is flagged ``is_drop``.
    """
    n_bars = len(bar_grid)
    if n_bars < 2 or step <= 0 or quantum_bars < 1:
        return []
    bands = [b for b in band_novelties.values() if b]

    def bar_energy(bar_idx: int) -> float:
        t0 = bar_grid[bar_idx]
        t1 = bar_grid[bar_idx + 1] if bar_idx + 1 < n_bars else t0 + (
            bar_grid[1] - bar_grid[0] if n_bars > 1 else step)
        i0 = max(0, int(round(t0 / step)))
        i1 = max(i0, int(round(t1 / step)))
        total = 0.0
        for arr in bands:
            lo = min(i0, len(arr))
            hi = min(i1, len(arr))
            total += sum(arr[lo:hi])
        return total

    energies = [bar_energy(i) for i in range(n_bars)]
    median_energy = statistics.median(energies)

    boundaries = list(range(0, n_bars, quantum_bars))
    if boundaries[-1] != n_bars:
        boundaries.append(n_bars)
    if len(boundaries) < 2:
        boundaries = [0, n_bars]

    out: List[Dict[str, Any]] = []
    block_energies: List[float] = []
    for start_bar, end_bar in zip(boundaries[:-1], boundaries[1:]):
        block = energies[start_bar:end_bar]
        block_energies.append(sum(block) / max(1, len(block)))

    best_jump = None
    prev_energy = None
    for idx, (start_bar, end_bar) in enumerate(zip(boundaries[:-1], boundaries[1:])):
        seg_energy = block_energies[idx]
        is_rising = prev_energy is not None and seg_energy > prev_energy
        label = _label_section_energy(
            seg_energy, median_energy, is_first=(idx == 0), is_rising=is_rising)
        out.append({
            "start_bar": start_bar,
            "end_bar": end_bar,
            "start_seconds": bar_grid[start_bar],
            "end_seconds": bar_grid[end_bar] if end_bar < n_bars else bar_grid[-1],
            "energy": seg_energy,
            "label": label,
            "is_drop": False,
        })
        if prev_energy is not None:
            jump = seg_energy - prev_energy
            if jump > 0 and (best_jump is None or jump > best_jump[0]):
                best_jump = (jump, idx)
        prev_energy = seg_energy

    if best_jump is not None:
        out[best_jump[1]]["is_drop"] = True
    return out


def detect_beats(
    path: str,
    *,
    sample_rate: int = BEAT_SAMPLE_RATE,
    sensitivity: float = DEFAULT_ONSET_SENSITIVITY,
    min_gap_seconds: float = DEFAULT_MIN_ONSET_GAP_SECONDS,
) -> Dict[str, Any]:
    """Beat/onset detection for the montage genre — ffmpeg-only, no librosa.

    Returns ``{success, available, onsets: [seconds], onset_count, tempo_bpm,
    duration_seconds, sample_rate, method, beat_grid, bar_grid, downbeats,
    sections, tempo_confidence, beat_zero, grid_available}``. ``tempo_bpm``
    comes from :func:`tempogram` on the kick-band novelty (falling back to the
    full-mix novelty when the kick band carries negligible signal, e.g. a
    track with no low end). Honest failure (``available`` False) when the file
    is missing or ffmpeg cannot decode it. Honest *degradation* below
    ``MIN_TEMPO_CONFIDENCE``: ``grid_available`` is False, ``beat_grid`` is
    empty, and ``problems`` explains why — montage cutting must never run on a
    fabricated grid.
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
    duration = round(len(samples) / sr, 3)
    step = (times[1] - times[0]) if len(times) > 1 else (_BEAT_HOP / sr)

    kick_times, kick_novelty = band_novelty(path, "kick", sr)
    kick_step = (kick_times[1] - kick_times[0]) if len(kick_times) > 1 else step

    problems: List[str] = []
    tempo_bpm: Optional[float] = None
    tempo_confidence = 0.0
    beat_zero = 0.0
    beat_grid_values: List[float] = []
    provisional_tempo_bpm: Optional[float] = None
    provisional_beat_grid: List[float] = []
    downbeats: List[float] = []
    bar_grid_values: List[float] = []
    sections_values: List[Dict[str, Any]] = []
    grid_available = False

    tempo_novelty, tempo_step = kick_novelty, kick_step
    if not tempo_novelty or max(abs(v) for v in tempo_novelty) < 1e-9:
        # No usable low end (e.g. a track/click with nothing under 150 Hz) —
        # fall back to the full-mix novelty rather than reporting no tempo.
        tempo_novelty, tempo_step = novelty, step

    if len(tempo_novelty) >= 4:
        tg = tempogram(tempo_novelty, tempo_step)
        ranked = tg["ranked"]
        tempo_confidence = tg["confidence"]
        if ranked and tempo_confidence >= MIN_TEMPO_CONFIDENCE:
            tempo_bpm = round(ranked[0]["bpm"], 1)
            period = 60.0 / tempo_bpm
            beat_zero = round(lock_phase(tempo_novelty, tempo_step, period, duration), 3)
            beat_grid_values = beat_grid(tempo_bpm, beat_zero, duration)
            if beat_grid_values:
                phase = downbeat_phase(kick_novelty or tempo_novelty, kick_step,
                                       beat_grid_values, meter=BAR_METER)
                downbeats = [t for i, t in enumerate(beat_grid_values) if i % BAR_METER == phase]
                bar_grid_values = list(downbeats)
                if len(bar_grid_values) >= 2:
                    _, snare_novelty = band_novelty(path, "snare", sr)
                    _, hats_novelty = band_novelty(path, "hats", sr)
                    band_map = {"kick": kick_novelty, "snare": snare_novelty, "hats": hats_novelty}
                    sections_values = sections(bar_grid_values, band_map, kick_step)
                grid_available = True
        elif ranked:
            # Sub-threshold: NOT confident enough to schedule an arrangement on
            # (grid_available stays False, tempo_bpm/beat_grid stay empty, and
            # every existing consumer behaves exactly as before). But it is
            # still a kick-phase-locked pulse estimate, and the no-grid cutter
            # snapping to it beats snapping to raw onsets by a wide, MEASURED
            # margin: on the reference track, onsets land near a beat 0.228 of
            # the time (chance is 0.240 — i.e. onset peaks do not follow the
            # pulse at all, in the full mix OR the kick band), while a
            # phase-locked grid is on the pulse by construction.
            provisional_tempo_bpm = round(ranked[0]["bpm"], 1)
            provisional_beat_zero = round(
                lock_phase(tempo_novelty, tempo_step, 60.0 / provisional_tempo_bpm, duration), 3)
            provisional_beat_grid = beat_grid(
                provisional_tempo_bpm, provisional_beat_zero, duration)
            problems.append(
                f"tempo estimate not confident enough (confidence={tempo_confidence:.2f} < "
                f"{MIN_TEMPO_CONFIDENCE}); no beat grid produced — a provisional "
                f"{provisional_tempo_bpm} BPM pulse is offered for cut snapping only"
            )
        else:
            problems.append(
                f"tempo estimate not confident enough (confidence={tempo_confidence:.2f} < "
                f"{MIN_TEMPO_CONFIDENCE}); no beat grid produced"
            )
    else:
        problems.append("novelty curve too short to estimate tempo; no beat grid produced")

    result: Dict[str, Any] = {
        "success": True,
        "available": True,
        "path": path,
        "duration_seconds": duration,
        "sample_rate": sr,
        "onsets": onsets,
        "onset_count": len(onsets),
        # Sub-threshold pulse, for CUT SNAPPING only — never for scheduling an
        # arrangement (that is what grid_available/beat_grid gate). Both are
        # empty whenever the confident grid exists or no tempo could be
        # estimated at all. Measured on the reference track: raw onsets land
        # near a beat 0.228 of the time against a 0.240 chance level, in the
        # full mix AND in the kick band alone — peak-picking does not recover
        # the pulse, so a phase-locked grid is the only honest snap target.
        "provisional_tempo_bpm": provisional_tempo_bpm,
        "provisional_beat_grid": provisional_beat_grid,
        "tempo_bpm": tempo_bpm,
        "method": "ffmpeg mono-PCM decode + time-domain energy-novelty onset picking (no librosa)",
        "beat_grid": beat_grid_values,
        "bar_grid": bar_grid_values,
        "downbeats": downbeats,
        "sections": sections_values,
        "tempo_confidence": round(tempo_confidence, 3),
        "beat_zero": beat_zero,
        "grid_available": grid_available,
    }
    if problems:
        result["problems"] = problems
    return result
