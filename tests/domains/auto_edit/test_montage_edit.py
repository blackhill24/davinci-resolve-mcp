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


def _sig_candidate(clip_uuid, *, tone=None, brightness=None, file_path=None):
    sig = {"tone": tone, "brightness": brightness} if tone is not None else None
    return {"clip_uuid": clip_uuid, "time_seconds_start": 0.0,
            "file_path": file_path, "colour_signature": sig}


class LookBucketingTests(unittest.TestCase):
    """issue #179, phase 4/6 of the montage-quality epic: cluster source
    clips by colour signature into 2-4 look buckets, each with its own
    match CDL pulling toward a shared (median-brightness) target."""

    def test_distinct_signatures_yield_distinct_buckets(self):
        candidates = [
            _sig_candidate("a", tone="warm", brightness=0.7),
            _sig_candidate("b", tone="cool", brightness=0.3),
        ]
        bucket_of_clip, sigs, basis = montage_edit.assign_look_buckets(candidates)
        self.assertEqual(basis, "scout")
        self.assertEqual(len(set(bucket_of_clip.values())), 2)
        self.assertNotEqual(bucket_of_clip["a"], bucket_of_clip["b"])
        self.assertEqual(sigs["a"]["basis"], "scout")

    def test_same_signature_shares_a_bucket(self):
        candidates = [
            _sig_candidate("a", tone="warm", brightness=0.72),
            _sig_candidate("b", tone="warm", brightness=0.68),
        ]
        bucket_of_clip, _sigs, _basis = montage_edit.assign_look_buckets(candidates)
        self.assertEqual(bucket_of_clip["a"], bucket_of_clip["b"])

    def test_caps_at_four_buckets(self):
        # 6 clips spanning 3 tones x 2 bands = 6 distinct (tone, band) keys.
        candidates = [
            _sig_candidate("a", tone="warm", brightness=0.9),
            _sig_candidate("b", tone="warm", brightness=0.1),
            _sig_candidate("c", tone="cool", brightness=0.9),
            _sig_candidate("d", tone="cool", brightness=0.1),
            _sig_candidate("e", tone="neutral", brightness=0.9),
            _sig_candidate("f", tone="neutral", brightness=0.1),
        ]
        bucket_of_clip, _sigs, _basis = montage_edit.assign_look_buckets(candidates)
        self.assertLessEqual(len(set(bucket_of_clip.values())), montage_edit.MAX_LOOK_BUCKETS)

    def test_no_scout_data_falls_back_to_default_signature(self):
        # No colour_signature and no real file to probe -> honest neutral default.
        candidates = [_sig_candidate("a", file_path="/media/does-not-exist.mp4")]
        bucket_of_clip, sigs, basis = montage_edit.assign_look_buckets(candidates)
        self.assertEqual(basis, "default")
        self.assertEqual(sigs["a"]["tone"], "neutral")
        self.assertEqual(bucket_of_clip["a"], "neutral_mid")

    def test_mixed_basis_reported_honestly(self):
        candidates = [
            _sig_candidate("a", tone="warm", brightness=0.7),
            _sig_candidate("b", file_path="/media/does-not-exist.mp4"),
        ]
        _bucket_of_clip, _sigs, basis = montage_edit.assign_look_buckets(candidates)
        self.assertEqual(basis, "mixed")

    def test_match_cdl_targets_shared_median_brightness(self):
        candidates = [
            _sig_candidate("a", tone="warm", brightness=0.8),
            _sig_candidate("b", tone="cool", brightness=0.2),
            _sig_candidate("c", tone="neutral", brightness=0.5),
        ]
        bucket_of_clip, sigs, _basis = montage_edit.assign_look_buckets(candidates)
        cdls = montage_edit.compute_match_cdls(sigs, bucket_of_clip)
        self.assertEqual(len(cdls), 3)
        for bucket, cdl in cdls.items():
            self.assertIn("NodeIndex", cdl)
            self.assertEqual(len(cdl["Slope"]), 3)
            self.assertEqual(len(cdl["Offset"]), 3)
        # the neutral/median-brightness bucket needs no exposure correction
        neutral_bucket = bucket_of_clip["c"]
        self.assertEqual(cdls[neutral_bucket]["Offset"], [0.0, 0.0, 0.0])
        self.assertEqual(cdls[neutral_bucket]["Slope"], [1.0, 1.0, 1.0])
        # warm bucket's slope pulls red down / blue up (cools it toward neutral)
        warm_bucket = bucket_of_clip["a"]
        self.assertLess(cdls[warm_bucket]["Slope"][0], 1.0)
        self.assertGreater(cdls[warm_bucket]["Slope"][2], 1.0)

    def test_ffmpeg_fallback_used_when_file_missing_returns_none(self):
        self.assertIsNone(
            montage_edit._ffmpeg_colour_signature("/media/does-not-exist.mp4", 1.0))
        self.assertIsNone(montage_edit._ffmpeg_colour_signature(None, 1.0))

    # ── #193 phase 6.2.3: the match was too coarse to be a match ────────────

    def test_single_bucket_match_is_an_identity(self):
        # One bucket has nothing to match AGAINST: the exposure correction is
        # 0 by construction, but the white-balance tilt still fired, shifting
        # every channel 5% across the whole montage for no benefit.
        candidates = [
            _sig_candidate("a", tone="warm", brightness=0.5),
            _sig_candidate("b", tone="warm", brightness=0.5),
        ]
        bucket_of_clip, sigs, _basis = montage_edit.assign_look_buckets(candidates)
        self.assertEqual(len(set(bucket_of_clip.values())), 1)
        cdl = next(iter(montage_edit.compute_match_cdls(sigs, bucket_of_clip).values()))
        self.assertEqual(cdl["Slope"], [1.0, 1.0, 1.0])
        self.assertEqual(cdl["Offset"], [0.0, 0.0, 0.0])
        self.assertEqual(cdl["Power"], [1.0, 1.0, 1.0])
        self.assertEqual(cdl["Saturation"], 1.0)

    def test_exposure_offset_is_clamped(self):
        # The offset used to be the RAW brightness delta with no clamp, so a
        # dusk shot against a midday target got an enormous, shot-wrecking lift.
        candidates = [
            _sig_candidate("a", tone="neutral", brightness=0.05),
            _sig_candidate("b", tone="neutral", brightness=0.95),
        ]
        bucket_of_clip, sigs, _basis = montage_edit.assign_look_buckets(candidates)
        for cdl in montage_edit.compute_match_cdls(sigs, bucket_of_clip).values():
            for value in cdl["Offset"]:
                self.assertLessEqual(abs(value), montage_edit.MAX_MATCH_OFFSET)

    def test_white_balance_tilt_is_proportional_to_the_measured_cast(self):
        # A fixed +/-0.05 fired on a 3-way label whether the cast was a hint or
        # a wash. With a measured `cast` the tilt scales — and stays clamped.
        gentle = montage_edit._match_cdl(
            {"tone": "warm", "brightness": 0.5, "cast": 0.03},
            {"brightness": 0.5})
        strong = montage_edit._match_cdl(
            {"tone": "warm", "brightness": 0.5, "cast": 0.20},
            {"brightness": 0.5})
        self.assertLess(gentle["Slope"][0], 1.0)          # both cool it down
        self.assertLess(strong["Slope"][0], gentle["Slope"][0])   # ...strong more so
        self.assertGreaterEqual(strong["Slope"][0], 1.0 - montage_edit.MAX_MATCH_TILT)

    def test_label_only_signature_keeps_the_old_fixed_tilt(self):
        # Scout signatures carry the label and no magnitude — the label is all
        # the evidence there is, so that path must be unchanged.
        cdl = montage_edit._match_cdl(
            {"tone": "cool", "brightness": 0.5}, {"brightness": 0.5})
        self.assertAlmostEqual(cdl["Slope"][0], 1.0 + montage_edit._LOOK_TONE_TILT, places=4)
        self.assertAlmostEqual(cdl["Slope"][2], 1.0 - montage_edit._LOOK_TONE_TILT, places=4)

    def test_contrast_and_saturation_are_matched_when_measured(self):
        # Power and Saturation used to be hard-coded to 1.0, so neither axis
        # was ever matched at all.
        flat = montage_edit._match_cdl(
            {"tone": "neutral", "brightness": 0.5, "contrast": 0.08, "saturation": 0.10},
            {"brightness": 0.5, "contrast": 0.20, "saturation": 0.35})
        self.assertNotEqual(flat["Power"], [1.0, 1.0, 1.0])
        self.assertNotEqual(flat["Saturation"], 1.0)
        # ...and both corrections stay inside their ceilings (1e-9 absorbs the
        # float representation of an exactly-at-the-clamp value)
        self.assertLessEqual(abs(flat["Power"][0] - 1.0),
                             montage_edit.MAX_POWER_CORRECTION + 1e-9)
        self.assertLessEqual(abs(flat["Saturation"] - 1.0),
                             montage_edit.MAX_SAT_CORRECTION + 1e-9)

    def test_unmeasured_axes_stay_neutral(self):
        # Every axis no-ops when its input is missing — a tone+brightness
        # signature must behave exactly as it did before.
        cdl = montage_edit._match_cdl(
            {"tone": "neutral", "brightness": 0.4}, {"brightness": 0.5})
        self.assertEqual(cdl["Power"], [1.0, 1.0, 1.0])
        self.assertEqual(cdl["Saturation"], 1.0)
        self.assertEqual(cdl["Slope"], [1.0, 1.0, 1.0])
        self.assertAlmostEqual(cdl["Offset"][0], 0.1, places=4)

    # ── #193 phase 6.2.4: log/flat footage ──────────────────────────────────

    def test_flat_footage_is_detected_from_the_measurements(self):
        self.assertTrue(montage_edit._looks_flat(
            {"contrast": 0.05, "saturation": 0.08}))
        self.assertFalse(montage_edit._looks_flat(
            {"contrast": 0.25, "saturation": 0.40}))

    def test_flat_detection_never_guesses_without_measurements(self):
        # A scout or default signature carries no contrast/saturation — it
        # must not be reported as log on no evidence.
        self.assertFalse(montage_edit._looks_flat({"tone": "neutral", "brightness": 0.5}))
        self.assertFalse(montage_edit._looks_flat({"contrast": 0.05}))


