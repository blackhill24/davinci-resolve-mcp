"""Cut intermediate representation (Cut-IR) and the mechanical (Pass-1) detector.

The Cut-IR is the typed contract between a timestamped transcript and concrete,
governed timeline operations. Producers emit Cuts; an executor (a later phase)
consumes them as governed, versioned edits; a review UI shows them. This module
provides the schema plus the deterministic Pass-1 detectors — cue-level (filler
cues, long pauses, repeated lines) and word-level (fillers, false starts over
``transcript_words`` timings) — with no LLM. The semantic Pass-2 decision layer
lives in ``src/utils/auto_edit.py``; the timeline executor is the ``auto_edit``
compound tool in server.py.

A Cut:
    {
      "kind": "filler" | "long_pause" | "stammer" | "false_start" | "semantic",
      "span": {"start": <frame>, "end": <frame>},   # half-open [start, end)
      "action": "lift" | "ripple_delete" | "keep" | "reorder" | "swap",
      "confidence": 0.0..1.0,
      "rationale": str,
      "evidence": {...},
    }

A CutList (kind="auto_edit_cut") extends Cut-IR into a full build plan:
ordered ``segments`` (role, clip identity, half-open source frames, audio
mirroring, optional punch-in / transition / jump-cut smoothing), ``overlays``,
``titles``, ``music`` (with ``ducking`` consent state), the ``removed`` Cuts
that justify the tightened runtime, and ``estimates``. It persists via
``edit_engine.save_plan``/``load_plan`` so fingerprint + stale-plan protection
come free.
"""
from typing import Any, Dict, List, Optional, Sequence

# Common English fillers (single tokens and short phrases).
FILLER_WORDS = {
    "um", "uh", "er", "ah", "eh", "hmm", "mm", "uhh", "umm",
    "like", "so", "well", "right", "okay", "ok",
}
FILLER_PHRASES = {"you know", "i mean", "sort of", "kind of", "you see"}


def make_cut(
    kind: str,
    start: Optional[int],
    end: Optional[int],
    action: str,
    confidence: float,
    rationale: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "span": {"start": start, "end": end},
        "action": action,
        "confidence": round(float(confidence), 2),
        "rationale": rationale,
        "evidence": evidence or {},
    }


def _norm(text: str) -> str:
    return (text or "").strip().lower().strip(".,!?;:")


