"""montage_arrangement — turns phase 1's section/beat-grid evidence into a
beat-indexed cutting schedule (issue #177, phase 2/6 of the montage-quality
epic).

Pure planning: no I/O, no Resolve, no ffmpeg, no DB. Input is exactly what
``music_analysis.detect_beats`` already computes (``beat_grid`` + ``sections``);
output is an ordered list of ``{beat_index, beat_length, section, role,
flags}`` covering the whole grid with no gaps and no overlaps, for
``montage_edit`` to walk. Cut length is now a property of the ARRANGEMENT
(the musical structure), not of local onset density — see
``src/domains/auto_edit/CONTEXT.md`` for how this fits the montage_edit /
montage_arrangement split.
"""
from __future__ import annotations

import bisect
from typing import Any, Dict, List, Optional

# Section label -> base cut length in beats. "build" ramps 4->2 across the
# section instead of using this flat value (see _section_schedule); every
# other label cuts at a constant length. Values mirror the table in issue
# #177: intro holds longest, mid/high/drop are the 2-beat "body" cadence,
# breathe/outro are long holds, accelerate is a hard 1-beat run.
SECTION_CUT_BEATS: Dict[str, int] = {
    "intro": 4,
    "build": 4,
    "mid": 2,
    "low": 4,
    "high": 2,
    "drop": 2,
    "breathe": 6,
    "accelerate": 1,
    "outro": 6,
}

# Every flag this module can put on an entry. montage_edit copies exactly these
# onto each segment and actions.py's finish() reads them back — the loop is
# closed on purpose. `shake` and `fadeout` shipped here in #177 and were then
# emitted for two phases with NOTHING reading them; the round trip is now
# asserted in tests/domains/auto_edit/test_montage_arrangement.py, so adding a
# flag without a consumer fails offline instead of silently doing nothing.
ARRANGEMENT_FLAGS = ("flash", "shake", "retime", "fadeout")

DROP_CUT_BEATS = SECTION_CUT_BEATS["drop"]
BREATHE_CUT_BEATS = SECTION_CUT_BEATS["breathe"]
ACCELERATE_RUN_BEATS = SECTION_CUT_BEATS["accelerate"]
ACCELERATE_RUN_LENGTH = 8  # beats of straight 1-beat cuts before a peak/drop


def _beat_index_at(beat_grid: List[float], seconds: float) -> int:
    """Index of the beat_grid entry closest to ``seconds`` (grid is sorted)."""
    if not beat_grid:
        return 0
    i = bisect.bisect_left(beat_grid, seconds)
    if i <= 0:
        return 0
    if i >= len(beat_grid):
        return len(beat_grid) - 1
    before, after = beat_grid[i - 1], beat_grid[i]
    return i - 1 if abs(before - seconds) <= abs(after - seconds) else i


def _entry(beat_index: int, beat_length: int, section: str, flags: List[str]) -> Dict[str, Any]:
    return {
        "beat_index": beat_index,
        "beat_length": beat_length,
        "section": section,
        "role": "montage",
        "flags": list(dict.fromkeys(flags)),  # dedupe, preserve order
    }


