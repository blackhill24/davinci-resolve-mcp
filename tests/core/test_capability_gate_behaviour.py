"""Capability gates driven in BOTH directions with a faithful double (#119 tasks 4, 5).

`_has_method()` is the repo's defence against the bridge fabricating a callable for
any attribute name. There are ~130 of these gates in `src/`, and the issue measured
how few were actually exercised: forcing every gate to `True` — the value the
fabrication bug produces, i.e. the exact thing `_has_method` exists to prevent —
failed only **10 of 2153 tests**. The #110 finding-11 fix could have been reverted
wholesale and 99.5% of the suite would have stayed green.

The cause is a testing habit, not missing intent. `_has_method` tests `dir()`, and a
MagicMock's `dir()` lists only the children a test has *touched* — so every method
the test did not explicitly configure reads as absent, the gate closes, and
production code takes its fallback. The test then asserts on the fallback and passes
without executing the path it was written for.

Every test here uses `tests.bridge_double.ResolveBridgeDouble` and runs each gate
twice — once against an object that has the method, once against one that does not —
asserting the two paths differ *and* that the supported path really calls through.
Both mutations then fail: `_has_method -> True` breaks the missing-method
expectations, `_has_method -> False` breaks the supported ones.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import src.server  # noqa: E402,F401  imported first: the domain modules import back
                   # from it, so importing one directly hits a circular import
import src.granular.folder as granular_folder  # noqa: E402
import src.granular.media_pool_item as granular_mpi  # noqa: E402
from tests.bridge_double import ResolveBridgeDouble, call_names  # noqa: E402


def _double(methods, name="obj"):
    return ResolveBridgeDouble(methods=methods, name=name)


# (tool function, gated method name, kwargs) for the Resolve-21 analysis family.
# Every one of these is `if not _has_method(x, M): return {"error": "M requires ..."}`
# followed by `return {"success": bool(x.M(...))}` — a shape with exactly two
# branches, so a gate stuck in either position is observable.
#
# `ok_return` is what the real API hands back on the success path — `True` for the
# boolean family, a media-pool item (or a list of before/after pairs) for
# RemoveMotionBlur, which the caller then dereferences. `false_return` is the
# API's own "I ran and declined" answer, which must stay distinguishable from
# "the method does not exist".
def _new_clip_double():
    return _double({"GetName": "A001_C003_deblur", "GetUniqueId": "uid-2"},
                   name="newClip")


_FOLDER_GATES = [
    (granular_folder.folder_perform_audio_classification, "PerformAudioClassification", {}, True, False),
    (granular_folder.folder_clear_audio_classification, "ClearAudioClassification", {}, True, False),
    (granular_folder.folder_analyze_for_intellisearch, "AnalyzeForIntellisearch", {}, True, False),
    (granular_folder.folder_analyze_for_slate, "AnalyzeForSlate", {}, True, False),
]

_CLIP_GATES = [
    (granular_mpi.perform_clip_audio_classification, "PerformAudioClassification", {}, True, False),
    (granular_mpi.clear_clip_audio_classification, "ClearAudioClassification", {}, True, False),
    (granular_mpi.analyze_clip_for_intellisearch, "AnalyzeForIntellisearch", {}, True, False),
    (granular_mpi.analyze_clip_for_slate, "AnalyzeForSlate", {}, True, False),
]

# RemoveMotionBlur is the same gate shape but returns objects, so it gets its own
# cases rather than a bool in the table above.
_MOTION_BLUR_GATES = [
    (granular_folder, granular_folder.folder_remove_motion_blur, {}),
    (granular_mpi, granular_mpi.remove_clip_motion_blur, {"clip_id": "clip-1"}),
]


class FolderAnalysisGateTest(unittest.TestCase):
    """src/granular/folder.py — five Resolve-21 gates, both directions."""

    def _run(self, fn, folder, **kwargs):
        mp = _double({"GetRootFolder": folder}, name="mediaPool")
        with mock.patch.object(granular_folder, "_get_mp", autospec=True,
                               return_value=(None, mp, None)), \
             mock.patch.object(granular_folder, "_resolve_folder", autospec=True,
                               return_value=(folder, None)):
            return fn(**kwargs)

    def test_gate_open_calls_through_and_reports_success(self):
        for fn, method, kwargs, ok_return, _false in _FOLDER_GATES:
            with self.subTest(gate=method, tool=fn.__name__):
                folder = _double({method: ok_return}, name="folder")
                out = self._run(fn, folder, **kwargs)
                self.assertNotIn("error", out,
                                 f"{fn.__name__} refused a folder that HAS {method}")
                self.assertTrue(out["success"])
                self.assertEqual([method], call_names(folder))

    def test_gate_closed_refuses_and_never_calls_the_fabricated_attribute(self):
        for fn, method, kwargs, _ok, _false in _FOLDER_GATES:
            with self.subTest(gate=method, tool=fn.__name__):
                folder = _double({"GetName": "ingest"}, name="folder")
                out = self._run(fn, folder, **kwargs)
                self.assertIn("error", out,
                              f"{fn.__name__} accepted a folder lacking {method} — "
                              f"the bridge would fabricate it and return None")
                self.assertIn(method, str(out["error"]))
                self.assertEqual([], call_names(folder))

    def test_a_false_api_return_is_reported_as_failure_not_as_a_missing_method(self):
        """The two failure modes must stay distinguishable."""
        for fn, method, kwargs, _ok, false_return in _FOLDER_GATES:
            with self.subTest(gate=method):
                folder = _double({method: false_return}, name="folder")
                out = self._run(fn, folder, **kwargs)
                self.assertNotIn("error", out)
                self.assertFalse(out["success"])


class ClipAnalysisGateTest(unittest.TestCase):
    """src/granular/media_pool_item.py — the same five gates on a clip."""

    def _run(self, fn, clip, **kwargs):
        mp = _double({"GetRootFolder": _double({}, name="root")}, name="mediaPool")
        with mock.patch.object(granular_mpi, "_get_mp", autospec=True,
                               return_value=(None, mp, None)), \
             mock.patch.object(granular_mpi, "_find_clip_by_id", autospec=True,
                               return_value=clip):
            return fn(clip_id="clip-1", **kwargs)

    def test_gate_open_calls_through(self):
        for fn, method, kwargs, ok_return, _false in _CLIP_GATES:
            with self.subTest(gate=method, tool=fn.__name__):
                clip = _double({method: ok_return}, name="clip")
                out = self._run(fn, clip, **kwargs)
                self.assertNotIn("error", out)
                self.assertTrue(out["success"])
                self.assertEqual([method], call_names(clip))

    def test_gate_closed_refuses(self):
        for fn, method, kwargs, _ok, _false in _CLIP_GATES:
            with self.subTest(gate=method, tool=fn.__name__):
                clip = _double({"GetName": "A001_C003"}, name="clip")
                out = self._run(fn, clip, **kwargs)
                self.assertIn("error", out)
                self.assertIn(method, str(out["error"]))
                self.assertEqual([], call_names(clip))


class MotionBlurGateTest(unittest.TestCase):
    """RemoveMotionBlur — same gate, but the API answers with objects.

    The success path dereferences what came back (`new_clip.GetName()`), so a gate
    stuck open here does not merely return a wrong flag: it calls a method on the
    `None` the bridge fabricated and raises. Worth its own coverage.
    """

    def _run_folder(self, folder):
        mp = _double({"GetRootFolder": folder}, name="mediaPool")
        with mock.patch.object(granular_folder, "_get_mp", autospec=True,
                               return_value=(None, mp, None)), \
             mock.patch.object(granular_folder, "_resolve_folder", autospec=True,
                               return_value=(folder, None)):
            return granular_folder.folder_remove_motion_blur()

    def _run_clip(self, clip):
        mp = _double({"GetRootFolder": _double({}, name="root")}, name="mediaPool")
        with mock.patch.object(granular_mpi, "_get_mp", autospec=True,
                               return_value=(None, mp, None)), \
             mock.patch.object(granular_mpi, "_find_clip_by_id", autospec=True,
                               return_value=clip):
            return granular_mpi.remove_clip_motion_blur(clip_id="clip-1")

    def test_clip_gate_open_returns_the_new_clips_identity(self):
        clip = _double({"RemoveMotionBlur": _new_clip_double()}, name="clip")
        out = self._run_clip(clip)
        self.assertTrue(out["success"])
        self.assertEqual("A001_C003_deblur", out["new"])
        self.assertEqual("uid-2", out["new_id"])
        self.assertEqual(["RemoveMotionBlur"], call_names(clip))

    def test_clip_gate_closed_refuses_before_dereferencing_anything(self):
        clip = _double({"GetName": "A001_C003"}, name="clip")
        out = self._run_clip(clip)
        self.assertIn("RemoveMotionBlur", str(out["error"]))
        self.assertEqual([], call_names(clip))

    def test_clip_declined_render_is_a_failure_not_a_missing_method(self):
        clip = _double({"RemoveMotionBlur": None}, name="clip")
        out = self._run_clip(clip)
        self.assertNotIn("error", out)
        self.assertFalse(out["success"])

    def test_folder_gate_open_reports_the_created_pairs(self):
        new_clip = _new_clip_double()
        orig = _double({"GetName": "A001_C003", "GetUniqueId": "uid-1"}, name="orig")
        folder = _double({"RemoveMotionBlur": [{1: orig, 2: new_clip}]}, name="folder")
        out = self._run_folder(folder)
        self.assertNotIn("error", out)
        self.assertEqual(["RemoveMotionBlur"], call_names(folder))

    def test_folder_gate_closed_refuses(self):
        folder = _double({"GetName": "ingest"}, name="folder")
        out = self._run_folder(folder)
        self.assertIn("RemoveMotionBlur", str(out["error"]))
        self.assertEqual([], call_names(folder))


class MagicMockWouldHaveHiddenAllOfThisTest(unittest.TestCase):
    """Why every test above uses the double instead of a mock.

    A MagicMock drives *every* gate above down the missing-method branch, so the
    supported path is never executed — the test passes having tested nothing.
    """

    def test_a_magicmock_folder_is_refused_by_every_gate(self):
        for fn, method, _kwargs, _ok, _false in _FOLDER_GATES:
            with self.subTest(gate=method):
                folder = mock.MagicMock()
                mp = mock.MagicMock()
                with mock.patch.object(granular_folder, "_get_mp", autospec=True,
                                       return_value=(None, mp, None)), \
                     mock.patch.object(granular_folder, "_resolve_folder",
                                       autospec=True,
                                       return_value=(folder, None)):
                    out = fn()
                self.assertIn(
                    "error", out,
                    "a MagicMock is now accepted by the gate — if the double was "
                    "made mock-like, every test above became vacuous (#119 §2)")


if __name__ == "__main__":
    unittest.main()