def _is_filler_only(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    if low in FILLER_PHRASES:
        return True
    tokens = [t.strip(".,!?;:") for t in low.split()]
    tokens = [t for t in tokens if t]
    return bool(tokens) and all(t in FILLER_WORDS for t in tokens)


def detect_cuts_pass1(
    cues: List[Dict[str, Any]],
    *,
    long_pause_frames: int = 48,
) -> List[Dict[str, Any]]:
    """Mechanical Pass-1 detection over timestamped cues.

    cues: list of {"text", "start", "end"} in frames. Returns a list of Cuts.
    """
    cuts: List[Dict[str, Any]] = []
    for i, cue in enumerate(cues):
        text = (cue.get("text") or "").strip()
        start, end = cue.get("start"), cue.get("end")

        if _is_filler_only(text):
            cuts.append(make_cut(
                "filler", start, end, "lift", 0.8,
                f"Filler-only cue: {text!r}", {"text": text},
            ))

        if i > 0:
            prev_text = (cues[i - 1].get("text") or "").strip()
            if _norm(prev_text) and _norm(text) == _norm(prev_text):
                cuts.append(make_cut(
                    "stammer", start, end, "lift", 0.6,
                    f"Repeated line: {text!r}", {"text": text},
                ))
            prev_end = cues[i - 1].get("end")
            if (prev_end is not None and start is not None
                    and start - prev_end > long_pause_frames):
                cuts.append(make_cut(
                    "long_pause", prev_end, start, "lift", 0.5,
                    f"Pause of {start - prev_end} frames before {text!r}",
                    {"frames": start - prev_end},
                ))
    return cuts


def build_cut_list(
    cues: List[Dict[str, Any]],
    *,
    long_pause_frames: int = 48,
) -> Dict[str, Any]:
    """Run Pass-1 and wrap the result as a (dry-run) CutList."""
    cuts = detect_cuts_pass1(cues, long_pause_frames=long_pause_frames)
    return {
        "cuts": cuts,
        "cut_count": len(cuts),
        "basis_cue_count": len(cues),
        "pass": "mechanical",
        "note": "Dry-run proposal. Review before applying; no edits were made.",
    }


# ── word-level Pass-1 (fillers + false starts over transcript_words) ─────────

# Hesitation sounds are safe to cut wherever they occur; discourse words
# ("like", "so", …) are only cut when spoken as an isolated interjection.
HESITATION_WORDS = {"um", "uh", "er", "ah", "eh", "hmm", "mm", "uhh", "umm"}
DISCOURSE_WORDS = FILLER_WORDS - HESITATION_WORDS
ISOLATED_GAP_SECONDS = 0.25  # silence on both sides that marks an interjection
MAX_FALSE_START_WORDS = 4


def _word_seconds(word: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Extract {start, end} seconds from a word row (strata or whisper shape)."""
    start = word.get("start_seconds", word.get("start"))
    end = word.get("end_seconds", word.get("end"))
    if not isinstance(start, (int, float)):
        return None
    if not isinstance(end, (int, float)):
        end = start
    return {"start": float(start), "end": float(end)}


def _word_span_frames(
    words: Sequence[Dict[str, Any]], fps: float
) -> Optional[Dict[str, int]]:
    """Half-open frame span covering a run of word rows."""
    first, last = _word_seconds(words[0]), _word_seconds(words[-1])
    if first is None or last is None:
        return None
    start = int(round(first["start"] * fps))
    end = max(start + 1, int(round(last["end"] * fps)))
    return {"start": start, "end": end}


def _word_text(word: Dict[str, Any]) -> str:
    return str(word.get("word", word.get("text", "")) or "").strip()


def detect_cuts_words(
    words: Sequence[Dict[str, Any]],
    *,
    fps: float,
    max_false_start_words: int = MAX_FALSE_START_WORDS,
) -> List[Dict[str, Any]]:
    """Word-level Pass-1 over transcript_words rows (fillers + false starts).

    words: rows shaped like ``strata.read_words`` output ({"word",
    "start_seconds", "end_seconds", …}) or normalized whisper words ({"word",
    "start", "end"} in seconds). Frame spans are half-open at ``fps``.
    """
    rows = [w for w in words if _word_text(w) and _word_seconds(w) is not None]
    norms = [_norm(_word_text(w)) for w in rows]
    cuts: List[Dict[str, Any]] = []
    consumed = [False] * len(rows)

    def emit(kind: str, run: Sequence[Dict[str, Any]], confidence: float,
             rationale: str, evidence: Dict[str, Any]) -> None:
        span = _word_span_frames(run, fps)
        if span is not None:
            cuts.append(make_cut(kind, span["start"], span["end"], "lift",
                                 confidence, rationale, evidence))

    # False starts: a run of 1..N words immediately repeated restates the
    # phrase — cut the first occurrence. A single repeated word is a stammer.
    i = 0
    while i < len(rows):
        matched = 0
        for n in range(min(max_false_start_words, (len(rows) - i) // 2), 0, -1):
            if norms[i:i + n] == norms[i + n:i + 2 * n]:
                phrase = " ".join(norms[i:i + n])
                if n == 1 and phrase in FILLER_WORDS:
                    break  # repeated fillers are handled by the filler pass
                kind = "stammer" if n == 1 else "false_start"
                emit(kind, rows[i:i + n], 0.75,
                     f"Restarted phrase: {phrase!r}", {"words": phrase, "repeat_len": n})
                for j in range(i, i + n):
                    consumed[j] = True
                matched = n
                break
        i += matched or 1

    for idx, (row, low) in enumerate(zip(rows, norms)):
        if consumed[idx]:
            continue
        text = _word_text(row)
        # Aborted words ("wor-") read as false starts even without a repeat.
        if text.endswith("-") and len(text) > 1:
            emit("false_start", [row], 0.7,
                 f"Aborted word: {text!r}", {"word": text})
            continue
        if low in HESITATION_WORDS:
            emit("filler", [row], 0.85, f"Hesitation: {text!r}", {"word": text})
        elif low in DISCOURSE_WORDS and _is_isolated_word(rows, idx):
            emit("filler", [row], 0.5,
                 f"Isolated discourse filler: {text!r}", {"word": text})
        elif idx + 1 < len(rows) and not consumed[idx + 1]:
            phrase = f"{low} {norms[idx + 1]}"
            if phrase in FILLER_PHRASES:
                emit("filler", rows[idx:idx + 2], 0.6,
                     f"Filler phrase: {phrase!r}", {"phrase": phrase})
                consumed[idx + 1] = True

    cuts.sort(key=lambda c: (c["span"]["start"], c["span"]["end"]))
    return cuts


def _is_isolated_word(rows: Sequence[Dict[str, Any]], idx: int) -> bool:
    """True when silence of ISOLATED_GAP_SECONDS+ flanks the word."""
    cur = _word_seconds(rows[idx])
    if cur is None:
        return False
    if idx > 0:
        prev = _word_seconds(rows[idx - 1])
        if prev is not None and cur["start"] - prev["end"] < ISOLATED_GAP_SECONDS:
            return False
    if idx + 1 < len(rows):
        nxt = _word_seconds(rows[idx + 1])
        if nxt is not None and nxt["start"] - cur["end"] < ISOLATED_GAP_SECONDS:
            return False
    return True


def detect_cuts_auto(
    words: Sequence[Dict[str, Any]],
    cues: List[Dict[str, Any]],
    *,
    fps: float,
    long_pause_frames: int = 48,
) -> Dict[str, Any]:
    """Word-level Pass-1 when word timings exist, else cue-level fallback.

    Graceful degradation per the configured Whisper backend: backends without
    word timestamps still get filler/pause/repeat detection over cues.
    """
    usable = [w for w in words or [] if _word_text(w) and _word_seconds(w) is not None]
    if usable:
        cuts = detect_cuts_words(usable, fps=fps)
        return {"cuts": cuts, "basis": "words", "basis_word_count": len(usable)}
    cuts = detect_cuts_pass1(cues or [], long_pause_frames=long_pause_frames)
    return {"cuts": cuts, "basis": "cues", "basis_cue_count": len(cues or [])}


# ── CutList schema (kind="auto_edit_cut") ────────────────────────────────────

CUT_LIST_KIND = "auto_edit_cut"
SEGMENT_ROLES = {"intro", "speech", "broll", "outro"}
DUCKING_MODES = {"none", "static", "rendered_bed"}


def make_cut_list_segment(
    *,
    role: str,
    clip_id: Optional[str] = None,
    clip_uuid: Optional[str] = None,
    source_start_frame: int,
    source_end_frame: int,
    audio_track_indices: Optional[List[int]] = None,
    jumpcut_smoothing: Optional[str] = None,
    punch_in: Optional[Dict[str, Any]] = None,
    transition_in: Optional[Dict[str, Any]] = None,
    transcript_excerpt: str = "",
    rationale: str = "",
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One ordered CutList segment; source frames are half-open [start, end)."""
    segment: Dict[str, Any] = {
        "role": role,
        "clip_id": clip_id,
        "clip_uuid": clip_uuid,
        "source_start_frame": int(source_start_frame),
        "source_end_frame": int(source_end_frame),
        "audio_track_indices": list(audio_track_indices or []),
        "transcript_excerpt": transcript_excerpt,
        "rationale": rationale,
        "evidence": evidence or {},
    }
    if jumpcut_smoothing:
        segment["jumpcut_smoothing"] = jumpcut_smoothing
    if punch_in:
        segment["punch_in"] = punch_in
    if transition_in:
        segment["transition_in"] = transition_in
    return segment


def compute_cut_list_estimates(
    segments: Sequence[Dict[str, Any]], fps: float
) -> Dict[str, Any]:
    frames = sum(
        max(0, int(seg.get("source_end_frame", 0)) - int(seg.get("source_start_frame", 0)))
        for seg in segments
        if seg.get("role") != "broll"  # overlays don't extend the runtime
    )
    return {
        "duration_frames": frames,
        "duration_seconds": round(frames / fps, 2) if fps else None,
        "segment_count": len(segments),
    }


def make_cut_list(
    *,
    segments: List[Dict[str, Any]],
    fps: float,
    overlays: Optional[List[Dict[str, Any]]] = None,
    titles: Optional[List[Dict[str, Any]]] = None,
    music: Optional[Dict[str, Any]] = None,
    removed: Optional[List[Dict[str, Any]]] = None,
    brief_id: Optional[str] = None,
    revision: int = 0,
) -> Dict[str, Any]:
    """Assemble a CutList plan dict, ready for ``edit_engine.save_plan``."""
    if music is not None:
        music = dict(music)
        ducking = dict(music.get("ducking") or {})
        ducking.setdefault("mode", "static")
        ducking.setdefault("user_approved_render", False)
        music["ducking"] = ducking
    return {
        "kind": CUT_LIST_KIND,
        "brief_id": brief_id,
        "revision": int(revision),
        "fps": float(fps),
        "segments": segments,
        "overlays": overlays or [],
        "titles": titles or [],
        "music": music,
        "removed": removed or [],
        "estimates": compute_cut_list_estimates(segments, fps),
    }


def validate_cut_list(plan: Dict[str, Any]) -> List[str]:
    """Return a list of problems; an empty list means the CutList is valid."""
    errors: List[str] = []
    if not isinstance(plan, dict):
        return ["cut list is not a dict"]
    if plan.get("kind") != CUT_LIST_KIND:
        errors.append(f"kind must be {CUT_LIST_KIND!r}, got {plan.get('kind')!r}")
    fps = plan.get("fps")
    if not isinstance(fps, (int, float)) or fps <= 0:
        errors.append("fps must be a positive number")
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        errors.append("segments must be a non-empty list")
        segments = []
    for i, seg in enumerate(segments):
        where = f"segments[{i}]"
        if not isinstance(seg, dict):
            errors.append(f"{where} is not a dict")
            continue
        if seg.get("role") not in SEGMENT_ROLES:
            errors.append(f"{where}.role {seg.get('role')!r} not in {sorted(SEGMENT_ROLES)}")
        if not seg.get("clip_id") and not seg.get("clip_uuid"):
            errors.append(f"{where} needs clip_id or clip_uuid")
        start, end = seg.get("source_start_frame"), seg.get("source_end_frame")
        if not isinstance(start, int) or not isinstance(end, int) or end <= start:
            errors.append(f"{where} frames must be ints with end > start (half-open), got {start!r}..{end!r}")
        if not isinstance(seg.get("audio_track_indices", []), list):
            errors.append(f"{where}.audio_track_indices must be a list")
    for i, title in enumerate(plan.get("titles") or []):
        if not isinstance(title, dict) or not title.get("text"):
            errors.append(f"titles[{i}] needs text")
    music = plan.get("music")
    if music is not None:
        if not isinstance(music, dict):
            errors.append("music must be a dict")
        else:
            if not music.get("clip_id") and not music.get("clip_uuid") and not music.get("path"):
                errors.append("music needs clip_id, clip_uuid, or path")
            ducking = music.get("ducking")
            if not isinstance(ducking, dict):
                errors.append("music.ducking must be a dict")
            else:
                if ducking.get("mode") not in DUCKING_MODES:
                    errors.append(f"music.ducking.mode {ducking.get('mode')!r} not in {sorted(DUCKING_MODES)}")
                if not isinstance(ducking.get("user_approved_render", False), bool):
                    errors.append("music.ducking.user_approved_render must be a bool")
    for i, cut in enumerate(plan.get("removed") or []):
        span = cut.get("span") if isinstance(cut, dict) else None
        if not isinstance(span, dict) or span.get("start") is None or span.get("end") is None:
            errors.append(f"removed[{i}] needs a span with start/end")
    return errors