def _resolve_labels(sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Phase 1's raw energy labels -> arrangement roles: the flagged drop
    becomes its own "drop" label, and the final section becomes "outro"
    regardless of its energy (a track's tail is an outro even if it reads as
    "high" or "mid" by raw energy)."""
    n = len(sections)
    resolved = []
    for i, sec in enumerate(sections):
        label = sec.get("label", "mid")
        if sec.get("is_drop"):
            label = "drop"
        elif i == n - 1:
            label = "outro"
        resolved.append({**sec, "label": label})
    return resolved


def _section_schedule(start_beat: int, end_beat: int, label: str, prev_label: Optional[str]) -> List[Dict[str, Any]]:
    """Cutting schedule for one section's beat span [start_beat, end_beat)."""
    entries: List[Dict[str, Any]] = []
    cursor = start_beat

    if label == "drop":
        length = min(DROP_CUT_BEATS, end_beat - cursor)
        entries.append(_entry(cursor, length, label, ["flash", "shake"]))
        cursor += length
        label = "high"  # the remainder of the drop section plays out as a peak

    if prev_label == "high" and label in ("mid", "low") and (end_beat - cursor) > BREATHE_CUT_BEATS:
        length = min(BREATHE_CUT_BEATS, end_beat - cursor)
        entries.append(_entry(cursor, length, "breathe", []))
        cursor += length

    accel_start = None
    if label == "build" and (end_beat - ACCELERATE_RUN_LENGTH) > cursor:
        accel_start = end_beat - ACCELERATE_RUN_LENGTH

    limit = accel_start if accel_start is not None else end_beat
    section_start, section_span = start_beat, max(1, end_beat - start_beat)
    while cursor < limit:
        if label == "build":
            progress = (cursor - section_start) / section_span
            length = max(2, round(4 - 2 * progress))
        else:
            length = SECTION_CUT_BEATS.get(label, 2)
        length = min(length, limit - cursor)
        flags = ["shake"] if label == "high" else (["retime"] if label == "build" else [])
        entries.append(_entry(cursor, length, label, flags))
        cursor += length

    if accel_start is not None:
        while cursor < end_beat:
            length = min(ACCELERATE_RUN_BEATS, end_beat - cursor)
            entries.append(_entry(cursor, length, "accelerate", ["retime"]))
            cursor += length

    if entries:
        # Every section opens on a downbeat (section boundaries are bar-
        # aligned) — flash the shot that lands on it.
        entries[0]["flags"] = list(dict.fromkeys(entries[0]["flags"] + ["flash"]))
    if label == "outro" and entries:
        entries[-1]["flags"] = list(dict.fromkeys(entries[-1]["flags"] + ["fadeout"]))

    return entries


def _flat_schedule(beat_count: int) -> List[Dict[str, Any]]:
    """No section evidence at all: a flat 2-beat body cadence covering the grid."""
    schedule = []
    idx = 0
    length = SECTION_CUT_BEATS["mid"]
    while idx < beat_count:
        step = min(length, beat_count - idx)
        schedule.append(_entry(idx, step, "mid", []))
        idx += step
    return schedule


def plan_arrangement(
    beat_grid: List[float], sections: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The beat-indexed cutting schedule for a whole track.

    Every entry's ``beat_index`` is exactly the previous entry's
    ``beat_index + beat_length`` — the schedule covers ``[0, len(beat_grid))``
    with no gaps and no overlaps, which is what lets montage_edit derive each
    cut's source length from its record length instead of rounding twice, and
    what makes ``normalize_grid_phase`` + ``apply_revision``'s accumulate walk
    agree. Do not break it.

    The first section starts on the track's first DOWNBEAT, not on beat 0
    (#193 phase 3.2). ``downbeat_phase`` recovers which of the 4 beat phases
    carries the bar line, so the first downbeat sits at beat index ``phase``
    (0-3) and every section boundary after it is bar-aligned. Forcing section
    one to ``start_beat = 0`` meant that on roughly 3 of 4 tracks the whole
    intro cut on beat 3 of every bar, and the "section-opening downbeat" flash
    fired off the downbeat. The beats before that first downbeat are covered
    by an explicit PICKUP entry so the schedule still starts at beat 0 and the
    contiguity invariant above still holds.
    """
    beat_count = len(beat_grid)
    if beat_count <= 0:
        return []
    if not sections:
        return _flat_schedule(beat_count)

    resolved = _resolve_labels(sections)
    n = len(resolved)
    bounds = []
    for i, sec in enumerate(resolved):
        start_beat = _beat_index_at(beat_grid, sec["start_seconds"])
        end_beat = beat_count if i == n - 1 else _beat_index_at(beat_grid, resolved[i + 1]["start_seconds"])
        bounds.append((max(0, start_beat), min(beat_count, end_beat)))

    schedule: List[Dict[str, Any]] = []
    # Pickup: the pre-downbeat beats at the head of the track. It carries the
    # first section's label so downstream motion/pacing treat it as part of
    # that section, but no flags — the flash belongs on the real downbeat.
    first_start = bounds[0][0] if bounds else 0
    if first_start > 0:
        schedule.append(_entry(0, first_start, resolved[0]["label"], []))

    prev_label = None
    for (start_beat, end_beat), sec in zip(bounds, resolved):
        if end_beat > start_beat:
            schedule.extend(_section_schedule(start_beat, end_beat, sec["label"], prev_label))
        prev_label = sec["label"]
    return schedule
