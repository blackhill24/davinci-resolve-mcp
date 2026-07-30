"""Unit tests for src/domains/auto_edit/utils/montage_edit.py (the montage decision layer,
epic #38 P1 = issue #40).

No Resolve required — DB-only, same posture as test_auto_edit.py. Seeds the
analysis DB via analysis_store.ingest_report with per-shot editorial.
select_potential/pacing (the fields the schema actually carries at shot
level — energy_arc is clip-level only, see the module docstring).
"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
import unittest
import wave
from unittest import mock

from src.core import timeline_brain_db
from src.domains.auto_edit.utils import auto_edit, cut_ir, montage_edit
from src.domains.media_analysis.utils import analysis_store

from tests.domains.media_analysis.test_analysis_store import make_report

FPS = 24.0


def _click_track(path: str, *, bpm: float = 120.0, clicks: int = 12, sample_rate: int = 22050) -> None:
    """A decaying-tone metronome WAV — exact pattern from
    test_music_analysis.py's click-track fixture (proven to detect cleanly)."""
    import math
    interval = 60.0 / bpm
    total = int(sample_rate * (interval * clicks + 1.0))
    click_len = int(0.02 * sample_rate)
    buf = bytearray(total * 2)
    for beat in range(clicks):
        start = int(beat * interval * sample_rate)
        for i in range(click_len):
            idx = start + i
            if idx >= total:
                break
            env = 1.0 - i / click_len
            val = int(0.7 * env * math.sin(2 * math.pi * 880 * i / sample_rate) * 32767)
            struct.pack_into("<h", buf, idx * 2, val)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(buf))


def _visual_report(shots, *, clip_select_potential="medium"):
    return {
        "success": True,
        "clip_summary": "B-roll candidate clip.",
        "clip_summary_oneliner": "B-roll.",
        "editorial_classification": {
            "primary_use": "montage", "select_potential": clip_select_potential,
            "energy_arc": "varied", "style": "documentary", "genre_indicators": [], "reason": "",
        },
        "content": {"locations": [], "actions": []},
        "shot_and_style": {"shot_sizes": ["medium"], "camera_motion": ["static"]},
        "slate": {"slate_visible": False},
        "editing_notes": {"best_moments": [], "search_tags": []},
        "shot_descriptions": shots,
    }


def _shot(idx, start, end, *, select_potential, pacing, description="A shot.",
          best_moment=None, scout=None):
    shot = {
        "shot_index": idx,
        "time_seconds_start": start,
        "time_seconds_end": end,
        "frame_indices_used": [idx],
        "description": description,
        "qc_flags": [],
        "editorial": {
            "editorial_role": "montage_element",
            "select_potential": select_potential,
            "best_moment_present": best_moment is not None,
            "best_moment": best_moment,
            "pacing": pacing,
            "stillness_type": None,
            "pacing_note": None,
        },
    }
    if scout is not None:
        shot["scout"] = scout
    return shot


class MontageEditBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="montage-edit-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(timeline_brain_db.close_all)

    def _ingest_clip(self, *, clip_id, name, path, clip_dir, shots, clip_select_potential="medium"):
        report = make_report(visual=_visual_report(shots, clip_select_potential=clip_select_potential))
        report["clip"] = dict(report["clip"], clip_id=clip_id, clip_name=name,
                              file_path=path, media_id=clip_id + "-m", fps=FPS)
        report["transcription"] = {"success": False, "segments": []}
        result = analysis_store.ingest_report(self.root, report, clip_dir=clip_dir)
        self.assertTrue(result["success"], result)
        return result["clip_uuid"]

    def _seed_pool(self):
        """3 clips, mixed select_potential + pacing, enough shots to fill a
        short track without exhausting the pool."""
        self._ingest_clip(
            clip_id="resolve-b1", name="B1.mp4", path="/media/b1.mp4", clip_dir="b1-dir",
            shots=[
                _shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic", description="Fast action."),
                _shot(2, 3.0, 6.0, select_potential="high", pacing="still", description="Calm beauty shot."),
                _shot(3, 6.0, 9.0, select_potential="medium", pacing="moderate", description="Walking."),
            ])
        self._ingest_clip(
            clip_id="resolve-b2", name="B2.mp4", path="/media/b2.mp4", clip_dir="b2-dir",
            shots=[
                _shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic", description="Crowd cheering."),
                _shot(2, 3.0, 6.0, select_potential="medium", pacing="still", description="Landscape."),
                _shot(3, 6.0, 9.0, select_potential="low", pacing="variable", description="B-roll filler."),
            ])
        self._ingest_clip(
            clip_id="resolve-b3", name="B3.mp4", path="/media/b3.mp4", clip_dir="b3-dir",
            shots=[
                _shot(1, 0.0, 3.0, select_potential="high", pacing="still", description="Sunset."),
                _shot(2, 3.0, 6.0, select_potential="medium", pacing="kinetic", description="Dancing."),
            ])
        return ["/media/b1.mp4", "/media/b2.mp4", "/media/b3.mp4"]


