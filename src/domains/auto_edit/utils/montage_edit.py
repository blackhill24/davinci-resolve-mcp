"""montage_edit — auto-assembly decision layer for the montage genre
(Phase 3 of the auto_edit/cut_ir program; epic #38, P1 = issue #40).

Pure evidence + planning, mirroring auto_edit.py's shape: no Resolve
imports, reads the DB-canonical analysis store, produces a
cut_ir.CutList using the schema's existing montage/montage_hook roles
(cut_ir.MONTAGE_SEGMENT_ROLES — "a montage plan is the same shape with
montage roles", no schema change). Execution reuses auto_edit's
build_timeline/approve_cut/finish/apply_revision UNCHANGED — those
functions only operate on the CutList structure, not on which decision
layer produced it. server.py's auto_edit tool branches start_brief/
plan_cut by brief.genre (wired in P2, issue #41); this module never
registers its own MCP tool.

Cut timing (issue #177, phase 2/6 of the montage-quality epic): when
music_analysis.detect_beats locks a confident grid (`grid_available: True`),
every cut boundary is a BEAT INDEX from montage_arrangement's schedule, not a
snapped-to-nearest-onset time — see montage_arrangement.py for how section
evidence becomes cut lengths. Source length is DERIVED from record length
(`beat_frames[k+n] - beat_frames[k]`), so drift is structurally impossible
rather than merely corrected. Below the confidence threshold, phase 1
degrades honestly (`grid_available: False`) and this module falls back to
the original onset-snap behaviour rather than inventing a grid:
  - PACING comes from local onset DENSITY around each point (dense onsets
    nearby read as high-energy — shorter target cut; sparse — a longer
    hold). Reuses the onset list music_analysis.detect_beats returns.
  - PLACEMENT (which candidate shot) still comes from each shot's own
    `pacing` classification in BOTH modes — kinetic shots slot into
    high-density regions, still shots into low-density ones (`shot_fits_zone`)
    — local onset density remains a placement tiebreaker even when it no
    longer drives cut length.
  - Fallback-mode cut boundaries snap to the NEAREST real onset at or after
    the running cursor, never a mathematical beat count.

Shot exhaustion: the select_potential floor loosens high -> medium -> low
(mirrors edit_engine.plan_selects' own tunable) to keep filling the music's
runtime. In grid mode, candidate WINDOWS (a shot's own advancing source
cursor) and clip-level round-robin (never two consecutive segments from the
same clip_uuid) let a shot be reused via a different in-point rather than
exhausting the pool early; if every clip is genuinely out of usable seconds
the montage still TRUNCATES rather than repeating a window or fabricating
coverage, and says so honestly in `problems`.

No voiceover/ducking concept in v1 — strictly B-roll + music. Music is
required (its length sets the montage's runtime; target_duration_seconds,
if given, trims it rather than replacing it as the primary driver).
"""

from __future__ import annotations

import bisect
import json
import os
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.core import timeline_brain_db
from src.core.proc import safe_run
from src.domains.auto_edit.utils import auto_edit, cut_ir, edit_engine, montage_arrangement, music_analysis

GENRE = "montage"

_SELECT_RANK = {"high": 3, "medium": 2, "low": 1}
_SELECT_TIERS = ("high", "medium", "low")  # loosen in this order

MIN_SHOT_SECONDS = 0.4
HOOK_BEATS = 2.0
DEFAULT_HOOK_SECONDS = 1.5  # fallback when tempo can't be estimated (<2 onsets)
MIN_CUT_SECONDS = 0.5
MAX_CUT_SECONDS = 6.0
DEFAULT_TARGET_CUT_SECONDS = 2.0
ENERGY_WINDOW_SECONDS = 4.0  # local onset-density window, both ends

# pacing (per-shot) -> which local-density zone the shot fits. "any" always
# matches; the other two are exclusive (a shot flagged for the opposite zone
# is skipped there, not just deprioritized) so the categorical tag actually
# means something in placement, not just a tiebreaker.
_PACING_ZONE = {
    "still": "low",
    "kinetic": "high",
    "moderate": "any",
    "variable": "any",
    "unknown": "any",
}
HIGH_DENSITY_THRESHOLD = 0.5  # density_ratio at/above this reads as "high" zone


def validate_montage_brief_inputs(
    *,
    files: Any,
    music: Any,
    target_duration_seconds: Any = None,
) -> List[str]:
    """Pure input validation; mirrors auto_edit.validate_brief_inputs' shape."""
    errors: List[str] = []
    if not isinstance(files, (list, tuple)) or not files:
        errors.append("files must be a non-empty list of media paths (the candidate shot pool)")
    if not music or not isinstance(music, str):
        errors.append("music is required for the montage genre — its length sets the runtime")
    if target_duration_seconds is not None:
        if not isinstance(target_duration_seconds, (int, float)) or target_duration_seconds <= 0:
            errors.append("target_duration_seconds must be a positive number")
    return errors


# ── candidate shot gathering ─────────────────────────────────────────────────


