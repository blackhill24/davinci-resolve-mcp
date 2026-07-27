"""Behaviour of the granular (`--full`) confirm gate and AI-ops ledger (#138, #139).

`tests/test_granular_guard_drift.py` is the static half — it proves the guards are
*wired* at every site. This is the dynamic half: it proves they *work*, by driving
real granular tools against a faithful bridge double and checking that the
destructive call does not reach Resolve until a token is presented, and that the
AI ops land in the ledger with the same op_class / success the compound path
records.

The assertion that matters most in both classes is the negative one — that the
Resolve API method was **not** called. A gate that returns a warning envelope and
performs the operation anyway would satisfy every positive assertion here.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import src.server  # noqa: F401  imported first — the domain modules import back
                   # from it, so importing one directly hits a circular import
import src.core.tool_kernel as _core_tool_kernel
import src.granular.folder as granular_folder
import src.granular.graph as granular_graph
import src.granular.guards as granular_guards
import src.granular.media_pool as granular_media_pool
import src.granular.media_pool_item as granular_mpi
import src.granular.project as granular_project
import src.granular.timeline as granular_timeline
from src.core import resolve_ai_ledger as _ledger
from tests.bridge_double import ResolveBridgeDouble, call_names


def _double(methods, name="obj"):
    return ResolveBridgeDouble(methods=methods, name=name)


class LedgerRootMixin:
    """Point the ledger at a throwaway project root.

    Also keeps `_destructive_versioning_provider` — which the real
    `_ai_ledger_root` consults — away from a live Resolve during the offline run.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_root = _core_tool_kernel._ai_ledger_root
        _core_tool_kernel._ai_ledger_root = lambda: self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, _core_tool_kernel, "_ai_ledger_root", self._orig_root)

    def rows(self, op=None):
        return _ledger.get_usage(project_root=self._tmp.name, op=op)


