"""auto_edit decision layer (semantic Pass-2 of the Cut-IR program).

Pure evidence + planning, mirroring ``edit_engine``: this module reads the
DB-canonical analysis store and produces a CutList plan with per-decision
rationale. It never imports or touches Resolve — execution (build_timeline,
finish) lives in server.py behind the confirm-token gate.

Pipeline position: ``start_brief`` analyzes media, ``plan_cut`` calls
``build_cut_list_for_brief`` here, the host shows ``render_cut_summary`` at
THE one human checkpoint (``approve_cut``), and the executor rebuilds the
timeline from the approved CutList (append-rebuild; revisions = rebuild).

Briefs and CutLists persist via ``edit_engine.save_plan``/``load_plan`` so
content fingerprints + stale-plan protection come free.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.utils import cut_ir, edit_engine, strata, timeline_brain_db

BRIEF_KIND = "auto_edit_brief"
GENRES = {"talking_head"}
BRIEF_STATES = (
    "created", "analyzing", "ready", "planned", "approved", "built", "finished",
)
_TRANSITIONS = {
    "created": {"analyzing", "ready"},
    "analyzing": {"ready"},
    "ready": {"planned"},
    "planned": {"planned", "approved"},   # planned→planned = revision
    "approved": {"planned", "built"},     # re-plan after approval = new checkpoint
    "built": {"planned", "finished"},
    "finished": set(),
}

DEFAULT_MIN_PAUSE_SECONDS = edit_engine.DEFAULT_MIN_PAUSE_SECONDS
MIN_SEGMENT_SECONDS = 0.4
BROLL_OVERLAY_SECONDS = 2.0
DEFAULT_TITLE_SECONDS = 4.0
PUNCH_IN_ZOOM = 1.12
EXCERPT_WORDS = 10

MUSIC_BED_CONSENT_LINE = (
    "Music-bed render consent: approving WITH music-bed consent renders a "
    "derivative ducked audio file (ffmpeg) under the analysis root; without "
    "consent the music keeps a static level and no derivative is created."
)


# ── brief intake + state machine ─────────────────────────────────────────────


def validate_brief_inputs(
    *,
    files: Any,
    music: Any = None,
    target_duration_seconds: Any = None,
    genre: str = "talking_head",
    deliverable: str = "youtube_1080p",
    title_text: Any = None,
) -> List[str]:
    """Pure input validation; file existence/ffprobe checks live in server.py."""
    errors: List[str] = []
    if not isinstance(files, (list, tuple)) or not files:
        errors.append("files must be a non-empty list of media paths")
    elif not all(isinstance(f, str) and f.strip() for f in files):
        errors.append("every entry in files must be a non-empty path string")
    if music is not None and (not isinstance(music, str) or not music.strip()):
        errors.append("music must be a path string when given")
    if target_duration_seconds is not None:
        if not isinstance(target_duration_seconds, (int, float)) or target_duration_seconds <= 0:
            errors.append("target_duration_seconds must be a positive number")
    if str(genre) not in GENRES:
        errors.append(f"genre {genre!r} not supported yet (Phase 1: {sorted(GENRES)})")
    if not isinstance(deliverable, str) or not deliverable.strip():
        errors.append("deliverable must be a non-empty string")
    if title_text is not None and not isinstance(title_text, str):
        errors.append("title_text must be a string when given")
    return errors


def create_brief(
    project_root: str,
    *,
    files: List[str],
    music: Optional[str] = None,
    target_duration_seconds: Optional[float] = None,
    genre: str = "talking_head",
    deliverable: str = "youtube_1080p",
    title_text: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    errors = validate_brief_inputs(
        files=files, music=music, target_duration_seconds=target_duration_seconds,
        genre=genre, deliverable=deliverable, title_text=title_text,
    )
    if errors:
        return {"success": False, "error": "invalid brief", "problems": errors}
    brief = edit_engine.save_plan(project_root, {
        "kind": BRIEF_KIND,
        "state": "created",
        "files": list(files),
        "music": music,
        "target_duration_seconds": target_duration_seconds,
        "genre": genre,
        "deliverable": deliverable,
        "title_text": title_text,
        "options": options or {},
        "latest_plan_id": None,
        "summary": f"auto_edit brief ({genre}, {len(files)} file(s))",
    })
    return {"success": True, "brief_id": brief["plan_id"], "brief": brief}


def load_brief(project_root: str, brief_id: str) -> Optional[Dict[str, Any]]:
    brief = edit_engine.load_plan(project_root, brief_id)
    if not brief or brief.get("_corrupt") or brief.get("kind") != BRIEF_KIND:
        return None
    return brief


def advance_brief(project_root: str, brief_id: str, state: str, **updates: Any) -> Dict[str, Any]:
    """Move a brief through the orchestration state machine (persisted)."""
    brief = load_brief(project_root, brief_id)
    if not brief:
        return {"success": False, "error": f"brief not found: {brief_id!r}"}
    current = brief.get("state", "created")
    if state not in BRIEF_STATES:
        return {"success": False, "error": f"unknown state {state!r}"}
    if state != current and state not in _TRANSITIONS.get(current, set()):
        return {"success": False,
                "error": f"illegal transition {current!r} -> {state!r}"}
    brief["state"] = state
    brief.update(updates)
    brief = edit_engine.save_plan(project_root, brief)
    return {"success": True, "brief": brief}


# ── evidence readers ─────────────────────────────────────────────────────────


def _clip_for_file(conn, path: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM clips WHERE file_path = ?", (path,)
    ).fetchone()
    if row is None:
        base = os.path.basename(path)
        row = conn.execute(
            "SELECT * FROM clips WHERE clip_name = ? COLLATE NOCASE", (base,)
        ).fetchone()
    return dict(row) if row is not None else None


def _transcript_rows(conn, clip_uuid: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        """
        SELECT segment_index, start_seconds, end_seconds, text
        FROM transcript_segments
        WHERE clip_uuid = ? AND start_seconds IS NOT NULL AND end_seconds IS NOT NULL
        ORDER BY start_seconds
        """,
        (clip_uuid,),
    ).fetchall()]


def _story_beat_label(conn, clip_uuid: str, start: float, end: float) -> Optional[str]:
    row = conn.execute(
        """
        SELECT label, beat_type FROM story_beats
        WHERE clip_uuid = ? AND superseded_at IS NULL
          AND start_seconds < ? AND end_seconds > ?
        ORDER BY start_seconds LIMIT 1
        """,
        (clip_uuid, end, start),
    ).fetchone()
    if row is None:
        return None
    return str(row["label"] or row["beat_type"] or "") or None


def _merge_intervals(
    intervals: Sequence[Tuple[float, float]], *, join_gap: float
) -> List[Tuple[float, float]]:
    merged: List[Tuple[float, float]] = []
    for s, e in sorted((float(s), float(e)) for s, e in intervals if e > s):
        if merged and s - merged[-1][1] < join_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _subtract_intervals(
    window: Tuple[float, float], holes: Sequence[Tuple[float, float]]
) -> List[Tuple[float, float]]:
    """Pieces of window not covered by holes (both seconds, half-open)."""
    pieces: List[Tuple[float, float]] = []
    cursor, end = window
    for hs, he in sorted(holes):
        if he <= cursor or hs >= end:
            continue
        if hs > cursor:
            pieces.append((cursor, hs))
        cursor = max(cursor, he)
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def _excerpt(words: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]],
             start: float, end: float) -> str:
    inside = [str(w["word"]) for w in words
              if w.get("start_seconds") is not None
              and start <= float(w["start_seconds"]) < end]
    if inside:
        text = " ".join(inside[:EXCERPT_WORDS])
        return text + (" …" if len(inside) > EXCERPT_WORDS else "")
    for row in rows:
        if float(row["end_seconds"]) > start and float(row["start_seconds"]) < end:
            text = str(row.get("text") or "").strip()
            if text:
                tokens = text.split()
                return " ".join(tokens[:EXCERPT_WORDS]) + (" …" if len(tokens) > EXCERPT_WORDS else "")
    return ""


# ── the decision layer ───────────────────────────────────────────────────────


def build_cut_list_for_brief(
    project_root: str,
    brief: Dict[str, Any],
    *,
    similar_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    music_gain_db: Optional[float] = None,
    min_pause_seconds: float = DEFAULT_MIN_PAUSE_SECONDS,
) -> Dict[str, Any]:
    """Assemble a CutList from analysis evidence for a talking-head brief.

    similar_fn: b-roll matcher with the ``embeddings.find_similar`` keyword
    signature (injectable for tests; defaults to the real index).
    """
    conn = timeline_brain_db.connect(project_root)
    speech_sources: List[Dict[str, Any]] = []
    broll_clips: List[Dict[str, Any]] = []
    problems: List[str] = []
    for path in brief.get("files") or []:
        clip = _clip_for_file(conn, path)
        if not clip:
            problems.append(f"no analysis for {path!r} — analyze it first")
            continue
        rows = _transcript_rows(conn, str(clip["clip_uuid"]))
        (speech_sources if rows else broll_clips).append({"clip": clip, "rows": rows})
    if not speech_sources:
        return {"success": False, "error": "no transcribed speech source in the brief",
                "problems": problems}

    if similar_fn is None:
        from src.utils import embeddings
        similar_fn = embeddings.find_similar

    segments: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    basis = None
    for source in speech_sources:
        clip, rows = source["clip"], source["rows"]
        clip_uuid = str(clip["clip_uuid"])
        fps = edit_engine._clip_fps(clip)
        words = strata.read_words(conn, clip_uuid)
        cues = [{"text": r.get("text") or "",
                 "start": int(round(float(r["start_seconds"]) * fps)),
                 "end": int(round(float(r["end_seconds"]) * fps))} for r in rows]
        detection = cut_ir.detect_cuts_auto(words, cues, fps=fps)
        basis = basis or detection["basis"]
        cuts = [c for c in detection["cuts"] if c["action"] == "lift"]
        removed.extend(cuts)
        holes = _merge_intervals(
            [(c["span"]["start"] / fps, c["span"]["end"] / fps) for c in cuts],
            join_gap=0.0,
        )
        windows = _merge_intervals(
            [(float(r["start_seconds"]), float(r["end_seconds"])) for r in rows],
            join_gap=min_pause_seconds,
        )
        for window in windows:
            pieces = _subtract_intervals(window, holes)
            for j, (s, e) in enumerate(pieces):
                if e - s < MIN_SEGMENT_SECONDS:
                    continue
                beat = _story_beat_label(conn, clip_uuid, s, e)
                rationale = f"speech {s:.2f}-{e:.2f}s"
                if beat:
                    rationale += f"; story beat: {beat}"
                segment = cut_ir.make_cut_list_segment(
                    role="speech",
                    clip_id=clip.get("resolve_clip_id"),
                    clip_uuid=clip_uuid,
                    source_start_frame=int(round(s * fps)),
                    source_end_frame=max(int(round(s * fps)) + 1, int(round(e * fps))),
                    audio_track_indices=[1],
                    jumpcut_smoothing="pending" if j > 0 else None,
                    transcript_excerpt=_excerpt(words, rows, s, e),
                    rationale=rationale,
                    evidence={"basis": detection["basis"], "clip_name": clip.get("clip_name")},
                )
                segments.append(segment)

    if not segments:
        return {"success": False, "error": "nothing to keep after Pass-1 cuts",
                "problems": problems}

    fps = edit_engine._clip_fps(speech_sources[0]["clip"])
    segments, dropped = _fit_to_duration(
        segments, fps=fps,
        target_seconds=brief.get("target_duration_seconds"),
    )
    removed.extend(dropped)

    _assign_smoothing_and_overlays(
        project_root, segments, fps=fps,
        has_broll=bool(broll_clips), similar_fn=similar_fn,
        broll_uuids={str(b["clip"]["clip_uuid"]) for b in broll_clips},
    )
    overlays = _collect_overlays(segments, fps=fps)

    titles: List[Dict[str, Any]] = []
    if brief.get("title_text"):
        titles.append({
            "text": brief["title_text"], "role": "intro", "at_frame": 0,
            "duration_frames": int(round(DEFAULT_TITLE_SECONDS * fps)),
        })

    music = None
    if brief.get("music"):
        music = {
            "path": brief["music"],
            "track_index": 2,
            "gain_db": music_gain_db,
            "ducking": {"mode": "static", "user_approved_render": False},
        }

    plan = cut_ir.make_cut_list(
        segments=segments, fps=fps, overlays=overlays, titles=titles,
        music=music, removed=removed, brief_id=brief.get("plan_id"),
        revision=0,
    )
    plan["basis"] = basis
    plan["problems"] = problems
    _assign_record_frames(plan)
    errors = cut_ir.validate_cut_list(plan)
    if errors:
        return {"success": False, "error": "generated CutList failed validation",
                "problems": errors}
    plan = edit_engine.save_plan(project_root, plan)
    return {"success": True, "plan": plan, "plan_id": plan["plan_id"]}


def _fit_to_duration(
    segments: List[Dict[str, Any]], *, fps: float, target_seconds: Optional[float]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Keep segments in story order until the duration budget is spent.

    Whole segments are dropped (never blind mid-sentence trims); drops are
    recorded as semantic Cuts so the checkpoint summary can show them.
    """
    if not target_seconds:
        return segments, []
    budget = int(round(float(target_seconds) * fps))
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    used = 0
    for seg in segments:
        length = seg["source_end_frame"] - seg["source_start_frame"]
        if used + length <= budget or not kept:
            kept.append(seg)
            used += length
        else:
            dropped.append(cut_ir.make_cut(
                "semantic", seg["source_start_frame"], seg["source_end_frame"],
                "lift", 0.6,
                f"Dropped to fit the {target_seconds:.0f}s target: "
                f"{seg.get('transcript_excerpt') or seg['rationale']!r}",
                {"reason": "duration_fit", "segment": seg},
            ))
    return kept, dropped