def _clip_level_select_potential(conn, clip_uuid: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT value_json FROM subjective_fields
        WHERE entity_type='clip' AND entity_uuid=? AND superseded_at IS NULL
          AND field_path='editorial_classification.select_potential'
        """,
        (clip_uuid,),
    ).fetchone()
    if row is None:
        return None
    try:
        return str(json.loads(row["value_json"])).lower()
    except (TypeError, ValueError):
        return None


def _candidate_shots(conn, clip_uuids: Sequence[str]) -> List[Dict[str, Any]]:
    """Every usable shot across the given clips, ranked by select_potential
    (shot-level deep vision, falling back to clip-level) with its pacing."""
    if not clip_uuids:
        return []
    placeholders = ",".join("?" * len(clip_uuids))
    clips = {
        str(r["clip_uuid"]): dict(r)
        for r in conn.execute(
            f"SELECT * FROM clips WHERE clip_uuid IN ({placeholders})", list(clip_uuids)
        ).fetchall()
    }
    candidates: List[Dict[str, Any]] = []
    for shot_row in conn.execute(
        f"SELECT * FROM shots WHERE clip_uuid IN ({placeholders}) ORDER BY clip_uuid, shot_index",
        list(clip_uuids),
    ).fetchall():
        shot = dict(shot_row)
        clip = clips.get(str(shot["clip_uuid"]))
        if not clip or not clip.get("resolve_clip_id"):
            continue
        start, end = shot.get("time_seconds_start"), shot.get("time_seconds_end")
        if start is None or end is None or float(end) - float(start) < MIN_SHOT_SECONDS:
            continue
        groups = edit_engine._shot_groups(shot)
        editorial = groups.get("editorial") if isinstance(groups.get("editorial"), dict) else {}
        select_potential = str(editorial.get("select_potential") or "").lower()
        rank = _SELECT_RANK.get(select_potential, 0)
        if rank == 0:
            # Standard-analyzed clips have no per-shot deep pass yet — fall
            # back to clip-level select potential, same as plan_selects (E1).
            fallback = _clip_level_select_potential(conn, str(shot["clip_uuid"]))
            if fallback:
                rank = _SELECT_RANK.get(fallback, 0)
        pacing = str(editorial.get("pacing") or "unknown").lower()
        if pacing not in _PACING_ZONE:
            pacing = "unknown"
        candidates.append({
            "clip_uuid": str(shot["clip_uuid"]),
            "clip_name": clip.get("clip_name"),
            "resolve_clip_id": clip.get("resolve_clip_id"),
            "file_path": clip.get("file_path"),
            "shot_uuid": shot.get("shot_uuid"),
            "shot_index": shot["shot_index"],
            "time_seconds_start": float(start),
            "time_seconds_end": float(end),
            "duration_seconds": round(float(end) - float(start), 3),
            "fps": edit_engine._clip_fps(clip),
            "rank": rank,
            "pacing": pacing,
            "description": shot.get("description"),
            "colour_signature": _scout_colour_signature(groups.get("scout")),
        })
    return candidates


# ── look bucketing: per-clip colour match (issue #179) ──────────────────────
#
# Desktop defined three hand-picked grade buckets and corrected each toward a
# shared target so dusk/storm/midday footage would actually intercut; one
# uniform grade cannot do that. This clusters source clips by a colour
# signature (phase 3's scouted dominant_colour when available, else a cheap
# ffmpeg brightness/tone read, else an honest neutral default — never a
# fabricated contrast) and derives a per-bucket "match" CDL that pulls every
# bucket toward the shared (median-brightness) target. The creative "look"
# (a LUT/DRX) stays a SEPARATE, uniform stage 2 applied by `finish` — see its
# `grade` branch in actions.py.

_BRIGHTNESS_BANDS = (("dark", 0.35), ("mid", 0.65), ("bright", 1.01))
_BAND_ORDER = {"dark": 0, "mid": 1, "bright": 2}
MAX_LOOK_BUCKETS = 4
_LOOK_TONE_TILT = 0.05  # per-channel slope nudge that neutralizes a warm/cool bias


def _brightness_band(brightness: float) -> str:
    for label, upper in _BRIGHTNESS_BANDS:
        if brightness < upper:
            return label
    return "bright"


_SCOUT_QUALITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _scout_desirability(entry: Dict[str, Any]) -> float:
    """Higher is better among usable scout windows (issue #178's scout
    schema) — used here purely to pick which window's colour read
    represents the shot, mirroring how the in-point itself is chosen."""
    return (
        _SCOUT_QUALITY_RANK.get(str(entry.get("subject_clarity", "")).lower(), 0)
        + _SCOUT_QUALITY_RANK.get(str(entry.get("motion_interest", "")).lower(), 0)
        + _SCOUT_QUALITY_RANK.get(str(entry.get("composition", "")).lower(), 0)
    )


def _scout_colour_signature(scout_entries: Any) -> Optional[Dict[str, Any]]:
    """{"tone", "brightness", "exposure"} from the best USABLE scout window
    for this shot (issue #178's per-window scout data), or None when it was
    never scouted — the ffmpeg fallback and honest default take over in
    assign_look_buckets."""
    if not isinstance(scout_entries, list):
        return None
    usable = [e for e in scout_entries if isinstance(e, dict) and e.get("usable")]
    if not usable:
        return None
    best = max(usable, key=_scout_desirability)
    dominant = best.get("dominant_colour")
    if not isinstance(dominant, dict):
        return None
    tone = str(dominant.get("tone") or "").lower()
    brightness = dominant.get("brightness")
    if tone not in ("warm", "cool", "neutral") or not isinstance(brightness, (int, float)):
        return None
    return {"tone": tone, "brightness": float(brightness),
            "exposure": str(best.get("exposure") or "good").lower()}


def _ffmpeg_colour_signature(path: Optional[str], time_seconds: float) -> Optional[Dict[str, Any]]:
    """Cheap tone/brightness fallback via ffmpeg raw-pixel decode — no
    signalstats log parsing, no new dependency. Downscales to 8x8 and
    averages RGB; None on any failure (missing file, no ffmpeg, bad decode)."""
    if not path or not os.path.isfile(path):
        return None
    args = [
        "ffmpeg", "-v", "error", "-ss", f"{max(0.0, time_seconds):.3f}", "-i", path,
        "-frames:v", "1", "-vf", "scale=8:8", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ]
    try:
        proc = safe_run(args, capture_output=True, timeout=30)
    except Exception:
        return None  # any ffmpeg failure degrades honestly to the caller's next fallback
    raw = proc.stdout if proc.returncode == 0 else b""
    n = len(raw) // 3
    if n < 16:  # need enough of the 8x8 frame to trust an average
        return None
    r_sum = g_sum = b_sum = 0
    for i in range(n):
        r_sum += raw[3 * i]
        g_sum += raw[3 * i + 1]
        b_sum += raw[3 * i + 2]
    r, g, b = (r_sum / n / 255.0, g_sum / n / 255.0, b_sum / n / 255.0)
    brightness = (r + g + b) / 3.0
    diff = r - b
    tone = "warm" if diff > 0.03 else ("cool" if diff < -0.03 else "neutral")
    exposure = "crushed" if brightness < 0.15 else ("clipped" if brightness > 0.9 else "good")
    return {"tone": tone, "brightness": round(brightness, 3), "exposure": exposure}


def assign_look_buckets(
    candidates: List[Dict[str, Any]]
) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]], str]:
    """Cluster candidate clips into 2-4 look buckets.

    Returns ``(bucket_of_clip_uuid, signature_of_clip_uuid, basis)``. One
    signature per DISTINCT CLIP (a clip is normally shot under one lighting
    condition) — scout data first, then the ffmpeg fallback, then an honest
    neutral default. ``basis`` is ``"scout"``/``"ffmpeg_signature"``/
    ``"default"``/``"mixed"`` for the caller to report honestly.
    """
    signatures: Dict[str, Dict[str, Any]] = {}
    bases_used = set()
    for c in candidates:
        clip_uuid = c["clip_uuid"]
        if clip_uuid in signatures:
            continue
        sig = c.get("colour_signature")
        basis = "scout"
        if not sig:
            sig = _ffmpeg_colour_signature(c.get("file_path"), c["time_seconds_start"])
            basis = "ffmpeg_signature"
        if not sig:
            sig = {"tone": "neutral", "brightness": 0.5, "exposure": "good"}
            basis = "default"
        bases_used.add(basis)
        signatures[clip_uuid] = {**sig, "basis": basis}

    keyed: Dict[Tuple[str, str], List[str]] = {}
    for clip_uuid, sig in signatures.items():
        key = (sig["tone"], _brightness_band(sig["brightness"]))
        keyed.setdefault(key, []).append(clip_uuid)

    if len(keyed) > MAX_LOOK_BUCKETS:
        ranked = sorted(keyed, key=lambda k: -len(keyed[k]))
        kept, dropped = ranked[:MAX_LOOK_BUCKETS], ranked[MAX_LOOK_BUCKETS:]
        for key in dropped:
            nearest = min(kept, key=lambda k: abs(_BAND_ORDER[k[1]] - _BAND_ORDER[key[1]]))
            keyed[nearest].extend(keyed[key])
        keyed = {k: v for k, v in keyed.items() if k in kept}

    bucket_of_clip: Dict[str, str] = {}
    for tone, band in keyed:
        label = f"{tone}_{band}" if tone != "neutral" else f"neutral_{band}"
        for clip_uuid in keyed[(tone, band)]:
            bucket_of_clip[clip_uuid] = label

    basis = bases_used.pop() if len(bases_used) == 1 else "mixed"
    return bucket_of_clip, signatures, basis


def _match_cdl(tone: str, brightness: float, target_brightness: float) -> Dict[str, Any]:
    """A slope/offset/power CDL that neutralizes `tone`'s warm/cool bias and
    pulls `brightness` toward `target_brightness` — stage 1 ("match")."""
    offset = round(target_brightness - brightness, 4)
    if tone == "warm":
        slope = [1.0 - _LOOK_TONE_TILT, 1.0, 1.0 + _LOOK_TONE_TILT]
    elif tone == "cool":
        slope = [1.0 + _LOOK_TONE_TILT, 1.0, 1.0 - _LOOK_TONE_TILT]
    else:
        slope = [1.0, 1.0, 1.0]
    return {
        "NodeIndex": 1,
        "Slope": [round(v, 4) for v in slope],
        "Offset": [offset, offset, offset],
        "Power": [1.0, 1.0, 1.0],
    }


def compute_match_cdls(
    signatures: Dict[str, Dict[str, Any]], bucket_of_clip: Dict[str, str]
) -> Dict[str, Dict[str, Any]]:
    """bucket label -> match CDL, pulling every bucket toward the shared
    target (the MEDIAN bucket's brightness — derived from the buckets
    themselves, not a fixed constant, so it adapts to whatever footage this
    montage actually has)."""
    per_bucket: Dict[str, List[Dict[str, Any]]] = {}
    for clip_uuid, bucket in bucket_of_clip.items():
        per_bucket.setdefault(bucket, []).append(signatures[clip_uuid])
    bucket_avg: Dict[str, Dict[str, Any]] = {}
    for bucket, sigs in per_bucket.items():
        avg_brightness = sum(s["brightness"] for s in sigs) / len(sigs)
        tones = [s["tone"] for s in sigs]
        dominant_tone = max(set(tones), key=tones.count)
        bucket_avg[bucket] = {"brightness": avg_brightness, "tone": dominant_tone}
    if not bucket_avg:
        return {}
    target_brightness = statistics.median(v["brightness"] for v in bucket_avg.values())
    return {
        bucket: _match_cdl(sig["tone"], sig["brightness"], target_brightness)
        for bucket, sig in bucket_avg.items()
    }


# ── energy curve (pacing + placement) ────────────────────────────────────────


def local_onset_density(
    onsets: Sequence[float], t: float, *, window: float = ENERGY_WINDOW_SECONDS
) -> float:
    """Onsets per second within [t - window/2, t + window/2] — the pacing
    signal. Dense onsets nearby = high-energy section = a shorter target
    cut there; sparse onsets = mellow = a longer hold. Reuses the same
    onset list snap-to-beat needs — no separate DSP pass."""
    if window <= 0:
        return 0.0
    lo, hi = t - window / 2.0, t + window / 2.0
    count = sum(1 for o in onsets if lo <= o < hi)
    return count / window


def target_cut_seconds(density: float, *, max_density: float) -> float:
    """Higher local onset density -> shorter target cut (faster pacing).
    Linear interpolation between MAX_CUT_SECONDS (zero density) and
    MIN_CUT_SECONDS (max observed density in this track)."""
    if max_density <= 0:
        return DEFAULT_TARGET_CUT_SECONDS
    ratio = min(1.0, max(0.0, density / max_density))
    return MAX_CUT_SECONDS - ratio * (MAX_CUT_SECONDS - MIN_CUT_SECONDS)


def shot_fits_zone(pacing: str, density_ratio: float, *, high_threshold: float = HIGH_DENSITY_THRESHOLD) -> bool:
    zone = _PACING_ZONE.get(pacing, "any")
    if zone == "any":
        return True
    is_high = density_ratio >= high_threshold
    return (zone == "high") == is_high


def nearest_onset(onsets: Sequence[float], target: float, *, minimum: float) -> float:
    """Nearest onset at or after `minimum`, closest to `target`. Falls back
    to `target` itself when no onset qualifies (e.g. sparse tail of the
    track) — never fabricates a beat that isn't there."""
    candidates = [o for o in onsets if o >= minimum]
    if not candidates:
        return target
    return min(candidates, key=lambda o: abs(o - target))


class _ShotPool:
    """Candidate shots grouped by clip, with round-robin selection and a
    per-shot advancing source cursor ("candidate windows") instead of
    one-shot-one-use.

    A shot may be picked again later with a *different* in-point (whatever
    the cursor has advanced to) as long as its clip isn't the immediately
    preceding pick — that's what removes the old truncation path without
    ever repeating the exact same footage back-to-back.
    """

    def __init__(self, candidates: List[Dict[str, Any]]):
        by_clip: Dict[str, List[Dict[str, Any]]] = {}
        for c in candidates:
            by_clip.setdefault(c["clip_uuid"], []).append(dict(c, cursor=c["time_seconds_start"]))
        self._by_clip = by_clip
        self._clip_order = list(by_clip.keys())
        self._rr_ptr = 0

    def _best_in_clip(self, clip_uuid: str, *, floor_rank: int, needed_seconds: float,
                       density_ratio: float) -> Optional[Dict[str, Any]]:
        shots = self._by_clip.get(clip_uuid, [])
        eligible = [s for s in shots
                    if s["rank"] >= floor_rank
                    and (s["time_seconds_end"] - s["cursor"]) >= needed_seconds]
        if not eligible:
            return None
        zone_matches = [s for s in eligible if shot_fits_zone(s["pacing"], density_ratio)]
        pick_from = zone_matches or eligible
        pick_from.sort(key=lambda s: (-s["rank"], -(s["time_seconds_end"] - s["cursor"])))
        return pick_from[0]

    def pick(self, *, exclude_clip_uuid: Optional[str], floor_rank: int,
             needed_seconds: float, density_ratio: float) -> Optional[Dict[str, Any]]:
        """Round-robin across clips (skipping `exclude_clip_uuid`), best shot
        within the winning clip. Advances the rotation only on success."""
        n = len(self._clip_order)
        for step in range(n):
            i = (self._rr_ptr + step) % n
            clip_uuid = self._clip_order[i]
            if clip_uuid == exclude_clip_uuid:
                continue
            shot = self._best_in_clip(
                clip_uuid, floor_rank=floor_rank, needed_seconds=needed_seconds,
                density_ratio=density_ratio)
            if shot is not None:
                self._rr_ptr = (i + 1) % n
                return shot
        return None

    def get(self, clip_uuid: str, shot_uuid: Any) -> Optional[Dict[str, Any]]:
        """The pool's own tracked copy of a shot (so advancing its cursor via
        `take` is visible to later round-robin picks)."""
        for s in self._by_clip.get(clip_uuid, []):
            if s["shot_uuid"] == shot_uuid:
                return s
        return None

    @staticmethod
    def take(shot: Dict[str, Any], seconds: float) -> Tuple[float, float]:
        """Consume `seconds` from the shot's advancing cursor; returns the
        (src_start, src_end) window actually used."""
        src_start = shot["cursor"]
        src_end = min(shot["time_seconds_end"], src_start + seconds)
        shot["cursor"] = src_end
        return src_start, src_end


def _finalize_grid_locked_frames(plan: Dict[str, Any], *, runtime_frames: int) -> None:
    """Plan-level totals for a grid-locked montage plan.

    Unlike auto_edit._assign_record_frames (talking-head's accumulate walk),
    this never touches a segment's `record_start_frame` — every segment
    already carries a beat-quantised one from the arrangement schedule, and
    re-walking would throw that alignment away. Music runs the TRACK'S OWN
    length (`runtime_frames`), not the summed cut length, since a grid-locked
    plan may leave a few trailing frames after the last beat that's short of
    one more cut.
    """
    segments = plan["segments"]
    plan["record_duration_frames"] = max(
        (seg["record_start_frame"] + (seg["source_end_frame"] - seg["source_start_frame"])
         for seg in segments), default=0)
    for overlay in plan.get("overlays") or []:
        idx = overlay.get("over_segment_index")
        if isinstance(idx, int) and 0 <= idx < len(segments):
            seg = segments[idx]
            overlay["record_start_frame"] = seg["record_start_frame"]
            overlay["record_end_frame"] = seg["record_start_frame"] + overlay["duration_frames"]
    music = plan.get("music")
    if music:
        music["record_start_frame"] = 0
        music["record_end_frame"] = runtime_frames


# ── the decision layer ───────────────────────────────────────────────────────


def build_cut_list_for_brief(
    project_root: str,
    brief: Dict[str, Any],
    *,
    min_select_potential: str = "high",
) -> Dict[str, Any]:
    """Assemble a montage CutList: hook + beat-snapped body, from analysis
    evidence. No Resolve; DB-only, same posture as
    auto_edit.build_cut_list_for_brief."""
    music_path = brief.get("music")
    errors = validate_montage_brief_inputs(
        files=brief.get("files"), music=music_path,
        target_duration_seconds=brief.get("target_duration_seconds"))
    if errors:
        return {"success": False, "error": "invalid montage brief", "problems": errors}

    beats = music_analysis.detect_beats(music_path)
    if not beats.get("available"):
        return {"success": False, "error": f"could not analyze music track: {beats.get('error')}"}
    onsets = beats.get("onsets") or []
    music_duration = float(beats.get("duration_seconds") or 0.0)
    if music_duration <= 0:
        return {"success": False, "error": "music track has no measurable duration"}
    total_runtime = music_duration
    target = brief.get("target_duration_seconds")
    if isinstance(target, (int, float)) and target > 0:
        total_runtime = min(total_runtime, float(target))

    conn = timeline_brain_db.connect(project_root)
    problems: List[str] = []
    clip_uuids: List[str] = []
    for path in brief.get("files") or []:
        clip = auto_edit._clip_for_file(conn, path)
        if not clip:
            problems.append(f"no analysis for {path!r} — analyze it first")
            continue
        clip_uuids.append(str(clip["clip_uuid"]))
    if not clip_uuids:
        return {"success": False, "error": "no analyzed candidate clips in the brief", "problems": problems}

    candidates = _candidate_shots(conn, clip_uuids)
    if not candidates:
        return {"success": False, "error": "no usable shots found for the candidate clips",
                "problems": problems}

    fps_values = {round(c["fps"], 3) for c in candidates}
    if len(fps_values) > 1:
        return {"success": False,
                "error": f"mixed frame rates in brief {sorted(fps_values)} — "
                         "montage requires a single fps"}
    fps = candidates[0]["fps"]
    tempo = beats.get("tempo_bpm")

    # Look buckets (issue #179): cluster source clips by colour signature and
    # tag every candidate with its bucket, so segments carry look_bucket
    # regardless of which cutting path (grid-locked or onset-snap) builds them.
    bucket_of_clip, look_signatures, look_bucket_basis = assign_look_buckets(candidates)
    for c in candidates:
        c["look_bucket"] = bucket_of_clip.get(c["clip_uuid"])
    match_cdls = compute_match_cdls(look_signatures, bucket_of_clip)
    if look_bucket_basis != "scout":
        problems.append(
            f"look buckets derived from {look_bucket_basis} colour data (not scout) — "
            "grades may be less precise than a scouted pass would give")

    # Hook: single highest-select_potential shot overall, prepended once.
    ranked_all = sorted(candidates, key=lambda c: -c["rank"])
    hook = ranked_all[0]

    def _rationale(shot: Dict[str, Any]) -> str:
        return f"select_potential rank {shot['rank']}, pacing={shot['pacing']}"

    def _evidence(shot: Dict[str, Any], basis: str) -> Dict[str, Any]:
        return {"basis": basis, "clip_name": shot.get("clip_name"),
                "description": shot.get("description"), "pacing": shot["pacing"]}

    grid_available = bool(beats.get("grid_available")) and len(beats.get("beat_grid") or []) >= 2
    segments: Optional[List[Dict[str, Any]]] = None

    if grid_available:
        beat_grid_seconds: List[float] = beats["beat_grid"]
        usable_beat_count = max(2, min(
            bisect.bisect_right(beat_grid_seconds, total_runtime + 1e-6), len(beat_grid_seconds)))
        trimmed_grid = beat_grid_seconds[:usable_beat_count]
        trimmed_sections = [
            s for s in (beats.get("sections") or []) if s["start_seconds"] < trimmed_grid[-1]]
        schedule = montage_arrangement.plan_arrangement(trimmed_grid, trimmed_sections)
        beat_frames = [int(round(t * fps)) for t in trimmed_grid]

        if schedule:
            def _grid_segment(role: str, shot: Dict[str, Any], src_start_seconds: float,
                               record_start_frame: int, record_len: int,
                               arrangement: Dict[str, Any]) -> Dict[str, Any]:
                start_frame = int(round(src_start_seconds * fps))
                end_frame = start_frame + record_len  # derived from record length — never re-rounded
                seg = cut_ir.make_cut_list_segment(
                    role=role, clip_id=shot["resolve_clip_id"], clip_uuid=shot["clip_uuid"],
                    source_start_frame=start_frame, source_end_frame=end_frame,
                    rationale=_rationale(shot), evidence=_evidence(shot, "select_potential+pacing+beat_grid"),
                )
                seg["record_start_frame"] = record_start_frame
                seg["beat_index"] = arrangement["beat_index"]
                seg["beat_length"] = arrangement["beat_length"]
                seg["section"] = arrangement["section"]
                seg["look_bucket"] = shot.get("look_bucket")
                seg["motion"] = None       # phase 5 (beat-locked motion) fills this in
                seg["flash"] = "flash" in arrangement["flags"]
                seg["retime"] = "retime" in arrangement["flags"]
                return seg

            pool = _ShotPool(candidates)
            tier_floor_idx = (
                _SELECT_TIERS.index(min_select_potential) if min_select_potential in _SELECT_TIERS else 0)
            truncated = False

            hook_entry = schedule[0]
            hook_end_beat = min(hook_entry["beat_index"] + hook_entry["beat_length"], len(beat_frames) - 1)
            hook_record_len = beat_frames[hook_end_beat] - beat_frames[hook_entry["beat_index"]]
            hook_internal = pool.get(hook["clip_uuid"], hook["shot_uuid"])
            hook_src_start, _ = _ShotPool.take(hook_internal, hook_record_len / fps)
            segments = [_grid_segment("montage_hook", hook, hook_src_start,
                                      beat_frames[hook_entry["beat_index"]], hook_record_len, hook_entry)]
            last_clip_uuid = hook["clip_uuid"]

            max_density = max(
                (local_onset_density(onsets, t) for t in trimmed_grid), default=0.0) or 1.0

            for entry in schedule[1:]:
                k = entry["beat_index"]
                end_k = min(k + entry["beat_length"], len(beat_frames) - 1)
                record_start_frame = beat_frames[k]
                record_len = beat_frames[end_k] - record_start_frame
                if record_len <= 0:
                    continue
                needed_seconds = record_len / fps
                density = local_onset_density(onsets, trimmed_grid[k])
                density_ratio = min(1.0, density / max_density)

                chosen = None
                floor = tier_floor_idx
                while floor < len(_SELECT_TIERS) and chosen is None:
                    chosen = pool.pick(
                        exclude_clip_uuid=last_clip_uuid, floor_rank=_SELECT_RANK[_SELECT_TIERS[floor]],
                        needed_seconds=needed_seconds, density_ratio=density_ratio)
                    floor += 1

                if chosen is None:
                    # Round-robin couldn't be satisfied (e.g. only one clip in
                    # the whole brief) — fall back to the best-ranked eligible
                    # shot even if it repeats the previous clip, per issue #177.
                    floor = tier_floor_idx
                    while floor < len(_SELECT_TIERS) and chosen is None:
                        chosen = pool.pick(
                            exclude_clip_uuid=None, floor_rank=_SELECT_RANK[_SELECT_TIERS[floor]],
                            needed_seconds=needed_seconds, density_ratio=density_ratio)
                        floor += 1

                if chosen is None:
                    truncated = True
                    break

                src_start, _ = _ShotPool.take(chosen, needed_seconds)
                segments.append(_grid_segment(
                    "montage", chosen, src_start, record_start_frame, record_len, entry))
                last_clip_uuid = chosen["clip_uuid"]

            if truncated:
                problems.append(
                    f"ran out of candidate shots/windows at select_potential>="
                    f"{_SELECT_TIERS[tier_floor_idx]} before filling the music's "
                    f"{total_runtime:.1f}s runtime — montage ends early rather than repeating "
                    "a window or fabricating coverage")
        else:
            grid_available = False  # nothing schedulable at this runtime; fall through

    if segments is None:
        # Honest degradation (phase 1's grid_available: False, or a schedule
        # that came back empty) — the original onset-snap behaviour, never a
        # fabricated grid.
        if not beats.get("grid_available"):
            problems.append(
                "beat grid unavailable (tempo confidence too low, or too few beats for this "
                "runtime) — falling back to onset-snap cutting rather than inventing a grid")

        pool_list = [c for c in candidates if c is not hook]
        hook_seconds = (HOOK_BEATS * 60.0 / tempo) if tempo else DEFAULT_HOOK_SECONDS
        hook_seconds = max(MIN_CUT_SECONDS, min(hook_seconds, hook["duration_seconds"], total_runtime))

        def _segment(role: str, shot: Dict[str, Any], src_start: float, src_end: float) -> Dict[str, Any]:
            start_frame = int(round(src_start * fps))
            end_frame = max(start_frame + 1, int(round(src_end * fps)))
            seg = cut_ir.make_cut_list_segment(
                role=role, clip_id=shot["resolve_clip_id"], clip_uuid=shot["clip_uuid"],
                source_start_frame=start_frame, source_end_frame=end_frame,
                rationale=_rationale(shot), evidence=_evidence(shot, "select_potential+pacing"),
            )
            seg["look_bucket"] = shot.get("look_bucket")
            return seg

        hook_src_start = hook["time_seconds_start"]
        hook_src_end = min(hook["time_seconds_end"], hook_src_start + hook_seconds)
        segments = [_segment("montage_hook", hook, hook_src_start, hook_src_end)]
        used_shot_uuids = {hook["shot_uuid"]}
        record_cursor = hook_src_end - hook_src_start

        sample_points = [i * 0.5 for i in range(int(total_runtime / 0.5) + 2)]
        max_density = max(
            (local_onset_density(onsets, t) for t in sample_points), default=0.0) or 1.0

        tier_floor_idx = _SELECT_TIERS.index(min_select_potential) if min_select_potential in _SELECT_TIERS else 0
        truncated = False

        while record_cursor < total_runtime - 1e-6:
            density = local_onset_density(onsets, record_cursor)
            density_ratio = min(1.0, density / max_density)
            target_dur = min(target_cut_seconds(density, max_density=max_density),
                              total_runtime - record_cursor)
            if target_dur < MIN_CUT_SECONDS and (total_runtime - record_cursor) < MIN_CUT_SECONDS:
                break  # remaining gap too small to bother with

            chosen = None
            floor = tier_floor_idx
            while floor < len(_SELECT_TIERS) and chosen is None:
                floor_rank = _SELECT_RANK[_SELECT_TIERS[floor]]
                available = [
                    c for c in pool_list
                    if c["shot_uuid"] not in used_shot_uuids
                    and c["rank"] >= floor_rank
                    and c["duration_seconds"] >= MIN_CUT_SECONDS
                ]
                zone_matches = [c for c in available if shot_fits_zone(c["pacing"], density_ratio)]
                pick_from = zone_matches or available  # tier/duration beats an exact zone match once loosened
                if pick_from:
                    pick_from.sort(key=lambda c: (-c["rank"], -c["duration_seconds"]))
                    chosen = pick_from[0]
                    break
                floor += 1

            if chosen is None:
                truncated = True
                break

            used_shot_uuids.add(chosen["shot_uuid"])
            src_start = chosen["time_seconds_start"]
            raw_src_end = min(chosen["time_seconds_end"], src_start + target_dur)
            target_record_end = record_cursor + (raw_src_end - src_start)
            snapped_record_end = min(
                nearest_onset(onsets, target_record_end, minimum=record_cursor + MIN_CUT_SECONDS),
                total_runtime)
            actual_duration = max(MIN_CUT_SECONDS, snapped_record_end - record_cursor)
            src_end = min(chosen["time_seconds_end"], src_start + actual_duration)

            segments.append(_segment("montage", chosen, src_start, src_end))
            record_cursor += (src_end - src_start)

        if truncated:
            problems.append(
                f"ran out of candidate shots at select_potential>={_SELECT_TIERS[tier_floor_idx]} "
                f"before filling the music's {total_runtime:.1f}s runtime — montage ends early "
                "rather than repeating a shot or fabricating coverage")

    if len(segments) < 2:
        return {"success": False, "error": "not enough distinct shots to build a montage",
                "problems": problems}

    music = {
        "path": music_path,
        "track_index": 2,
        "ducking": {"mode": cut_ir.DUCKING_STATIC, "user_approved_render": False},
    }
    plan = cut_ir.make_cut_list(
        segments=segments, fps=fps, music=music, brief_id=brief.get("plan_id"), revision=0)
    plan["basis"] = "select_potential+pacing+beat_grid" if grid_available else "select_potential+pacing+beat_snap"
    plan["problems"] = problems
    plan["tempo_bpm"] = tempo
    plan["onset_count"] = len(onsets)
    plan["grid_available"] = grid_available
    # Suggested per-bucket match CDLs (issue #179) — a stage-1 starting point
    # for finish(grade={"match": ..., <shared look>}); the caller may take
    # these as-is or override them before applying.
    plan["look_buckets"] = match_cdls
    plan["look_bucket_basis"] = look_bucket_basis
    if grid_available:
        # Grid-locked segments already carry a correct, beat-quantised
        # record_start_frame from the arrangement schedule — re-walking (as
        # auto_edit._assign_record_frames does for talking-head) would throw
        # that alignment away, so only the plan-level totals are finalized.
        _finalize_grid_locked_frames(plan, runtime_frames=int(round(total_runtime * fps)))
    else:
        # record_start_frame is what build_timeline's shared executor actually
        # reads to place each segment — without it every segment defaults to 0
        # and stacks on top of the last. Reused verbatim (generic cursor walk,
        # not talking-head-specific) so the executor and this plan agree.
        auto_edit._assign_record_frames(plan)
    errors = cut_ir.validate_cut_list(plan)
    if errors:
        return {"success": False, "error": "generated CutList failed validation", "problems": errors}
    plan = edit_engine.save_plan(project_root, plan)
    return {"success": True, "plan": plan, "plan_id": plan["plan_id"]}


# ── checkpoint summary ───────────────────────────────────────────────────────


def render_montage_summary(plan: Dict[str, Any]) -> str:
    """Human-readable cut list for THE approval checkpoint (markdown).

    Mirrors auto_edit.render_cut_summary's shape, adapted to montage's
    fields: no transcript excerpt/smoothing columns (montage has neither),
    a description/pacing column instead, plus the beat-grid stats
    (tempo/onset count) auto_edit's talking-head plans don't carry."""
    fps = float(plan.get("fps") or 24.0)

    def tc(frames: int) -> str:
        seconds = frames / fps
        return f"{int(seconds // 60):d}:{seconds % 60:05.2f}"

    est = plan.get("estimates") or {}
    tempo = plan.get("tempo_bpm")
    lines = [
        f"# Montage cut list — revision {plan.get('revision', 0)} (`{plan.get('plan_id', 'unsaved')}`)",
        "",
        f"**Runtime:** ~{est.get('duration_seconds')}s "
        f"({est.get('duration_frames')} frames @ {fps:g} fps) · "
        f"**Segments:** {est.get('segment_count')} · "
        f"**Tempo:** {f'{tempo:.0f} BPM' if tempo else 'unknown'} · "
        f"**Onsets detected:** {plan.get('onset_count', 0)}",
        "",
    ]
    grid_available = bool(plan.get("grid_available"))
    if grid_available:
        lines += [
            "| # | Record | Source (frames) | Role | Section | Beats | Description | Pacing |",
            "|---|--------|-----------------|------|---------|-------|--------------|--------|",
        ]
    else:
        lines += [
            "| # | Record | Source (frames) | Role | Description | Pacing |",
            "|---|--------|-----------------|------|--------------|--------|",
        ]
    for i, seg in enumerate(plan.get("segments") or []):
        evidence = seg.get("evidence") or {}
        pacing = evidence.get("pacing") or ""
        description = evidence.get("description") or ""
        row = (
            f"| {i} | {tc(seg.get('record_start_frame', 0))} "
            f"| {seg['source_start_frame']}–{seg['source_end_frame']} "
            f"| {seg.get('role')} "
        )
        if grid_available:
            row += f"| {seg.get('section') or '—'} | {seg.get('beat_length', '—')} "
        row += f"| {description} | {pacing or '—'} |"
        lines.append(row)
    problems = plan.get("problems") or []
    if problems:
        lines += ["", "**Notes:**"] + [f"- {p}" for p in problems]
    music = plan.get("music")
    if music:
        lines += [
            "",
            f"**Music:** {os.path.basename(str(music.get('path') or ''))} on "
            f"A{music.get('track_index', 2)}, static level (montage has no "
            "voiceover to duck under — see epic #38).",
        ]
    lines += ["", "_Approve to build; revise with structured notes (reorder/keep/drop)._"]
    return "\n".join(lines)