class ConfirmGateBlocksTheFirstCall(unittest.TestCase):
    """Every gated granular tool must refuse before touching Resolve."""

    def test_reset_intellisearch_analysis_needs_a_token(self):
        project = _double({"ResetIntellisearchAnalysis": True}, name="project")
        resolve = _double({"GetProjectManager": _double(
            {"GetCurrentProject": project}, name="pm")}, name="resolve")
        with mock.patch.object(granular_project, "get_resolve", return_value=resolve):
            out = granular_project.reset_intellisearch_analysis()
        self.assertEqual("confirmation_required", out["status"])
        self.assertTrue(out["confirm_token"])
        self.assertIn("IntelliSearch", out["preview"]["warning"])
        self.assertEqual([], call_names(project),
                         "the analysis was wiped before the user confirmed anything")

    def test_reset_intellisearch_analysis_proceeds_with_the_token(self):
        project = _double({"ResetIntellisearchAnalysis": True}, name="project")
        resolve = _double({"GetProjectManager": _double(
            {"GetCurrentProject": project}, name="pm")}, name="resolve")
        with mock.patch.object(granular_project, "get_resolve", return_value=resolve):
            issued = granular_project.reset_intellisearch_analysis()
            out = granular_project.reset_intellisearch_analysis(
                confirm_token=issued["confirm_token"])
        self.assertTrue(out["success"])
        self.assertEqual(["ResetIntellisearchAnalysis"], call_names(project))

    def test_a_token_is_one_time_use(self):
        project = _double({"ResetIntellisearchAnalysis": True}, name="project")
        resolve = _double({"GetProjectManager": _double(
            {"GetCurrentProject": project}, name="pm")}, name="resolve")
        with mock.patch.object(granular_project, "get_resolve", return_value=resolve):
            token = granular_project.reset_intellisearch_analysis()["confirm_token"]
            granular_project.reset_intellisearch_analysis(confirm_token=token)
            replay = granular_project.reset_intellisearch_analysis(confirm_token=token)
        self.assertEqual("CONFIRM_TOKEN_INVALID", replay["error"]["code"])
        self.assertEqual(["ResetIntellisearchAnalysis"], call_names(project),
                         "a replayed token ran the operation a second time")

    def test_media_pool_delete_clips_needs_a_token(self):
        clip = _double({"GetName": "A001_C001", "GetUniqueId": "uid-1"}, name="clip")
        mp = _double({"GetRootFolder": _double({}, name="root"), "DeleteClips": True},
                     name="mediaPool")
        with mock.patch.object(granular_media_pool, "_get_mp", return_value=(None, mp, None)), \
             mock.patch.object(granular_media_pool, "_find_clips_by_ids", return_value=[clip]):
            out = granular_media_pool.delete_media_pool_clips(clip_ids=["uid-1"])
            self.assertEqual("confirmation_required", out["status"])
            self.assertEqual(1, out["preview"]["clips_lost"])
            self.assertNotIn("DeleteClips", call_names(mp))
            done = granular_media_pool.delete_media_pool_clips(
                clip_ids=["uid-1"], confirm_token=out["confirm_token"])
        self.assertTrue(done["success"])
        self.assertIn("DeleteClips", call_names(mp))

    def test_a_token_does_not_carry_to_a_different_target(self):
        """The fingerprint covers the arguments, not just the action name."""
        mp = _double({"GetRootFolder": _double({}, name="root"), "DeleteClips": True},
                     name="mediaPool")
        clip = _double({"GetName": "A001_C001", "GetUniqueId": "uid-1"}, name="clip")
        with mock.patch.object(granular_media_pool, "_get_mp", return_value=(None, mp, None)), \
             mock.patch.object(granular_media_pool, "_find_clips_by_ids", return_value=[clip]):
            token = granular_media_pool.delete_media_pool_clips(
                clip_ids=["uid-1"])["confirm_token"]
            out = granular_media_pool.delete_media_pool_clips(
                clip_ids=["uid-2"], confirm_token=token)
        self.assertEqual("CONFIRM_TOKEN_FINGERPRINT_MISMATCH", out["error"]["code"])
        self.assertNotIn("DeleteClips", call_names(mp))

    def test_delete_track_needs_a_token(self):
        tl = _double({"GetItemListInTrack": [], "DeleteTrack": True}, name="timeline")
        with mock.patch.object(granular_timeline, "_get_timeline", return_value=(None, tl, None)):
            out = granular_timeline.timeline_delete_track("video", 2)
        self.assertEqual("confirmation_required", out["status"])
        self.assertNotIn("DeleteTrack", call_names(tl))

    def test_reset_all_grades_needs_a_token(self):
        graph = _double({"ResetAllGrades": True}, name="graph")
        item = _double({"GetNodeGraph": graph, "GetName": "A001_C001"}, name="item")
        with mock.patch.object(granular_graph, "_get_timeline_item", return_value=(item, None)):
            out = granular_graph.graph_reset_all_grades()
        self.assertEqual("confirmation_required", out["status"])
        self.assertEqual([], call_names(graph))

    def test_delete_folders_needs_a_token(self):
        sub = _double({"GetName": "ingest"}, name="folder")
        current = _double({"GetSubFolderList": [sub]}, name="current")
        mp = _double({"GetCurrentFolder": current, "DeleteFolders": True}, name="mediaPool")
        with mock.patch.object(granular_media_pool, "_get_mp", return_value=(None, mp, None)):
            out = granular_media_pool.delete_media_pool_folders(folder_names=["ingest"])
        self.assertEqual("confirmation_required", out["status"])
        self.assertNotIn("DeleteFolders", call_names(mp))

    def test_delete_timelines_needs_a_token(self):
        tl = _double({"GetUniqueId": "tl-1", "GetName": "EP101 v3"}, name="timeline")
        project = _double({"GetTimelineCount": 1, "GetTimelineByIndex": tl}, name="project")
        mp = _double({"DeleteTimelines": True}, name="mediaPool")
        with mock.patch.object(granular_media_pool, "_get_mp", return_value=(project, mp, None)):
            out = granular_media_pool.delete_timelines_by_id(timeline_ids=["tl-1"])
        self.assertEqual("confirmation_required", out["status"])
        self.assertEqual(["EP101 v3"], out["preview"]["names"])
        self.assertEqual([], call_names(mp))