def _assign_smoothing_and_overlays(
    project_root: str,
    segments: List[Dict[str, Any]],
    *,
    fps: float,
    has_broll: bool,
    similar_fn: Callable[..., Dict[str, Any]],
    broll_uuids: set,
) -> None:
    """Resolve pending jump-cut smoothing: b-roll when a match exists, else punch-in."""
    for seg in segments:
        if seg.get("jumpcut_smoothing") != "pending":
            continue
        match = None
        if has_broll and seg.get("transcript_excerpt"):
            result = similar_fn(
                project_root, text=seg["transcript_excerpt"], kind="text",
                entity_types=["shot"], limit=5,
            )
            for hit in (result.get("results") or []) if result.get("success") else []:
                if str(hit.get("clip_uuid")) in broll_uuids:
                    match = hit
                    break
        if match:
            seg["jumpcut_smoothing"] = "broll"
            seg["_broll_match"] = match
        else:
            seg["jumpcut_smoothing"] = "punch_in"
            seg["punch_in"] = {"zoom": PUNCH_IN_ZOOM}


def _collect_overlays(segments: List[Dict[str, Any]], *, fps: float) -> List[Dict[str, Any]]:
    overlays: List[Dict[str, Any]] = []
    for seg in segments:
        match = seg.pop("_broll_match", None)
        if not match:
            continue
        duration = min(
            int(round(BROLL_OVERLAY_SECONDS * fps)),
            seg["source_end_frame"] - seg["source_start_frame"],
        )
        shot_start = match.get("time_seconds_start") or 0.0
        overlays.append({
            "role": "broll",
            "clip_uuid": match.get("clip_uuid"),
            "clip_name": match.get("clip_name"),
            "shot_index": match.get("shot_index"),
            "source_start_frame": int(round(float(shot_start) * fps)),
            "source_end_frame": int(round(float(shot_start) * fps)) + duration,
            "duration_frames": duration,
            "track_index": 2,
            "over_segment_index": segments.index(seg),
            "rationale": f"jump-cut cover; similarity {match.get('score')}",
        })
    return overlays


