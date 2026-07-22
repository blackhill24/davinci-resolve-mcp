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

Cut timing is a hybrid, not a rigid beat grid:
  - PACING (how long a cut holds at each point in the track) comes from
    local onset DENSITY around that point — dense onsets nearby read as
    a high-energy section (shorter target cut), sparse onsets read as
    mellow (longer target cut). This reuses the SAME onset list
    music_analysis.detect_beats already returns for snap-to-beat, so no
    new DSP is needed for a second "energy" signal.
  - PLACEMENT (which candidate shot goes at that point) comes from each
    shot's own `pacing` classification (still/moderate/kinetic/variable —
    the PER-SHOT signal in the analysis schema's shot_descriptions[i].editorial;
    `energy_arc` sits one level up, at editorial_classification/cross_shot,
    describing the whole CLIP's arc, not a per-shot value, so it can't drive
    per-shot placement) — kinetic shots slot into high-density regions,
    still shots into low-density ones, moderate/variable fit anywhere.
  - Every actual cut boundary snaps to the NEAREST real onset at or after
    the running cursor, never a mathematical beat count.

Shot exhaustion: the select_potential floor loosens high -> medium -> low
(mirrors edit_engine.plan_selects' own tunable) to keep filling the
music's runtime; if even "low" runs dry the montage TRUNCATES rather
than repeating a shot or fabricating coverage, and says so honestly in
`problems`.

No voiceover/ducking concept in v1 — strictly B-roll + music. Music is
required (its length sets the montage's runtime; target_duration_seconds,
if given, trims it rather than replacing it as the primary driver).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence

from src.utils import auto_edit, cut_ir, edit_engine, music_analysis, timeline_brain_db

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
            "shot_uuid": shot.get("shot_uuid"),
            "shot_index": shot["shot_index"],
            "time_seconds_start": float(start),
            "time_seconds_end": float(end),
            "duration_seconds": round(float(end) - float(start), 3),
            "fps": edit_engine._clip_fps(clip),
            "rank": rank,
            "pacing": pacing,
            "description": shot.get("description"),
        })
    return candidates


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

    # Hook: single highest-select_potential shot overall, prepended once and
    # withdrawn from the general pool.
    ranked_all = sorted(candidates, key=lambda c: -c["rank"])
    hook = ranked_all[0]
    pool = [c for c in candidates if c is not hook]

    tempo = beats.get("tempo_bpm")
    hook_seconds = (HOOK_BEATS * 60.0 / tempo) if tempo else DEFAULT_HOOK_SECONDS
    hook_seconds = max(MIN_CUT_SECONDS, min(hook_seconds, hook["duration_seconds"], total_runtime))

    segments: List[Dict[str, Any]] = []
    used_shot_uuids = set()

    def _segment(role: str, shot: Dict[str, Any], src_start: float, src_end: float) -> Dict[str, Any]:
        start_frame = int(round(src_start * fps))
        end_frame = max(start_frame + 1, int(round(src_end * fps)))
        return cut_ir.make_cut_list_segment(
            role=role, clip_id=shot["resolve_clip_id"], clip_uuid=shot["clip_uuid"],
            source_start_frame=start_frame, source_end_frame=end_frame,
            rationale=f"select_potential rank {shot['rank']}, pacing={shot['pacing']}",
            evidence={"basis": "select_potential+pacing", "clip_name": shot.get("clip_name"),
                      "description": shot.get("description"), "pacing": shot["pacing"]},
        )

    hook_src_start = hook["time_seconds_start"]
    hook_src_end = min(hook["time_seconds_end"], hook_src_start + hook_seconds)
    segments.append(_segment("montage_hook", hook, hook_src_start, hook_src_end))
    used_shot_uuids.add(hook["shot_uuid"])
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
                c for c in pool
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
    plan["basis"] = "select_potential+pacing+beat_snap"
    plan["problems"] = problems
    plan["tempo_bpm"] = tempo
    plan["onset_count"] = len(onsets)
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
        "| # | Record | Source (frames) | Role | Description | Pacing |",
        "|---|--------|-----------------|------|--------------|--------|",
    ]
    for i, seg in enumerate(plan.get("segments") or []):
        evidence = seg.get("evidence") or {}
        pacing = evidence.get("pacing") or ""
        description = evidence.get("description") or ""
        lines.append(
            f"| {i} | {tc(seg.get('record_start_frame', 0))} "
            f"| {seg['source_start_frame']}–{seg['source_end_frame']} "
            f"| {seg.get('role')} "
            f"| {description} "
            f"| {pacing or '—'} |"
        )
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