class TimelineDeleteClipsRippleGate(unittest.TestCase):
    """Only the rippling delete is gated — the same line the compound path draws."""

    def _timeline(self):
        item = _double({"GetUniqueId": "uid-1"}, name="item")
        return item, _double({"GetItemListInTrack": [item], "DeleteClips": True},
                             name="timeline")

    def test_non_rippling_delete_runs_without_a_token(self):
        item, tl = self._timeline()
        with mock.patch.object(granular_timeline, "_get_timeline", return_value=(None, tl, None)):
            out = granular_timeline.timeline_delete_clips(clip_ids=["uid-1"])
        self.assertTrue(out["success"])
        self.assertIn("DeleteClips", call_names(tl))

    def test_rippling_delete_is_gated(self):
        item, tl = self._timeline()
        with mock.patch.object(granular_timeline, "_get_timeline", return_value=(None, tl, None)):
            out = granular_timeline.timeline_delete_clips(clip_ids=["uid-1"], ripple=True)
            self.assertEqual("confirmation_required", out["status"])
            self.assertTrue(out["preview"]["ripple"])
            self.assertNotIn("DeleteClips", call_names(tl))
            done = granular_timeline.timeline_delete_clips(
                clip_ids=["uid-1"], ripple=True, confirm_token=out["confirm_token"])
        self.assertTrue(done["success"])

    def test_ripple_flag_reaches_the_api(self):
        """Default False, and True is passed through rather than dropped."""
        item, tl = self._timeline()
        with mock.patch.object(granular_timeline, "_get_timeline", return_value=(None, tl, None)), \
             mock.patch.object(granular_guards, "_confirm_token_required", return_value=False):
            granular_timeline.timeline_delete_clips(clip_ids=["uid-1"])
            granular_timeline.timeline_delete_clips(clip_ids=["uid-1"], ripple=True)
        from tests.bridge_double import calls_of
        ripples = [args[1] for name, args, _kw in calls_of(tl) if name == "DeleteClips"]
        self.assertEqual([False, True], ripples)


class GatePreferenceIsShared(unittest.TestCase):
    """Turning the gate off is one preference, honored on both surfaces."""

    def test_disabled_preference_lets_the_call_through(self):
        project = _double({"ResetIntellisearchAnalysis": True}, name="project")
        resolve = _double({"GetProjectManager": _double(
            {"GetCurrentProject": project}, name="pm")}, name="resolve")
        with mock.patch.object(granular_project, "get_resolve", return_value=resolve), \
             mock.patch.object(_core_tool_kernel, "_read_media_analysis_preferences",
                               return_value={"destructive": {"require_confirm_token": False}}):
            out = granular_project.reset_intellisearch_analysis()
        self.assertTrue(out["success"])
        self.assertEqual(["ResetIntellisearchAnalysis"], call_names(project))