def _assign_record_frames(plan: Dict[str, Any]) -> None:
    """Walk the record cursor so the executor and summary agree on placement."""
    cursor = 0
    for seg in plan["segments"]:
        seg["record_start_frame"] = cursor
        cursor += seg["source_end_frame"] - seg["source_start_frame"]
    plan["record_duration_frames"] = cursor
    for overlay in plan["overlays"]:
        idx = overlay.get("over_segment_index")
        if isinstance(idx, int) and 0 <= idx < len(plan["segments"]):
            seg = plan["segments"][idx]
            overlay["record_start_frame"] = seg["record_start_frame"]
            overlay["record_end_frame"] = seg["record_start_frame"] + overlay["duration_frames"]
    music = plan.get("music")
    if music:
        music["record_start_frame"] = 0
        music["record_end_frame"] = cursor  # trimmed to the cut length


# ── checkpoint summary ───────────────────────────────────────────────────────


def render_cut_summary(plan: Dict[str, Any]) -> str:
    """Human-readable cut list for THE approval checkpoint (markdown)."""
    fps = float(plan.get("fps") or 24.0)

    def tc(frames: int) -> str:
        seconds = frames / fps
        return f"{int(seconds // 60):d}:{seconds % 60:05.2f}"

    est = plan.get("estimates") or {}
    lines = [
        f"# Cut list — revision {plan.get('revision', 0)} (`{plan.get('plan_id', 'unsaved')}`)",
        "",
        f"**Runtime:** ~{est.get('duration_seconds')}s "
        f"({est.get('duration_frames')} frames @ {fps:g} fps) · "
        f"**Segments:** {est.get('segment_count')} · "
        f"**Evidence basis:** {plan.get('basis') or 'words'}",
        "",
        "| # | Record | Source (frames) | Excerpt | Smoothing |",
        "|---|--------|-----------------|---------|-----------|",
    ]
    for i, seg in enumerate(plan.get("segments") or []):
        lines.append(
            f"| {i} | {tc(seg.get('record_start_frame', 0))} "
            f"| {seg['source_start_frame']}–{seg['source_end_frame']} "
            f"| {seg.get('transcript_excerpt') or seg.get('rationale') or ''} "
            f"| {seg.get('jumpcut_smoothing') or '—'} |"
        )
    removed = plan.get("removed") or []
    if removed:
        by_kind: Dict[str, int] = {}
        for cut in removed:
            by_kind[cut.get("kind", "?")] = by_kind.get(cut.get("kind", "?"), 0) + 1
        summary = ", ".join(f"{v}× {k}" for k, v in sorted(by_kind.items()))
        lines += ["", f"**Removed:** {summary}"]
    for title in plan.get("titles") or []:
        lines += ["", f"**Title:** “{title.get('text')}” ({title.get('role', 'intro')})"]
    overlays = plan.get("overlays") or []
    if overlays:
        lines += ["", f"**B-roll overlays:** {len(overlays)} on V2"]
    music = plan.get("music")
    if music:
        gain = music.get("gain_db")
        ducking = (music.get("ducking") or {})
        lines += [
            "",
            f"**Music:** {os.path.basename(str(music.get('path') or ''))} on A{music.get('track_index', 2)}, "
            f"trimmed to the cut; gain {gain if gain is not None else 'static default'} dB; "
            f"ducking mode: {ducking.get('mode')}",
            "",
            f"> {MUSIC_BED_CONSENT_LINE}",
        ]
    lines += ["", "_Approve to build; revise with structured notes (reorder/keep/drop/title)._"]
    return "\n".join(lines)


