"""Confirm-token gate + AI-ops ledger for the granular (`--full`) surface.

Both machineries live in `src/core/tool_kernel.py` and, until #138/#139, were
only ever reached through the compound server: the *same* Resolve call was
guarded when it arrived as a compound action and unguarded when it arrived as a
granular tool on `python src/server.py --full`. The gate was one door with a
second door standing open next to it, and the ledger was not merely incomplete
for a `--full` session — it was empty while still looking authoritative.

This module is the granular surface's door onto that machinery, so there is one
gate and one ledger rather than one of each per surface.

`action` strings deliberately match the **compound** action names
(`"media_pool.delete_clips"`, not the granular tool name
`delete_media_pool_clips`): the token table is keyed by action, a preview the
user reads names the same operation on either surface, and
`tests/test_granular_guard_drift.py` pairs a compound entry to its granular call
site through that string.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from src.core.timeline_lookup import _clip_file_size
from src.core.tool_kernel import (
    _ai_ledger_timed,
    _confirm_token_required,
    _consume_confirm_token,
    _issue_confirm_token,
)

__all__ = [
    "confirm_gate",
    "ledger_timed",
    "_clip_file_size",
    "GRANULAR_CONFIRM_SITES",
    "GATED_API_METHODS",
    "LEDGERED_API_METHODS",
]

# Re-exported so granular modules reach the ledger by the same name the compound
# domains use, without each one importing tool_kernel privates directly.
ledger_timed = _ai_ledger_timed


def confirm_gate(
    *,
    action: str,
    confirm_token: Optional[str],
    params: Dict[str, Any],
    preview: Callable[[], Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Two-call confirm gate. Returns an envelope to hand back, or None to proceed.

    Mirrors the compound sequence exactly. With no token, mint one and return the
    `pending_user_decision` body; with a token, validate-and-consume it and return
    the block envelope if it does not check out. Honors the same
    `destructive.require_confirm_token` preference, so disabling the gate disables
    it on both surfaces at once.

    `params` must carry every user-supplied argument that decides *what* gets
    destroyed: the token is fingerprinted over it, so an argument omitted here is
    an argument the caller could change between the preview and the execution.
    Do not put `confirm_token` in `params` — pass it separately.

    `preview` is a **thunk**, not a dict, and is called only when a token is
    actually being minted. Previews describe what is about to be lost, so they
    read names and counts back off the bridge; built eagerly, every confirmed
    (second) call would pay for a preview nothing consumes. The compound path gets
    this for free by building its dict inside the branch.
    """
    p = dict(params)
    if confirm_token:
        p["confirm_token"] = confirm_token
    if "confirm_token" not in p and _confirm_token_required():
        return _issue_confirm_token(action=action, params=p, preview=preview())
    return _consume_confirm_token(action=action, params=p)


# ─── Drift-guard registries ───────────────────────────────────────────────────
# tests/test_granular_guard_drift.py reads these. They are the "a new destructive
# granular tool cannot land on the wrong side of the gate" half of #138/#139: the
# test AST-scans src/granular/ for calls to the API methods below and fails if the
# enclosing tool is not registered here and does not actually call the guard.

# Resolve API method -> the compound action(s) a granular caller of it may gate
# on. A set rather than one string because `DeleteClips` is two different
# operations: MediaPool.DeleteClips (always gated) and Timeline.DeleteClips
# (gated only in its rippling form, where the compound path draws the same line).
# The receiver is not recoverable from the AST, so the guard accepts either.
GATED_API_METHODS: Dict[str, frozenset] = {
    "DeleteClips": frozenset({"media_pool.delete_clips", "timeline.delete_clips_ripple"}),
    "DeleteFolders": frozenset({"media_pool.delete_folders"}),
    "DeleteTimelines": frozenset({"media_pool.delete_timelines"}),
    "DeleteTrack": frozenset({"timeline.delete_track"}),
    "ResetAllGrades": frozenset({"graph.reset_all_grades"}),
    "ResetIntellisearchAnalysis": frozenset({"project_settings.reset_intellisearch_analysis"}),
    "GenerateSpeech": frozenset({"project_settings.generate_speech"}),
    "RemoveMotionBlur": frozenset({"folder.remove_motion_blur", "media_pool_item.remove_motion_blur"}),
}

# compound action string -> (granular module, granular tool function name).
GRANULAR_CONFIRM_SITES: Dict[str, Tuple[str, str]] = {
    "project_settings.reset_intellisearch_analysis": ("project", "reset_intellisearch_analysis"),
    "project_settings.generate_speech": ("project", "generate_speech"),
    "folder.remove_motion_blur": ("folder", "folder_remove_motion_blur"),
    "media_pool_item.remove_motion_blur": ("media_pool_item", "remove_clip_motion_blur"),
    "media_pool.delete_clips": ("media_pool", "delete_media_pool_clips"),
    "media_pool.delete_folders": ("media_pool", "delete_media_pool_folders"),
    "media_pool.delete_timelines": ("media_pool", "delete_timelines_by_id"),
    "timeline.delete_track": ("timeline", "timeline_delete_track"),
    "timeline.delete_clips_ripple": ("timeline", "timeline_delete_clips"),
    "graph.reset_all_grades": ("graph", "graph_reset_all_grades"),
}

# Resolve API method -> resolve_ai_ledger OP_META key its granular caller must
# record under. Every key here is an OP_META key; the test asserts that too, so a
# typo or a renamed op fails rather than silently writing an unknown op name.
LEDGERED_API_METHODS: Dict[str, str] = {
    "PerformAudioClassification": "perform_audio_classification",
    "ClearAudioClassification": "clear_audio_classification",
    "AnalyzeForIntellisearch": "analyze_for_intellisearch",
    "AnalyzeForSlate": "analyze_for_slate",
    "ResetIntellisearchAnalysis": "reset_intellisearch_analysis",
    "RemoveMotionBlur": "remove_motion_blur",
    "GenerateSpeech": "generate_speech",
}