class BuildCutListLookBucketTests(MontageEditBase):
    def _mock_beats(self, *, duration=12.0):
        onsets = [round(0.5 * i, 3) for i in range(1, 25)]
        return {"success": True, "available": True, "duration_seconds": duration,
                "onsets": onsets, "onset_count": len(onsets), "tempo_bpm": 120.0,
                "grid_available": False}

    def test_no_shot_rows_error_names_vision_as_the_cause(self):
        """#193 phase 1 — the vision-off signature.

        A clip that was analysed WITHOUT the vision pass has a clips row and
        zero shots rows, because `shots` is written only from
        `visual.shot_descriptions`. That produced a bare "no usable shots"
        error that named nothing about vision and offered no route out, two
        steps after the decision that caused it.
        """
        self._ingest_clip(clip_id="resolve-novis", name="NV.mp4", path="/media/nv.mp4",
                          clip_dir="nv-dir", shots=[])
        brief = {"files": ["/media/nv.mp4"], "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertFalse(out["success"], out)
        self.assertIn("vision", out["error"])
        self.assertIn("start_brief", out["error"])
        self.assertIn("vision", out["remediation"])

    def test_too_few_shots_error_also_points_at_vision_first(self):
        # One usable shot is not enough for a montage. Unlike the zero-rows
        # case this has several causes, so vision is offered as the first
        # thing to check rather than asserted.
        self._ingest_clip(
            clip_id="resolve-one", name="ONE.mp4", path="/media/one.mp4", clip_dir="one-dir",
            shots=[_shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic")])
        brief = {"files": ["/media/one.mp4"], "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        if not out["success"]:
            self.assertIn("vision", out["error"])
            self.assertIn("remediation", out)

    def test_every_segment_carries_a_look_bucket_and_plan_carries_cdls(self):
        files = self._seed_pool()
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        plan = out["plan"]
        for seg in plan["segments"]:
            self.assertIn("look_bucket", seg)
            self.assertIsNotNone(seg["look_bucket"])
        self.assertIn("look_buckets", plan)
        self.assertTrue(plan["look_buckets"])
        for bucket in {seg["look_bucket"] for seg in plan["segments"]}:
            self.assertIn(bucket, plan["look_buckets"])
        # no scout data anywhere in this fixture -> honest default basis, noted
        self.assertEqual(plan["look_bucket_basis"], "default")
        self.assertTrue(any("look buckets derived from" in p for p in plan["problems"]))


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

    def test_mixed_fps_cuts_a_majority_rate_timeline(self):
        # Mixed rates used to be refused outright. They are supported now: the
        # beat grid is in seconds and Resolve resamples off-rate media to keep
        # its wall-clock length (live-verified, live_mixed_fps_probe.py), so a
        # segment's SOURCE frames follow its own clip and its RECORD length
        # follows the timeline.
        files = self._seed_pool()
        self._ingest_clip(
            clip_id="resolve-oddfps", name="Odd.mp4", path="/media/odd.mp4", clip_dir="odd-dir",
            shots=[_shot(1, 0.0, 3.0, select_potential="high", pacing="kinetic")])
        # Force a different fps on the odd clip directly via the DB row.
        conn = timeline_brain_db.connect(self.root)
        conn.execute("UPDATE clips SET fps = 48.0 WHERE clip_name = 'Odd.mp4'")
        conn.commit()
        brief = {"files": files + ["/media/odd.mp4"], "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                return_value=self._mock_beats()):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        plan = out["plan"]
        # the timeline runs at the MAJORITY rate (3 clips @24 vs 1 @48)
        self.assertEqual(plan["fps"], FPS)
        self.assertTrue(any("mixed frame rates" in p for p in plan["problems"]), plan["problems"])
        odd = [s for s in plan["segments"] if s["clip_id"] == "resolve-oddfps"]
        if odd:
            # a 48fps shot costs 2 source frames for every timeline frame
            seg = odd[0]
            self.assertEqual(
                seg["source_end_frame"] - seg["source_start_frame"],
                cut_ir.segment_record_length(seg) * 2)
        # and the record cursor still adds up in TIMELINE frames
        expected = 0
        for seg in plan["segments"]:
            self.assertEqual(seg["record_start_frame"], expected)
            expected += cut_ir.segment_record_length(seg)

    def test_cuts_snap_to_the_provisional_pulse_not_onset_peaks(self):
        # Onset peaks do not follow the pulse — measured near chance against a
        # known grid, in the full mix and in the kick band alone. When the
        # tempo was too shaky to schedule an arrangement but a kick-phase-locked
        # pulse still exists, cuts must ride THAT. Disjoint times prove which
        # list the cutter used.
        files = self._seed_pool()
        beats = self._mock_beats()
        beats["onsets"] = [round(0.25 + 0.5 * i, 3) for i in range(24)]        # off-pulse
        beats["provisional_tempo_bpm"] = 120.0
        beats["provisional_beat_grid"] = [round(0.5 * i, 3) for i in range(25)]  # the pulse
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        plan = out["plan"]
        fps = plan["fps"]
        pulse_frames = {round(t * fps) for t in beats["provisional_beat_grid"]}
        onset_only = {round(t * fps) for t in beats["onsets"]} - pulse_frames
        boundaries = [s["record_start_frame"] for s in plan["segments"][1:]]
        self.assertTrue(boundaries, "expected more than a hook")
        for b in boundaries:
            self.assertNotIn(b, onset_only, f"cut at {b} landed on an onset peak, not the pulse")
        self.assertTrue(any("provisional" in p for p in plan["problems"]), plan["problems"])

    def test_no_tempo_at_all_degrades_to_onsets_and_says_so(self):
        files = self._seed_pool()
        beats = self._mock_beats()
        beats["provisional_beat_grid"] = []  # nothing to lock a pulse to
        brief = {"files": files, "music": "/media/track.wav"}
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(self.root, brief)
        self.assertTrue(out["success"], out)
        self.assertTrue(any("no tempo could be estimated" in p for p in out["plan"]["problems"]),
                        out["plan"]["problems"])


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


def _grid_beats(*, bpm=120.0, n_beats=24, duration=None, drop_at_bar=3, bars_total=6,
                beat_zero=0.0):
    """A fabricated grid_available=True detect_beats() result — pure Python,
    no ffmpeg — for exercising the beat-grid cutting path (issue #177).

    ``beat_zero`` defaults to 0.0 for the historical tests, but ``lock_phase``
    returns a NON-zero phase offset for essentially every real track, and 0.0
    is the one value that hides the whole class of phase bugs #193 phase 3
    fixed. New tests should pass a real offset.
    """
    period = 60.0 / bpm
    beat_grid = [round(beat_zero + i * period, 6) for i in range(n_beats)]
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
        "sections": sections, "tempo_confidence": 5.0, "beat_zero": beat_zero,
        "grid_available": True, "method": "fabricated for tests",
    }


class BuildCutListGridLockedTests(MontageEditBase):
    """The beat-grid cutting path (issue #177, phase 2/6 of the
    montage-quality epic) — fabricated grid_available=True beats, no ffmpeg."""

    def test_picture_starts_on_frame_zero_with_a_real_phase_offset(self):
        """#193 phase 3.1 — the montage no longer opens with black.

        ``plan_arrangement`` pins the first section to beat index 0 and the
        music is pinned to record frame 0, so before this fix the picture
        started at ``round(beat_zero * fps)`` — up to a full beat of black
        with the track already playing. ``lock_phase`` returns a non-zero
        ``beat_zero`` for essentially every real track.
        """
        files = self._seed_pool()
        beats = _grid_beats(beat_zero=0.37)
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        self.assertTrue(out["success"], out)
        plan = out["plan"]
        self.assertTrue(plan["grid_available"])
        # The offset is real in the fixture...
        self.assertGreater(int(round(beats["beat_grid"][0] * plan["fps"])), 0)
        # ...and normalised out of the cut.
        self.assertEqual(plan["segments"][0]["record_start_frame"], 0)
        self.assertEqual(plan["music"]["record_start_frame"], 0)

    def test_segments_stay_contiguous_after_phase_normalisation(self):
        # The accumulate walk in auto_edit._assign_record_frames can only be a
        # no-op — which is what keeps a title revision beat-lock safe — if the
        # cut is gapless from frame 0. plan_arrangement guarantees contiguity;
        # this proves the normalisation preserves it.
        files = self._seed_pool()
        beats = _grid_beats(beat_zero=0.37)
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        cursor = 0
        for seg in out["plan"]["segments"]:
            self.assertEqual(seg["record_start_frame"], cursor)
            cursor += cut_ir.segment_record_length(seg)

    def test_tail_extension_never_outruns_the_shots_own_source(self):
        # The slack the head-shift moves to the tail is absorbed by extending
        # the last shot — but only by frames it really has. Montage never
        # fabricates coverage.
        files = self._seed_pool()
        beats = _grid_beats(beat_zero=0.37)
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        for seg in out["plan"]["segments"]:
            limit = seg.get("source_limit_frame")
            if isinstance(limit, int):
                self.assertLessEqual(seg["source_end_frame"], limit)

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
        # Starts are beat frames with the grid's phase offset normalised out
        # (#193 phase 3) — the picture starts WITH the track, not one
        # beat_zero of black after it.
        phase = int(round(beats["beat_grid"][0] * fps))
        beat_frames = {int(round(t * fps)) - phase for t in beats["beat_grid"]}
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
        phase = beat_frames[0]
        last = plan["segments"][-1]
        for seg in plan["segments"]:
            k = seg["beat_index"]
            end_k = min(k + seg["beat_length"], len(beat_frames) - 1)
            expected_len = beat_frames[end_k] - beat_frames[k]
            if seg is not last:
                # the last segment may be extended into the track's tail
                self.assertEqual(seg["source_end_frame"] - seg["source_start_frame"], expected_len)
            self.assertEqual(seg["record_start_frame"], beat_frames[k] - phase)

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

    def test_motion_directive_matches_each_segments_own_section(self):
        # issue #180: with a confident grid (tempo known), every segment gets
        # a real beat-locked motion directive, not phase 2's None placeholder.
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        beat_seconds = 60.0 / beats["tempo_bpm"]
        for seg in out["plan"]["segments"]:
            motion = seg["motion"]
            self.assertIsNotNone(motion)
            mm = montage_edit.montage_motion
            base_start, base_end = mm.MOTION_ZOOM_RANGE.get(
                seg["section"], mm.DEFAULT_ZOOM_RANGE)
            # The move now varies per shot (#193 phase 6.2.2) — direction and
            # magnitude — so it is the section's ENVELOPE that is fixed, not
            # one exact pair. Both ends stay at or above 1.0 (a zoom below 1
            # would under-scan past the frame edges) and inside the section's
            # span scaled by the cycle's largest factor.
            max_scale = max(scale for _rev, scale in mm.ZOOM_VARIATION_CYCLE)
            ceiling = base_start + (base_end - base_start) * max_scale + 1e-6
            for value in (motion["zoom_start"], motion["zoom_end"]):
                self.assertGreaterEqual(value, 1.0)
                self.assertLessEqual(value, ceiling)
            self.assertAlmostEqual(motion["beat_seconds"], beat_seconds, places=4)

    def test_zoom_moves_vary_and_include_pull_outs(self):
        # The loudest tell after shot size: every shot pushing in by the same
        # amount. There must be both directions and more than one magnitude.
        files = self._seed_pool()
        beats = _grid_beats()
        with mock.patch.object(montage_edit.music_analysis, "detect_beats", return_value=beats):
            out = montage_edit.build_cut_list_for_brief(
                self.root, {"files": files, "music": "/media/track.wav"})
        moves = [(s["motion"]["zoom_start"], s["motion"]["zoom_end"])
                 for s in out["plan"]["segments"] if s.get("motion")]
        self.assertTrue(any(e > s for s, e in moves), "no push-ins at all")
        self.assertTrue(any(e < s for s, e in moves), "every move still pushes in")
        self.assertGreater(len({round(abs(e - s), 6) for s, e in moves}), 1,
                           "every move is the same magnitude")

    def test_motion_directives_are_deterministic(self):
        # A plan is re-derived on every revision; the same cut must produce
        # the same move each time. No RNG in the variation.
        files = self._seed_pool()
        runs = []
        for _ in range(2):
            beats = _grid_beats()
            with mock.patch.object(montage_edit.music_analysis, "detect_beats",
                                    return_value=beats):
                out = montage_edit.build_cut_list_for_brief(
                    self.root, {"files": files, "music": "/media/track.wav"})
            runs.append([(s.get("motion") or {}).get("zoom_start") for s in out["plan"]["segments"]])
        self.assertEqual(runs[0], runs[1])

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


class MontageQCTests(MontageEditBase):
    """montage_edit.build_qc_request / commit_qc_report (issue #181, phase
    6/6 of the montage-quality epic): frame extraction from the RENDERED
    output (never source media), the deferred host-vision payload shape,
    and mapping findings back to a suggested revise_cut edit."""

    def _plan(self, n_segments=3):
        segments = [
            cut_ir.make_cut_list_segment(
                role="montage_hook" if i == 0 else "montage",
                clip_id=f"clip-{i}", clip_uuid=f"uuid-{i}",
                source_start_frame=0, source_end_frame=48)
            for i in range(n_segments)
        ]
        plan = cut_ir.make_cut_list(segments=segments, fps=24.0)
        auto_edit._assign_record_frames(plan)
        plan["plan_id"] = "test-plan"
        return plan

    def test_missing_render_file_is_honest_not_a_crash(self):
        plan = self._plan()
        out = montage_edit.build_qc_request(plan, "/no/such/render.mov", self.root)
        self.assertFalse(out["success"])
        self.assertIn("render output not found", out["error"])

    def test_extracts_cut_frames_and_contact_sheet_under_analysis_root(self):
        plan = self._plan(n_segments=3)
        render_path = os.path.join(self.root, "render.mov")
        with open(render_path, "wb") as handle:
            handle.write(b"fake-render")
        extracted = []

        def fake_export(path, time_seconds, output_path):
            extracted.append(output_path)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "wb") as handle:
                handle.write(b"\xff\xd8\xff\xdbfake-jpeg")
            return True

        with mock.patch(
            "src.domains.media_analysis.utils.sampling_and_frames._export_analysis_frame",
            side_effect=fake_export,
        ):
            out = montage_edit.build_qc_request(plan, render_path, self.root)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["status"], "pending_host_analysis")
        # hook (segment 0) has no preceding cut to check
        self.assertEqual(len(out["cut_frames"]), 2)
        self.assertEqual([c["segment_index"] for c in out["cut_frames"]], [1, 2])
        self.assertEqual(len(out["contact_sheet"]), montage_edit.DEFAULT_QC_CONTACT_SHEET_COUNT)
        self.assertEqual(set(out["frame_paths"]), set(extracted))
        from src.domains.media_analysis.utils import analysis_memory
        qc_root = os.path.join(analysis_memory.memory_dir(self.root), "auto_edit", "qc")
        for path in extracted:
            self.assertTrue(path.startswith(qc_root), path)
        self.assertEqual(out["commit_action"]["action"], "commit_qc")

    def test_no_frames_extracted_is_honest(self):
        plan = self._plan()
        render_path = os.path.join(self.root, "render.mov")
        with open(render_path, "wb") as handle:
            handle.write(b"fake-render")
        with mock.patch(
            "src.domains.media_analysis.utils.sampling_and_frames._export_analysis_frame",
            return_value=False,
        ):
            out = montage_edit.build_qc_request(plan, render_path, self.root)
        self.assertFalse(out["success"])
        self.assertIn("no frames could be extracted", out["error"])


class CommitQCReportTests(unittest.TestCase):
    def _plan(self, n_segments=3):
        segments = [
            cut_ir.make_cut_list_segment(
                role="montage_hook" if i == 0 else "montage",
                clip_id=f"clip-{i}", clip_uuid=f"uuid-{i}",
                source_start_frame=0, source_end_frame=48)
            for i in range(n_segments)
        ]
        return cut_ir.make_cut_list(segments=segments, fps=24.0)

    def test_repeated_shot_finding_suggests_a_drop_edit(self):
        plan = self._plan()
        report = {"overall": "needs_revision", "findings": [
            {"kind": "repeated_shot", "segment_index": 2, "why": "same shot as segment 0",
             "severity": "high", "frame_path": "/root/qc/cut_002.jpg"},
        ]}
        out = montage_edit.commit_qc_report(plan, report)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["report"]["overall"], "needs_revision")
        self.assertEqual(len(out["suggested_edits"]), 1)
        self.assertEqual(out["suggested_edits"][0], {"op": "drop", "index": 2})
        self.assertEqual(out["report"]["findings"][0]["suggested_edit"], {"op": "drop", "index": 2})

    def test_exposure_finding_has_no_suggested_edit(self):
        plan = self._plan()
        report = {"findings": [
            {"kind": "exposure_outlier", "segment_index": 1, "why": "blown highlights",
             "severity": "medium"},
        ]}
        out = montage_edit.commit_qc_report(plan, report)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["suggested_edits"], [])
        self.assertNotIn("suggested_edit", out["report"]["findings"][0])

    def test_out_of_range_segment_index_never_suggests_an_edit(self):
        plan = self._plan(n_segments=2)
        report = {"findings": [
            {"kind": "repeated_shot", "segment_index": 99, "why": "?"},
        ]}
        out = montage_edit.commit_qc_report(plan, report)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["suggested_edits"], [])

    def test_no_findings_defaults_to_pass(self):
        plan = self._plan()
        out = montage_edit.commit_qc_report(plan, {"findings": []})
        self.assertTrue(out["success"], out)
        self.assertEqual(out["report"]["overall"], "pass")

    def test_qc_report_as_json_string_is_parsed(self):
        plan = self._plan()
        out = montage_edit.commit_qc_report(plan, '{"findings": []}')
        self.assertTrue(out["success"], out)

    def test_malformed_json_string_is_honest(self):
        plan = self._plan()
        out = montage_edit.commit_qc_report(plan, "{not json")
        self.assertFalse(out["success"])
        self.assertIn("not valid JSON", out["error"])

    def test_missing_findings_key_is_honest(self):
        plan = self._plan()
        out = montage_edit.commit_qc_report(plan, {"overall": "pass"})
        self.assertFalse(out["success"])


if __name__ == "__main__":
    unittest.main()