# ── revisions + approval ─────────────────────────────────────────────────────


def apply_revision(
    project_root: str,
    plan_id: str,
    *,
    notes: str = "",
    edits: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Structured overrides on a CutList: reorder / keep / drop / title.

    Produces revision+1 as a NEW saved plan (append-rebuild: old revisions
    stay loadable); the caller re-shows the checkpoint for the new revision.
    """
    plan = edit_engine.load_plan(project_root, plan_id)
    if not plan or plan.get("_corrupt") or plan.get("kind") != cut_ir.CUT_LIST_KIND:
        return {"success": False, "error": f"cut list not found: {plan_id!r}"}
    segments = list(plan.get("segments") or [])
    removed = list(plan.get("removed") or [])
    titles = list(plan.get("titles") or [])
    for edit in edits or []:
        op = str(edit.get("op") or "")
        if op == "reorder":
            order = edit.get("order")
            if (not isinstance(order, list)
                    or sorted(order) != list(range(len(segments)))):
                return {"success": False,
                        "error": f"reorder.order must be a permutation of 0..{len(segments) - 1}"}
            segments = [segments[i] for i in order]
        elif op == "drop":
            idx = edit.get("index")
            if not isinstance(idx, int) or not 0 <= idx < len(segments):
                return {"success": False, "error": f"drop.index out of range: {idx!r}"}
            seg = segments.pop(idx)
            removed.append(cut_ir.make_cut(
                "semantic", seg["source_start_frame"], seg["source_end_frame"],
                "lift", 1.0, f"Dropped at revision: {notes or 'no note'}",
                {"reason": "revision_drop", "segment": seg},
            ))
        elif op == "keep":
            idx = edit.get("index")
            restored = None
            for i, cut in enumerate(removed):
                if (cut.get("evidence") or {}).get("segment") is not None and (
                        idx is None or i == idx):
                    restored = removed.pop(i)["evidence"]["segment"]
                    break
            if restored is None:
                return {"success": False,
                        "error": "keep found no dropped segment to restore"}
            segments.append(restored)
            segments.sort(key=lambda s: (str(s.get("clip_uuid")), s["source_start_frame"]))
        elif op == "title":
            text = str(edit.get("text") or "").strip()
            if not text:
                return {"success": False, "error": "title.text must be non-empty"}
            if titles:
                titles[0] = dict(titles[0], text=text)
            else:
                fps = float(plan.get("fps") or 24.0)
                titles.append({"text": text, "role": "intro", "at_frame": 0,
                               "duration_frames": int(round(DEFAULT_TITLE_SECONDS * fps))})
        else:
            return {"success": False, "error": f"unknown revision op {op!r}"}
    if not segments:
        return {"success": False, "error": "revision would leave no segments"}

    revised = dict(plan)
    revised.pop("plan_id", None)
    revised.pop("fingerprint", None)
    revised.pop("saved_at", None)
    revised.pop("approved_at", None)
    revised.update({
        "segments": segments, "removed": removed, "titles": titles,
        "revision": int(plan.get("revision") or 0) + 1,
        "revision_notes": notes,
        "revised_from": plan_id,
        "estimates": cut_ir.compute_cut_list_estimates(segments, float(plan.get("fps") or 24.0)),
    })
    _assign_record_frames(revised)
    errors = cut_ir.validate_cut_list(revised)
    if errors:
        return {"success": False, "error": "revised CutList failed validation",
                "problems": errors}
    revised = edit_engine.save_plan(project_root, revised)
    return {"success": True, "plan": revised, "plan_id": revised["plan_id"]}


def mark_approved(
    project_root: str,
    plan_id: str,
    *,
    music_bed_consent: bool = False,
) -> Dict[str, Any]:
    """Record checkpoint approval (and the music-bed consent decision).

    The confirm-token ceremony itself lives in server.py; this persists the
    outcome so the executor can trust ``approved_at`` + the ducking mode.
    """
    plan = edit_engine.load_plan(project_root, plan_id)
    if not plan or plan.get("_corrupt") or plan.get("kind") != cut_ir.CUT_LIST_KIND:
        return {"success": False, "error": f"cut list not found: {plan_id!r}"}
    plan["approved_at"] = edit_engine._now()
    music = plan.get("music")
    if music:
        ducking = dict(music.get("ducking") or {})
        ducking["user_approved_render"] = bool(music_bed_consent)
        ducking["mode"] = "rendered_bed" if music_bed_consent else "static"
        music["ducking"] = ducking
    plan = edit_engine.save_plan(project_root, plan)
    return {"success": True, "plan": plan, "plan_id": plan["plan_id"]}


def require_approved_plan(project_root: str, plan_id: str) -> Dict[str, Any]:
    """Executor gate: load a CutList and insist it is approved + intact."""
    plan = edit_engine.load_plan(project_root, plan_id)
    if not plan:
        return {"success": False, "error": f"cut list not found: {plan_id!r}"}
    if plan.get("_corrupt"):
        return {"success": False,
                "error": "cut list fingerprint mismatch — re-plan before building"}
    if plan.get("kind") != cut_ir.CUT_LIST_KIND:
        return {"success": False, "error": f"plan {plan_id!r} is not a CutList"}
    if not plan.get("approved_at"):
        return {"success": False,
                "error": "cut list is not approved — approve_cut is the checkpoint"}
    return {"success": True, "plan": plan}