class PureFunctionTests(unittest.TestCase):
    def test_local_onset_density_counts_within_window(self):
        onsets = [1.0, 1.5, 2.0, 5.0, 8.0]
        density = montage_edit.local_onset_density(onsets, 1.5, window=2.0)
        self.assertAlmostEqual(density, 3 / 2.0)  # 1.0,1.5,2.0 all within [0.5, 2.5]

    def test_local_onset_density_empty_onsets(self):
        self.assertEqual(montage_edit.local_onset_density([], 5.0), 0.0)

    def test_target_cut_seconds_scales_with_density(self):
        high = montage_edit.target_cut_seconds(4.0, max_density=4.0)
        low = montage_edit.target_cut_seconds(0.0, max_density=4.0)
        self.assertAlmostEqual(high, montage_edit.MIN_CUT_SECONDS)
        self.assertAlmostEqual(low, montage_edit.MAX_CUT_SECONDS)

    def test_target_cut_seconds_zero_max_density_falls_back(self):
        self.assertEqual(
            montage_edit.target_cut_seconds(0.0, max_density=0.0),
            montage_edit.DEFAULT_TARGET_CUT_SECONDS)

    def test_shot_fits_zone_kinetic_only_high(self):
        self.assertTrue(montage_edit.shot_fits_zone("kinetic", 0.9))
        self.assertFalse(montage_edit.shot_fits_zone("kinetic", 0.1))

    def test_shot_fits_zone_still_only_low(self):
        self.assertTrue(montage_edit.shot_fits_zone("still", 0.1))
        self.assertFalse(montage_edit.shot_fits_zone("still", 0.9))

    def test_shot_fits_zone_moderate_fits_anywhere(self):
        self.assertTrue(montage_edit.shot_fits_zone("moderate", 0.9))
        self.assertTrue(montage_edit.shot_fits_zone("moderate", 0.1))

    def test_nearest_onset_picks_closest_after_minimum(self):
        onsets = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(montage_edit.nearest_onset(onsets, 2.4, minimum=1.5), 2.0)

    def test_nearest_onset_falls_back_to_target_when_none_qualify(self):
        self.assertEqual(montage_edit.nearest_onset([1.0], 5.0, minimum=2.0), 5.0)


class ValidateBriefTests(unittest.TestCase):
    def test_requires_music(self):
        errors = montage_edit.validate_montage_brief_inputs(files=["/a.mp4"], music=None)
        self.assertTrue(any("music" in e for e in errors))

    def test_requires_nonempty_files(self):
        errors = montage_edit.validate_montage_brief_inputs(files=[], music="/m.wav")
        self.assertTrue(any("files" in e for e in errors))

    def test_rejects_negative_duration(self):
        errors = montage_edit.validate_montage_brief_inputs(
            files=["/a.mp4"], music="/m.wav", target_duration_seconds=-1)
        self.assertTrue(any("positive" in e for e in errors))

    def test_valid_brief_no_errors(self):
        errors = montage_edit.validate_montage_brief_inputs(files=["/a.mp4"], music="/m.wav")
        self.assertEqual(errors, [])


