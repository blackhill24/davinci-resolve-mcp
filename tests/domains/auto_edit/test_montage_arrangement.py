"""Tests for src/domains/auto_edit/utils/montage_arrangement.py (issue #177,
phase 2/6 of the montage-quality epic). Pure planning — build the beat grid
and sections in the test, no ffmpeg, no DB.
"""
from __future__ import annotations

import pathlib
import unittest

from src.domains.auto_edit.utils import montage_arrangement as ma


def _grid(bpm: float, beats: int, *, beat_zero: float = 0.0) -> list:
    period = 60.0 / bpm
    return [round(beat_zero + i * period, 6) for i in range(beats)]


def _section(start_bar, end_bar, start_beat, beats_per_bar, label, *, is_drop=False, grid=None):
    start_seconds = grid[start_bar * beats_per_bar] if grid else 0.0
    end_idx = end_bar * beats_per_bar
    end_seconds = grid[end_idx] if grid and end_idx < len(grid) else grid[-1]
    return {
        "start_bar": start_bar, "end_bar": end_bar,
        "start_seconds": start_seconds, "end_seconds": end_seconds,
        "energy": 1.0, "label": label, "is_drop": is_drop,
    }


class SectionCutLengthTest(unittest.TestCase):
    def test_table_values(self):
        self.assertEqual(ma.SECTION_CUT_BEATS["mid"], 2)
        self.assertEqual(ma.SECTION_CUT_BEATS["high"], 2)
        self.assertEqual(ma.SECTION_CUT_BEATS["drop"], 2)
        self.assertEqual(ma.SECTION_CUT_BEATS["accelerate"], 1)
        self.assertIn(ma.SECTION_CUT_BEATS["intro"], (4, 8))
        self.assertTrue(4 <= ma.SECTION_CUT_BEATS["breathe"] <= 8)
        self.assertTrue(4 <= ma.SECTION_CUT_BEATS["outro"] <= 8)


class NoGapsNoOverlapsTest(unittest.TestCase):
    def test_flat_schedule_covers_grid_exactly(self):
        grid = _grid(120.0, 37)
        schedule = ma.plan_arrangement(grid, [])
        self._assert_gapless(schedule, len(grid))

    def test_sectioned_schedule_covers_grid_exactly(self):
        bpm, bpb = 120.0, 4
        grid = _grid(bpm, 96)
        sections = [
            _section(0, 8, 0, bpb, "intro", grid=grid),
            _section(8, 16, 0, bpb, "build", grid=grid),
            _section(16, 17, 0, bpb, "high", is_drop=True, grid=grid),
            _section(17, 24, 0, bpb, "high", grid=grid),
        ]
        schedule = ma.plan_arrangement(grid, sections)
        self._assert_gapless(schedule, len(grid))

    def _assert_gapless(self, schedule, beat_count):
        self.assertTrue(schedule)
        cursor = 0
        for entry in schedule:
            self.assertEqual(entry["beat_index"], cursor)
            self.assertGreater(entry["beat_length"], 0)
            cursor += entry["beat_length"]
        self.assertEqual(cursor, beat_count)


class DropLandsOnDownbeatTest(unittest.TestCase):
    def test_drop_cut_starts_exactly_at_section_boundary(self):
        bpm, bpb = 120.0, 4
        grid = _grid(bpm, 64)
        sections = [
            _section(0, 12, 0, bpb, "build", grid=grid),
            _section(12, 16, 0, bpb, "high", is_drop=True, grid=grid),
        ]
        schedule = ma.plan_arrangement(grid, sections)
        drop_boundary_beat = 12 * bpb
        drop_entries = [e for e in schedule if e["section"] == "drop"]
        self.assertTrue(drop_entries)
        self.assertEqual(drop_entries[0]["beat_index"], drop_boundary_beat)
        self.assertIn("flash", drop_entries[0]["flags"])


class AccelerateRunTest(unittest.TestCase):
    def test_build_section_ends_in_consecutive_one_beat_cuts(self):
        bpm, bpb = 120.0, 4
        grid = _grid(bpm, 100)
        sections = [
            _section(0, 20, 0, bpb, "build", grid=grid),
            _section(20, 24, 0, bpb, "high", is_drop=True, grid=grid),
        ]
        schedule = ma.plan_arrangement(grid, sections)
        accel = [e for e in schedule if e["section"] == "accelerate"]
        self.assertGreaterEqual(len(accel), ma.ACCELERATE_RUN_LENGTH - 1)
        for e in accel:
            self.assertEqual(e["beat_length"], 1)
            self.assertIn("retime", e["flags"])
        # contiguous run immediately preceding the drop
        beats = [e["beat_index"] for e in accel]
        self.assertEqual(beats, list(range(beats[0], beats[0] + len(beats))))


class OutroTest(unittest.TestCase):
    def test_final_section_flagged_fadeout(self):
        bpm, bpb = 120.0, 4
        grid = _grid(bpm, 48)
        sections = [
            _section(0, 8, 0, bpb, "high", grid=grid),
            _section(8, 12, 0, bpb, "low", grid=grid),
        ]
        schedule = ma.plan_arrangement(grid, sections)
        self.assertIn("fadeout", schedule[-1]["flags"])


