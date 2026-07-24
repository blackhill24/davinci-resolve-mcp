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
from src.domains.auto_edit.utils import cut_ir, montage_edit
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


def _shot(idx, start, end, *, select_potential, pacing, description="A shot."):
    return {
        "shot_index": idx,
        "time_seconds_start": start,
        "time_seconds_end": end,
        "frame_indices_used": [idx],
        "description": description,
        "qc_flags": [],
        "editorial": {
            "editorial_role": "montage_element",
            "select_potential": select_potential,
            "best_moment_present": False,
            "best_moment": None,
            "pacing": pacing,
            "stillness_type": None,
            "pacing_note": None,
        },
    }


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