class LedgerRecordsGranularAiOps(LedgerRootMixin, unittest.TestCase):
    """#139: a `--full` session must not leave the ledger empty but authoritative."""

    def _folder_call(self, fn, methods, **kwargs):
        folder = _double(methods, name="folder")
        mp = _double({"GetRootFolder": _double({}, name="root")}, name="mediaPool")
        with mock.patch.object(granular_folder, "_get_mp", return_value=(None, mp, None)), \
             mock.patch.object(granular_folder, "_resolve_folder", return_value=(folder, None)):
            return fn(**kwargs), folder

    def _clip_call(self, fn, methods, **kwargs):
        clip = _double(methods, name="clip")
        mp = _double({"GetRootFolder": _double({}, name="root")}, name="mediaPool")
        with mock.patch.object(granular_mpi, "_get_mp", return_value=(None, mp, None)), \
             mock.patch.object(granular_mpi, "_find_clip_by_id", return_value=clip):
            return fn(**kwargs), clip

    def test_folder_analysis_ops_are_recorded(self):
        cases = [
            (granular_folder.folder_perform_audio_classification,
             "PerformAudioClassification", "perform_audio_classification", {}),
            (granular_folder.folder_clear_audio_classification,
             "ClearAudioClassification", "clear_audio_classification", {}),
            (granular_folder.folder_analyze_for_intellisearch,
             "AnalyzeForIntellisearch", "analyze_for_intellisearch", {}),
            (granular_folder.folder_analyze_for_slate,
             "AnalyzeForSlate", "analyze_for_slate", {"marker_color": "Sky"}),
        ]
        for fn, method, op, kwargs in cases:
            with self.subTest(op=op):
                out, _folder = self._folder_call(fn, {method: True}, **kwargs)
                self.assertTrue(out["success"])
                rows = self.rows(op=op)
                self.assertEqual(1, len(rows), f"{op} did not reach the ledger")
                self.assertEqual(1, rows[0]["success"])
                self.assertEqual(_ledger.OP_META[op]["op_class"], rows[0]["op_class"])

    def test_clip_analysis_ops_record_the_clip_id(self):
        out, _clip = self._clip_call(
            granular_mpi.analyze_clip_for_slate,
            {"AnalyzeForSlate": True}, clip_id="c-42", marker_color="Sky")
        self.assertTrue(out["success"])
        rows = self.rows(op="analyze_for_slate")
        self.assertEqual(1, len(rows))
        self.assertEqual("c-42", rows[0]["clip_id"])

    def test_a_declined_op_is_recorded_as_a_failure(self):
        """A refusal is a fact about the project, not an absence of one."""
        out, _folder = self._folder_call(
            granular_folder.folder_perform_audio_classification,
            {"PerformAudioClassification": False})
        self.assertFalse(out["success"])
        rows = self.rows(op="perform_audio_classification")
        self.assertEqual(1, len(rows))
        self.assertEqual(0, rows[0]["success"])

    def test_render_class_ops_are_recorded_as_render(self):
        new_clip = _double({"GetName": "A001_C003_deblur", "GetUniqueId": "uid-2"},
                           name="new")
        with mock.patch.object(granular_guards, "_confirm_token_required", return_value=False):
            out, _clip = self._clip_call(
                granular_mpi.remove_clip_motion_blur,
                {"RemoveMotionBlur": new_clip}, clip_id="c-42")
        self.assertTrue(out["success"])
        rows = self.rows(op="remove_motion_blur")
        self.assertEqual(1, len(rows))
        self.assertEqual(_ledger.OP_CLASS_RENDER, rows[0]["op_class"])
        self.assertEqual("c-42", rows[0]["clip_id"])

    def test_generate_speech_is_recorded(self):
        new_item = _double({"GetName": "vo_01.wav", "GetUniqueId": "uid-9"}, name="new")
        project = _double({"GenerateSpeech": new_item}, name="project")
        with mock.patch.object(granular_project, "get_current_project",
                               return_value=(None, project)), \
             mock.patch.object(granular_guards, "_confirm_token_required", return_value=False):
            out = granular_project.generate_speech(text_input="hello")
        self.assertTrue(out["success"])
        rows = self.rows(op="generate_speech")
        self.assertEqual(1, len(rows))
        self.assertEqual(_ledger.OP_CLASS_RENDER, rows[0]["op_class"])
        self.assertEqual("AI Speech Generator", rows[0]["extra_required"])

    def test_a_gated_op_that_never_ran_is_not_recorded(self):
        """A pending confirmation is not an operation — it must leave no row."""
        project = _double({"ResetIntellisearchAnalysis": True}, name="project")
        resolve = _double({"GetProjectManager": _double(
            {"GetCurrentProject": project}, name="pm")}, name="resolve")
        with mock.patch.object(granular_project, "get_resolve", return_value=resolve):
            granular_project.reset_intellisearch_analysis()
        self.assertEqual([], self.rows())


if __name__ == "__main__":
    unittest.main()