class FlagsAreConsumedTest(unittest.TestCase):
    """The guard that was missing: `shake` and `fadeout` were emitted here for
    two phases with nothing downstream reading them, and every offline test
    still passed. A flag with no consumer is a silent no-op, so assert the
    round trip — arrangement emits it, montage_edit copies it, finish() reads
    it — rather than trusting it."""

    def _every_emitted_flag(self):
        """Flags plan_arrangement actually produces across the shapes that
        exercise each branch (drop, high, build+accelerate, breathe, outro)."""
        bpm, bpb = 120.0, 4
        grid = _grid(bpm, 96)
        emitted = set()
        for sections in (
            [_section(0, 8, 0, bpb, "build", grid=grid),
             _section(8, 12, 0, bpb, "high", grid=grid, is_drop=True),
             _section(12, 20, 0, bpb, "low", grid=grid)],
            [_section(0, 8, 0, bpb, "high", grid=grid),
             _section(8, 16, 0, bpb, "mid", grid=grid)],
            [],
        ):
            for entry in ma.plan_arrangement(grid, sections):
                emitted.update(entry["flags"])
        return emitted

    def test_emitted_flags_are_all_declared(self):
        undeclared = self._every_emitted_flag() - set(ma.ARRANGEMENT_FLAGS)
        self.assertEqual(
            undeclared, set(),
            f"plan_arrangement emits flag(s) missing from ARRANGEMENT_FLAGS: {sorted(undeclared)}")

    def test_declared_flags_reach_the_segment(self):
        # montage_edit copies the whole vocabulary onto each segment, so every
        # declared flag must be a settable segment key.
        source = (pathlib.Path(__file__).resolve().parents[3]
                  / "src" / "domains" / "auto_edit" / "utils" / "montage_edit.py").read_text(encoding="utf-8")
        self.assertIn("montage_arrangement.ARRANGEMENT_FLAGS", source,
                      "montage_edit must copy the arrangement's flag vocabulary wholesale")

    def test_every_declared_flag_has_an_executor(self):
        # finish()'s montage pass must read each flag back off the segment.
        source = (pathlib.Path(__file__).resolve().parents[3]
                  / "src" / "domains" / "auto_edit" / "actions.py").read_text(encoding="utf-8")
        unread = [flag for flag in ma.ARRANGEMENT_FLAGS
                  if f'seg.get("{flag}")' not in source]
        self.assertEqual(
            unread, [],
            f"arrangement flag(s) emitted but never read by finish(): {unread} — "
            "either wire an executor in _apply_montage_motion or drop the flag")


class DownbeatPhasePickupTest(unittest.TestCase):
    """#193 phase 3.2 — the opening section must not cut off-bar.

    ``downbeat_phase`` recovers which of the 4 beat phases carries the bar
    line, so the first downbeat is at beat index ``phase`` (0-3) and every
    section's ``start_seconds`` is one of those downbeats. Forcing section one
    to ``start_beat = 0`` meant that on roughly 3 of 4 tracks the whole intro
    cut on beat 3 of every bar, and the section-opening ``flash`` fired off
    the downbeat. Every pre-existing fixture used ``phase = 0`` — the one
    value that hides it.
    """

    def _schedule_with_phase(self, phase: int, *, bpm=120.0, beats=32):
        grid = _grid(bpm, beats)
        # Sections start on DOWNBEATS, i.e. at beat indices phase, phase+4, ...
        sections = [
            {"start_bar": 0, "end_bar": 2, "start_seconds": grid[phase],
             "end_seconds": grid[phase + 8], "energy": 1.0, "label": "intro",
             "is_drop": False},
            {"start_bar": 2, "end_bar": 6, "start_seconds": grid[phase + 8],
             "end_seconds": grid[-1], "energy": 3.0, "label": "high", "is_drop": True},
        ]
        return grid, ma.plan_arrangement(grid, sections)

    def test_section_openings_land_on_a_downbeat_for_every_phase(self):
        # Cuts INSIDE a section run at that section's cadence (2 beats for
        # high/drop), so not every entry is a bar line — but every section
        # OPENING must be one. `flash` marks exactly those (the code flags
        # entries[0] of each section), which is the same set the audience
        # perceives as the arrangement landing.
        for phase in range(4):
            grid, schedule = self._schedule_with_phase(phase)
            self.assertTrue(schedule, phase)
            openers = [e for e in schedule if "flash" in e["flags"]]
            self.assertTrue(openers, phase)
            for entry in openers:
                self.assertEqual(
                    entry["beat_index"] % 4, phase % 4,
                    f"phase={phase}: section opens at beat {entry['beat_index']}, off the bar")

    def test_the_body_starts_at_the_first_downbeat(self):
        for phase in range(4):
            grid, schedule = self._schedule_with_phase(phase)
            body = schedule[1:] if phase > 0 else schedule
            self.assertEqual(body[0]["beat_index"], phase, phase)

    def test_pickup_covers_the_pre_downbeat_head(self):
        for phase in (1, 2, 3):
            grid, schedule = self._schedule_with_phase(phase)
            pickup = schedule[0]
            self.assertEqual(pickup["beat_index"], 0, phase)
            self.assertEqual(pickup["beat_length"], phase, phase)
            # The pickup is not the section opener — the flash belongs on the
            # real downbeat, which is the next entry.
            self.assertNotIn("flash", pickup["flags"], phase)
            self.assertIn("flash", schedule[1]["flags"], phase)

    def test_no_pickup_when_the_downbeat_is_beat_zero(self):
        grid, schedule = self._schedule_with_phase(0)
        self.assertEqual(schedule[0]["beat_index"], 0)
        self.assertIn("flash", schedule[0]["flags"])

    def test_contiguity_survives_the_pickup(self):
        # The invariant normalize_grid_phase and apply_revision's accumulate
        # walk both depend on: no gaps, no overlaps, starting at beat 0.
        for phase in range(4):
            grid, schedule = self._schedule_with_phase(phase)
            cursor = 0
            for entry in schedule:
                self.assertEqual(entry["beat_index"], cursor, f"phase={phase}")
                cursor += entry["beat_length"]


if __name__ == "__main__":
    unittest.main()