class BuildCutListMockedBeatsTests(MontageEditBase):
    """Deterministic assembly logic, with music_analysis.detect_beats mocked
    so onset placement is exactly known (real DSP is covered separately)."""

    def _mock_beats(self, *, duration=12.0, onsets=None, tempo=120.0):
        onsets = onsets if onsets is not None else [round(0.5 * i, 3) for i in range(1, 25)]
        return {
            "success": True, "available": True, "duration_seconds": duration,
            "onsets": onsets, "onset_count": len(onsets), "tempo_bpm": tempo,
        }

    def test_produces_valid_cut_list_with_hook_and_montage_segments(self):
        files = self._seed_pool()
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        plan = out["plan"]
        self.assertEqual(cut_ir.validate_cut_list(plan), [])
        roles = [s["role"] for s in plan["segments"]]
        self.assertEqual(roles[0], "montage_hook")
        self.assertTrue(all(r == "montage" for r in roles[1:]))
        self.assertGreater(len(plan["segments"]), 2)

    def test_record_frames_are_sequential(self):
        # build_timeline's shared executor reads record_start_frame to place
        # each segment — without _assign_record_frames every segment would
        # default to 0 and stack on top of the last.
        files = self._seed_pool()
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        segments = out["plan"]["segments"]
        cursor = 0
        for seg in segments:
            self.assertEqual(seg["record_start_frame"], cursor)
            cursor += seg["source_end_frame"] - seg["source_start_frame"]
        self.assertEqual(out["plan"]["record_duration_frames"], cursor)
        self.assertEqual(out["plan"]["music"]["record_start_frame"], 0)
        self.assertEqual(out["plan"]["music"]["record_end_frame"], cursor)

    def test_hook_is_highest_ranked_shot(self):
        files = self._seed_pool()
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        hook_seg = out["plan"]["segments"][0]
        # Every "high" shot is a plausible hook (ties broken by iteration order);
        # what matters is it's NOT a low/medium-only pick.
        self.assertIn("rank 3", hook_seg["rationale"])

    def test_no_shot_used_twice(self):
        files = self._seed_pool()
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        keys = [(s["clip_uuid"], s["source_start_frame"]) for s in out["plan"]["segments"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_music_no_ducking(self):
        files = self._seed_pool()
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertEqual(out["plan"]["music"]["ducking"]["mode"], cut_ir.DUCKING_STATIC)

    def test_target_duration_trims_runtime(self):
        files = self._seed_pool()
        brief = {"files": files, "music": "/media/track.wav", "target_duration_seconds": 3.0}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats(duration=12.0)):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        self.assertLessEqual(out["plan"]["estimates"]["duration_seconds"], 3.5)

    def test_truncates_honestly_when_pool_exhausted(self):
        # One clip, one usable shot besides the hook — nowhere near enough to
        # fill a long track. Must truncate, not repeat or fabricate.
        self._ingest_clip(
            clip_id="resolve-tiny", name="Tiny.mp4", path="/media/tiny.mp4", clip_dir="tiny-dir",
            shots=[
                _shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic"),
                _shot(2, 3.0, 6.0, select_potential="high", pacing="still"),
            ])
        brief = {"files": ["/media/tiny.mp4"], "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats(duration=60.0)):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        self.assertLess(out["plan"]["estimates"]["duration_seconds"], 60.0)
        self.assertTrue(any("ran out of candidate shots" in p for p in out["plan"]["problems"]))

    def test_no_music_refuses(self):
        files = self._seed_pool()
        out = montage_edit.build_cut_list_for_brief(self.root, {"files": files, "music": None})
        self.assertFalse(out["success"])

    def test_missing_analysis_for_file_reported(self):
        files = self._seed_pool() + ["/media/never-analyzed.mp4"]
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        self.assertTrue(any("never-analyzed" in p for p in out["plan"]["problems"]))

    def test_mixed_fps_refuses(self):
        files = self._seed_pool()
        self._ingest_clip(
            clip_id="resolve-oddfps", name="Odd.mp4", path="/media/odd.mp4", clip_dir="odd-dir",
            shots=[_shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic")])
        # Force a different fps on the odd clip directly via the DB row.
        conn = timeline_brain_db.connect(self.root)
        conn.execute("UPDATE clips SET fps = 30.0 WHERE clip_name = 'Odd.mp4'")
        conn.commit()
        brief = {"files": files + ["/media/odd.mp4"], "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertFalse(out["success"])
        self.assertIn("mixed frame rates", out["error"])


def _scout_window(window_start, window_end, in_point, *, usable=True,
                   subject_clarity="high", motion_interest="high", composition="high", exposure="good"):
    return {
        "window_start_seconds": window_start, "window_end_seconds": window_end,
        "in_point_seconds": in_point, "subject_clarity": subject_clarity,
        "motion_interest": motion_interest, "composition": composition, "exposure": exposure,
        "dominant_colour": {"tone": "warm", "brightness": 0.5}, "usable": usable, "why": "test",
    }


class InPointPrecedenceTests(MontageEditBase):
    """issue #178, phase 3/6 of the montage-quality epic: in-points come from
    a scouted window > best_moment > the shot's first frame — never just
    time_seconds_start by default."""

    def _mock_beats(self, *, duration=12.0):
        onsets = [round(0.5 * i, 3) for i in range(1, 25)]
        return {"success": True, "available": True, "duration_seconds": duration,
                "onsets": onsets, "onset_count": len(onsets), "tempo_bpm": 120.0,
                "grid_available": False}

    def test_scout_beats_best_moment_beats_shot_start(self):
        self._ingest_clip(
            clip_id="resolve-scoutclip", name="Scout.mp4", path="/media/scout.mp4",
            clip_dir="scout-dir",
            shots=[
                _shot(1, 0.0, 6.0, select_potential="high", pacing="kinetic",
                      best_moment={"time_seconds": 2.0, "why": "peak action"},
                      scout=[_scout_window(3.0, 4.0, 3.4)]),
                _shot(2, 6.0, 12.0, select_potential="high", pacing="still",
                      best_moment={"time_seconds": 8.0, "why": "held beat"}),
            ])
        brief = {"files": ["/media/scout.mp4"], "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        segments = out["plan"]["segments"]
        fps = out["plan"]["fps"]

        hook = segments[0]  # highest-ranked shot overall -> shot 1 (rank tie broken by iteration; both high)
        # Whichever shot became the hook, its in-point must honor the
        # scout > best_moment precedence for that specific shot.
        basis = hook["evidence"]["in_point_basis"]
        self.assertIn(basis, ("scout", "best_moment"))
        if hook["clip_uuid"] == segments[0]["clip_uuid"] and basis == "scout":
            self.assertEqual(hook["source_start_frame"], round(3.4 * fps))
        for seg in segments:
            self.assertIn(seg["evidence"]["in_point_basis"], ("scout", "best_moment", "shot_start"))
            self.assertNotEqual(seg["evidence"]["in_point_basis"], "shot_start")  # every shot here has a preference

    def test_preference_too_close_to_shot_end_is_clamped_not_used(self):
        # best_moment sits 0.2s before the shot ends — using it would leave no
        # room for even a short cut, so it must be skipped, not violated.
        self._ingest_clip(
            clip_id="resolve-clampclip", name="Clamp.mp4", path="/media/clamp.mp4",
            clip_dir="clamp-dir",
            shots=[
                _shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic",
                      best_moment={"time_seconds": 2.9, "why": "too late"}),
                _shot(2, 3.0, 6.0, select_potential="high", pacing="still"),
            ])
        brief = {"files": ["/media/clamp.mp4"], "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        hook = out["plan"]["segments"][0]
        self.assertEqual(hook["evidence"]["in_point_basis"], "shot_start")
        self.assertEqual(hook["source_start_frame"], 0)

    def test_no_scout_or_best_moment_falls_back_to_shot_start_honestly(self):
        files = self._seed_pool()  # plain shots, no best_moment/scout anywhere
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        for seg in out["plan"]["segments"]:
            self.assertEqual(seg["evidence"]["in_point_basis"], "shot_start")
        self.assertTrue(any("no scouted in-points" in p for p in out["plan"]["problems"]))


class RenderMontageSummaryTests(MontageEditBase):
    def test_summary_includes_beat_stats_and_roles_no_transcript_column(self):
        files = self._seed_pool()
        beats = {"success": True, "available": True, "duration_seconds": 12.0,
                 "onsets": [round(0.5 * i, 3) for i in range(1, 25)],
                 "onset_count": 24, "tempo_bpm": 120.0}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        summary = montage_edit.render_montage_summary(out["plan"])
        self.assertIn("Montage cut list", summary)
        self.assertIn("120 BPM", summary)
        self.assertIn("montage_hook", summary)
        self.assertIn("static level", summary)
        self.assertNotIn("Excerpt", summary)

    def test_summary_surfaces_truncation_problem(self):
        self._ingest_clip(
            clip_id="resolve-tiny2", name="Tiny2.mp4", path="/media/tiny2.mp4", clip_dir="tiny2-dir",
            shots=[
                _shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic"),
                _shot(2, 3.0, 6.0, select_potential="high", pacing="still"),
            ])
        beats = {"success": True, "available": True, "duration_seconds": 60.0,
                 "onsets": [round(0.5 * i, 3) for i in range(1, 121)],
                 "onset_count": 120, "tempo_bpm": 120.0}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": ["/media/tiny2.mp4"], "music": "/media/track.wav"})
        summary = montage_edit.render_montage_summary(out["plan"])
        self.assertIn("ran out of candidate shots", summary)

    def test_grid_locked_summary_shows_section_and_beats_columns(self):
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        summary = montage_edit.render_montage_summary(out["plan"])
        self.assertIn("| Section | Beats |", summary)
        self.assertIn("| intro | 4 |", summary)
        self.assertIn("| drop | 2 |", summary)


def _grid_beats(*, bpm=120.0, n_beats=24, duration=None, drop_at_bar=3, bars_total=6):
    """A fabricated grid_available=True detect_beats() result — pure Python,
    no ffmpeg — for exercising the beat-grid cutting path (issue #177)."""
    period = 60.0 / bpm
    beat_grid = [round(i * period, 6) for i in range(n_beats)]
    duration = duration if duration is not None else beat_grid[-1] + period
    drop_beat = drop_at_bar * 4
    sections = [
        {"start_bar": 0, "end_bar": drop_at_bar, "start_seconds": beat_grid[0],
         "end_seconds": beat_grid[min(drop_beat, n_beats - 1)],
         "energy": 1.0, "label": "intro", "is_drop": False},
        {"start_bar": drop_at_bar, "end_bar": bars_total, "start_seconds": beat_grid[min(drop_beat, n_beats - 1)],
         "end_seconds": beat_grid[-1], "energy": 3.0, "label": "high", "is_drop": True},
    ]
    return {
        "success": True, "available": True, "duration_seconds": duration,
        "onsets": [round(0.25 * i, 3) for i in range(int(duration / 0.25))],
        "onset_count": int(duration / 0.25), "tempo_bpm": bpm,
        "beat_grid": beat_grid, "bar_grid": beat_grid[::4], "downbeats": beat_grid[::4],
        "sections": sections, "tempo_confidence": 5.0, "beat_zero": 0.0,
        "grid_available": True, "method": "fabricated for tests",
    }


class BuildCutListGridLockedTests(MontageEditBase):
    """The beat-grid cutting path (issue #177, phase 2/6 of the
    montage-quality epic) — fabricated grid_available=True beats, no ffmpeg."""

    def test_grid_invariant_every_record_start_is_a_beat(self):
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        self.assertTrue(out["success"], out)
        plan = out["plan"]
        self.assertTrue(plan["grid_available"])
        fps = plan["fps"]
        beat_frames = {int(round(t * fps)) for t in beats["beat_grid"]}
        for seg in plan["segments"]:
            self.assertIn(seg["record_start_frame"], beat_frames)

    def test_source_length_equals_record_length_no_independent_rounding(self):
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        plan = out["plan"]
        fps = plan["fps"]
        beat_frames = [int(round(t * fps)) for t in beats["beat_grid"]]
        for seg in plan["segments"]:
            k = seg["beat_index"]
            end_k = min(k + seg["beat_length"], len(beat_frames) - 1)
            expected_len = beat_frames[end_k] - beat_frames[k]
            self.assertEqual(seg["source_end_frame"] - seg["source_start_frame"], expected_len)
            self.assertEqual(seg["record_start_frame"], beat_frames[k])

    def test_no_two_consecutive_segments_share_a_clip(self):
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        clip_uuids = [s["clip_uuid"] for s in out["plan"]["segments"]]
        for a, b in zip(clip_uuids, clip_uuids[1:]):
            self.assertNotEqual(a, b)

    def test_every_segment_carries_beat_index_length_and_section(self):
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        for seg in out["plan"]["segments"]:
            self.assertIsInstance(seg["beat_index"], int)
            self.assertIsInstance(seg["beat_length"], int)
            self.assertIsInstance(seg["section"], str)
            self.assertIn("look_bucket", seg)
            self.assertIn("motion", seg)
            self.assertIn("flash", seg)
            self.assertIn("retime", seg)

    def test_small_pool_fills_full_runtime_no_truncation(self):
        # 3 clips / 8 shots (~24s of raw material, the _seed_pool fixture) —
        # under the OLD one-shot-one-use model this pool caps out at ~8-9
        # segments regardless of how short each cut is, truncating well short
        # of a fast, cut-heavy track. Candidate windows + round-robin must now
        # reuse each clip's remaining seconds (via a different in-point) to
        # fill the whole runtime instead.
        files = self._seed_pool()
        # Mostly 2-beat (1s @ 120 BPM) cuts for ~18s straight -> needs ~20
        # segments, far more than the 8 distinct shots in the pool.
        beats = _grid_beats(bpm=120.0, n_beats=40, bars_total=10, drop_at_bar=1)
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        self.assertTrue(out["success"], out)
        self.assertFalse(any("ran out of candidate shots" in p for p in out["plan"]["problems"]))
        self.assertGreater(len(out["plan"]["segments"]), 8)

    def test_genuinely_insufficient_material_still_truncates_honestly(self):
        # 6s of total source material cannot fill a 24s track even with
        # window reuse — the grid path must still degrade honestly rather
        # than fabricate coverage that doesn't exist.
        self._ingest_clip(
            clip_id="resolve-tinygrid2", name="TinyGrid2.mp4", path="/media/tinygrid2.mp4",
            clip_dir="tinygrid2-dir",
            shots=[
                _shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic"),
                _shot(2, 3.0, 6.0, select_potential="high", pacing="still"),
            ])
        beats = _grid_beats(bpm=120.0, n_beats=48, bars_total=12, drop_at_bar=6)  # 24s track
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": ["/media/tinygrid2.mp4"], "music": "/media/track.wav"})
        self.assertTrue(out["success"], out)
        self.assertTrue(any("ran out of candidate shots" in p for p in out["plan"]["problems"]))
        self.assertLess(out["plan"]["estimates"]["duration_seconds"], 24.0)

    def test_grid_locked_plan_does_not_call_talking_heads_accumulate_walk(self):
        # auto_edit._assign_record_frames' accumulate walk would silently
        # throw away the beat-quantised record_start_frame values (it
        # re-derives every segment's start from source length in build
        # order) — the grid path must use _finalize_grid_locked_frames
        # instead and never call the shared talking-head walk at all.
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats), \
             mock.patch.object(montage_edit.auto_edit, "_assign_record_frames") as walk:
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        self.assertTrue(out["success"], out)
        walk.assert_not_called()

    def test_non_grid_plan_still_calls_talking_heads_accumulate_walk(self):
        files = self._seed_pool()
        beats = self._mock_beats_no_grid()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats), \
             mock.patch.object(montage_edit.auto_edit, "_assign_record_frames",
                                side_effect=auto_edit._assign_record_frames) as walk:
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        self.assertTrue(out["success"], out)
        walk.assert_called_once()

    @staticmethod
    def _mock_beats_no_grid():
        return {
            "success": True, "available": True, "duration_seconds": 12.0,
            "onsets": [round(0.5 * i, 3) for i in range(1, 25)],
            "onset_count": 24, "tempo_bpm": 120.0, "grid_available": False,
        }

    def test_music_record_end_uses_track_runtime_not_cursor(self):
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        plan = out["plan"]
        fps = plan["fps"]
        self.assertEqual(plan["music"]["record_end_frame"], round(beats["duration_seconds"] * fps))

    def test_talking_head_unaffected_by_grid_locked_addition(self):
        # The shared auto_edit.build_cut_list_for_brief path (talking-head)
        # never sets record_start_frame before calling _assign_record_frames,
        # so it must still get the original accumulate-walk untouched.
        from src.domains.auto_edit.utils import cut_ir as _cut_ir
        seg = _cut_ir.make_cut_list_segment(
            role="speech", clip_id="c1", source_start_frame=0, source_end_frame=48)
        plan = _cut_ir.make_cut_list(segments=[seg], fps=24.0)
        auto_edit._assign_record_frames(plan)
        self.assertEqual(plan["segments"][0]["record_start_frame"], 0)
        self.assertEqual(plan["record_duration_frames"], 48)


class BuildCutListRealBeatsTests(MontageEditBase):
    """Real end-to-end: an actual click-track WAV decoded by the real
    music_analysis.detect_beats (ffmpeg), same fixture proven in
    test_music_analysis.py's click-track test."""

    def setUp(self):
        super().setUp()
        import shutil as _shutil
        if not _shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not on PATH")

    def test_real_click_track_produces_valid_montage(self):
        files = self._seed_pool()
        music_path = os.path.join(self.root, "click.wav")
        _click_track(music_path, bpm=120.0, clicks=12)  # 6s track, beat every 0.5s
        out = montage_edit.build_cut_list_for_brief(
            self.root, {"files": files, "music": music_path})
        self.assertTrue(out["success"], out)
        plan = out["plan"]
        self.assertEqual(cut_ir.validate_cut_list(plan), [])
        self.assertEqual(plan["segments"][0]["role"], "montage_hook")
        self.assertGreaterEqual(plan["onset_count"], 8)
        self.assertIsNotNone(plan["tempo_bpm"])


if __name__ == "__main__":
    unittest.main()
