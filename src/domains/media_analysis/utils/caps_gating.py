"""Analysis-caps gating: preset/override providers, per-clip and per-manifest caps checks."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.domains.media_analysis.utils import analysis_caps as _analysis_caps

# Caps preference reader — server.py registers a provider that reads the
# media-analysis prefs file. Until then, the default preset is used.
_CAPS_PRESET_PROVIDER: Optional[Callable[[], Optional[str]]] = None
_CAPS_OVERRIDES_PROVIDER: Optional[Callable[[], Optional[Dict[str, Any]]]] = None


def register_caps_preset_provider(fn: Callable[[], Optional[str]]) -> None:
    global _CAPS_PRESET_PROVIDER
    _CAPS_PRESET_PROVIDER = fn


def register_caps_overrides_provider(fn: Callable[[], Optional[Dict[str, Any]]]) -> None:
    global _CAPS_OVERRIDES_PROVIDER
    _CAPS_OVERRIDES_PROVIDER = fn


def _resolve_active_caps() -> _analysis_caps.Caps:
    """Pull the active caps from the registered provider, falling back to defaults."""
    preset = None
    overrides = None
    if _CAPS_PRESET_PROVIDER is not None:
        try:
            preset = _CAPS_PRESET_PROVIDER()
        except Exception:
            preset = None
    if _CAPS_OVERRIDES_PROVIDER is not None:
        try:
            overrides = _CAPS_OVERRIDES_PROVIDER()
        except Exception:
            overrides = None
    return _analysis_caps.resolve_caps(preset, overrides)


def _apply_caps_to_response(payload: Any) -> Any:
    """Trim a response payload to the active caps.response_chars limit."""
    caps = _resolve_active_caps()
    return _analysis_caps.trim_response_payload(payload, caps.response_chars)


def _cap_frames_for_active_caps(frame_paths: List[str]) -> List[str]:
    """Clip `frame_paths` to caps.frames_per_clip (None = uncapped). Also
    downscales each frame in place to caps.max_frame_dim_pixels."""
    caps = _resolve_active_caps()
    capped = frame_paths
    if caps.frames_per_clip is not None and len(frame_paths) > caps.frames_per_clip:
        capped = frame_paths[: caps.frames_per_clip]
    if caps.max_frame_dim_pixels is not None:
        for path in capped:
            try:
                _analysis_caps.downscale_frame_if_needed(path, caps.max_frame_dim_pixels)
            except Exception:
                # Downscale is best-effort; original-resolution upload is acceptable.
                pass
    return capped


# Rough estimates for the pre-call budget check. Different vision providers
# tokenize images differently — these are deliberately conservative defaults
# (real cost will usually be a bit lower so refusals only fire on genuine
# overruns). Override at call sites if you have a tighter measurement.
AVG_VISION_TOKENS_PER_FRAME = 1000
AVG_TRANSCRIPTION_TOKENS_PER_SECOND = 10


def _check_caps_pre_call(
    *,
    project_root: Optional[str],
    estimated_vision_tokens: int = 0,
    clip_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Refuse the call if `estimated_vision_tokens` would blow any cumulative cap.

    Returns None if allowed, else a clean error dict suitable for return-as-is.
    Silently allows when project_root is unavailable (caps DB lives there).
    """
    if not project_root or estimated_vision_tokens <= 0:
        return None
    try:
        caps = _resolve_active_caps()
        decision = _analysis_caps.check_budget(
            project_root=project_root, caps=caps,
            estimated_vision_tokens=estimated_vision_tokens,
            clip_id=clip_id, job_id=job_id,
        )
    except Exception:
        return None  # never block on infra failure
    if decision.allowed:
        return None
    # Log the refusal so the dashboard can show recent denials. Best-effort —
    # never let logging failure mask the refusal itself.
    try:
        _analysis_caps.log_caps_event(
            project_root=project_root,
            event_type="refusal",
            reason=decision.reason,
            preset=caps.preset,
            estimated_vision_tokens=estimated_vision_tokens,
            current_usage=decision.current_usage,
            cap=decision.cap,
            headroom=decision.headroom,
            clip_id=clip_id,
            job_id=job_id,
        )
    except Exception:
        pass
    return {
        "success": False,
        "status": "caps_exhausted",
        "reason": decision.reason,
        "estimated_vision_tokens": estimated_vision_tokens,
        "current_usage": decision.current_usage,
        "cap": decision.cap,
        "headroom": decision.headroom,
        "preset": caps.preset,
        "remediation": (
            "Raise the cap via `media_analysis.set_caps_preset` (e.g. preset='generous' "
            "or preset='unlimited'), or wait for the day_bucket to roll over. "
            f"Most-binding scope: {decision.reason}."
        ),
    }


def _annotate_clip_vision_failure(clip_result: Dict[str, Any], vision: Any) -> None:
    """Lift caps-refusal info into a structured error envelope on `clip_result`.

    When vision returns `status="caps_exhausted"` (a pre-call budget refusal),
    the caller buries the cause in the per-clip `error` string. This helper
    instead writes a `{code, category, reason, remediation, message}` dict and
    surfaces a separate `caps_refusal` block with usage/cap/headroom numbers.
    Falls back to the generic "did not complete" message for non-caps failures.
    """
    caps_refusal = (
        vision
        if isinstance(vision, dict) and vision.get("status") == "caps_exhausted"
        else None
    )
    if caps_refusal:
        clip_result.update({
            "success": False,
            "error": {
                "code": "CAPS_REFUSAL",
                "category": "budget_exhausted",
                "retryable": False,
                "reason": caps_refusal.get("reason"),
                "remediation": caps_refusal.get("remediation"),
                "message": (
                    "Visual analysis refused — caps budget exhausted "
                    f"({caps_refusal.get('reason')})."
                ),
            },
            "caps_refusal": {
                "preset": caps_refusal.get("preset"),
                "estimated_vision_tokens": caps_refusal.get("estimated_vision_tokens"),
                "current_usage": caps_refusal.get("current_usage"),
                "cap": caps_refusal.get("cap"),
                "headroom": caps_refusal.get("headroom"),
            },
            "visual": vision,
        })
    else:
        clip_result.update({
            "success": False,
            "error": "Visual analysis was requested but did not complete.",
            "visual": vision,
        })


def _annotate_partial_success(manifest: Dict[str, Any]) -> None:
    """D3 — Mark batch manifests with explicit completed/failed clip-id lists.

    When N-of-M clips fail mid-batch, the caller needs to know exactly which
    clips succeeded so it can retry only the failed subset instead of redoing
    everything. We populate:
        - partial_success: True when there's a mix (some success, some fail);
          False otherwise (all-success or all-fail).
        - completed_clip_ids: list of clip_ids whose row.success is True.
        - failed_clip_ids: list of clip_ids whose row.success is False AND
          which are not in a vision-pending state (pending isn't a failure).

    For all-fail batches, set an aggregate error envelope with code=PARTIAL_FAILURE,
    category=batch_partial (per D1) so the caller's retry policy can route on it.
    """
    clips = manifest.get("clips") or []
    if not clips:
        return

    def _clip_id(row: Dict[str, Any]) -> Optional[str]:
        record = row.get("record") or {}
        return record.get("clip_id") or row.get("clip_id")

    completed_ids = [_clip_id(row) for row in clips if row.get("success")]
    completed_ids = [cid for cid in completed_ids if cid]
    failed_ids = [
        _clip_id(row) for row in clips
        if not row.get("success") and row.get("vision_status") != "pending_host_analysis"
    ]
    failed_ids = [cid for cid in failed_ids if cid]

    has_success = bool(completed_ids)
    has_failure = bool(failed_ids)
    is_partial = has_success and has_failure

    manifest["partial_success"] = is_partial
    manifest["completed_clip_ids"] = completed_ids
    manifest["failed_clip_ids"] = failed_ids

    # Only set a top-level aggregate error envelope when at least one clip
    # failed and no other top-level error has already been set (e.g. by
    # _annotate_manifest_caps_refusal). Don't clobber a more specific error.
    if has_failure and not manifest.get("error"):
        if is_partial:
            manifest["error"] = {
                "code": "PARTIAL_FAILURE",
                "category": "batch_partial",
                "retryable": False,
                "message": (
                    f"{len(failed_ids)} of {manifest.get('clip_count', len(clips))} "
                    "clip(s) failed. Other clips completed successfully."
                ),
                "remediation": (
                    "Retry only failed_clip_ids; do not re-run completed_clip_ids."
                ),
            }


def _annotate_manifest_caps_refusal(manifest: Dict[str, Any]) -> None:
    """Aggregate per-clip CAPS_REFUSAL errors onto the manifest top-level.

    Counts refusals and, if any fired, copies the first refusal's structured
    fields onto `manifest["error"]` so server.py's `executed.error` propagation
    surfaces a CAPS_REFUSAL envelope on the top-level analyze response without
    callers having to walk `manifest.clips[*].error`.
    """
    clips = manifest.get("clips") or []
    refusal_count = sum(
        1 for row in clips
        if isinstance(row.get("error"), dict) and row["error"].get("code") == "CAPS_REFUSAL"
    )
    manifest["caps_refusal_clip_count"] = refusal_count
    if refusal_count <= 0:
        return
    first_refusal = next(
        (row["error"] for row in clips
         if isinstance(row.get("error"), dict) and row["error"].get("code") == "CAPS_REFUSAL"),
        None,
    )
    if first_refusal:
        manifest["error"] = {
            "code": "CAPS_REFUSAL",
            "category": "budget_exhausted",
            "retryable": False,
            "reason": first_refusal.get("reason"),
            "remediation": first_refusal.get("remediation"),
            "message": (
                f"{refusal_count} of {manifest.get('clip_count', refusal_count)} "
                "clip(s) refused — caps budget exhausted. See manifest.clips[*].caps_refusal."
            ),
        }


def _record_caps_usage(
    *,
    project_root: Optional[str],
    clip_id: Optional[str] = None,
    job_id: Optional[str] = None,
    vision_tokens: int = 0,
    transcription_tokens: int = 0,
    frames_uploaded: int = 0,
    wall_clock_ms: int = 0,
) -> None:
    """Best-effort caps usage recording. Silently degrades if the brain DB isn't
    available (e.g. project_root not resolved)."""
    if not project_root:
        return
    try:
        caps = _resolve_active_caps()
        _analysis_caps.record_usage_all_scopes(
            project_root=project_root,
            clip_id=clip_id,
            job_id=job_id,
            vision_tokens=vision_tokens,
            transcription_tokens=transcription_tokens,
            frames_uploaded=frames_uploaded,
            wall_clock_ms=wall_clock_ms,
            preset=caps.preset,
        )
    except Exception:
        pass  # caps recording is advisory; never break the analysis pipeline



def _ensure_path_includes_standard_tool_dirs() -> None:
    """Augment os.environ['PATH'] with common tool install dirs.

    macOS GUI apps (Claude.app, Dock/Spotlight launches) inherit launchd's bare
    PATH (/usr/bin:/bin:/usr/sbin:/sbin) and never source the user's shell rc.
    That makes shutil.which("ffprobe") return None even when Homebrew has it at
    /opt/homebrew/bin/ffprobe. Subprocess calls (subprocess.run(["ffprobe"...]))
    then also fail to find the binary. Prepending the standard tool dirs here
    fixes both detection and execution for every importer of this module.
    """
    candidates = [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        "/opt/local/bin",
        "/opt/local/sbin",
    ]
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    existing = set(parts)
    additions = [d for d in candidates if os.path.isdir(d) and d not in existing]
    if additions:
        os.environ["PATH"] = os.pathsep.join(additions + parts) if parts else os.pathsep.join(additions)


_ensure_path_includes_standard_tool_dirs()


ANALYSIS_DIR_NAME = "davinci-resolve-mcp-analysis"
HIDDEN_ANALYSIS_DIR_NAME = ".davinci-resolve-mcp-analysis"
ANALYSIS_VERSION = "0.2"
ANALYSIS_INDEX_FILENAME = "index.sqlite"
ANALYSIS_REGISTRY_FILENAME = "analysis_registry.json"
ANALYSIS_INDEX_SCHEMA_VERSION = 1
ANALYSIS_REGISTRY_SCHEMA_VERSION = 1
DEFAULT_MAX_RELATED_PROJECT_ROOTS = 32
COMMAND_TIMEOUT_SECONDS = 300
HOST_CHAT_PATHS_PROVIDER = "host_chat_paths"
HOST_CHAT_VISION_PROVIDERS = {
    "host_chat_paths",
    "host_chat",
    "current_chat",
    "chat_context",
    "mcp_sampling",
}
VISION_SCHEMA_REFERENCE = "davinci_resolve_mcp.visual_analysis.v2"
DEFAULT_TRANSCRIPTION_ENABLED = True
SOURCE_TRUST_VALUES = ("auto", "filename", "low", "medium", "high")
DEFAULT_SOURCE_TRUST = "auto"


def _resolve_source_trust(options: Any) -> str:
    """Return the effective source_trust for an analysis run.

    Pulls from options.source_trust, options.vision.source_trust (and camelCase
    aliases). Defaults to "auto" (conservative-by-default — see
    _build_vision_prompt_with_source_trust for trust-tier semantics). Unknown
    values fall back to the default rather than raising; the prompt-builder
    surfaces a note in that case.
    """
    if not isinstance(options, dict):
        return DEFAULT_SOURCE_TRUST
    vision = options.get("vision") if isinstance(options.get("vision"), dict) else {}
    candidate = (
        options.get("source_trust") or options.get("sourceTrust")
        or vision.get("source_trust") or vision.get("sourceTrust")
    )
    if not candidate:
        return DEFAULT_SOURCE_TRUST
    value = str(candidate).strip().lower()
    if value in SOURCE_TRUST_VALUES:
        return value
    return DEFAULT_SOURCE_TRUST
MARKER_PLAN_DEFAULT_COLORS = {
    "shot": "Blue",
    "best_moment": "Green",
    "qc_warning": "Red",
    "black_or_title": "Red",
}

DEFAULT_VISION_ANALYSIS_PROMPT = """Return only strict JSON for editorial media analysis (schema v2).

You are producing the foundation an editorial AI uses to assemble cuts and answer
questions about this footage. Outputs are TRUSTED BY DEFAULT — downstream tools
treat them as ground truth. Be conservative: hedge identity, intent, and value
claims when frame evidence is thin. Per-field confidence is how you signal
uncertainty (low / medium / high). Description-of-what-is-visible beats
interpretation when the evidence is ambiguous. If you cannot tell, return
`unknown` or `null` — that is a valid and useful answer.

READ every frame file listed under `frame_paths` as an image. Use the full
sequence plus the computed motion / variance and cut-boundary evidence in the
payload. Describe what changes across the clip; do not treat one frame as the
whole clip unless only one frame was provided. When frames are tagged
`shot_start`, `shot_end`, `cut_before`, `cut_after`, or `flash_candidate`,
explicitly compare adjacent boundary frames and say whether they read as a real
cut, a flash frame, a title / black insertion, or a high-motion moment inside
one continuous shot.

PER-SHOT COVERAGE IS REQUIRED. The payload's `shot_table` lists every detected
shot. Emit one `shot_descriptions` entry for every `shot_index` in `shot_table`.
Each entry's content must be grounded in THAT shot's frames only — never paste
the clip summary or a neighbouring shot's content. If a shot has no associated
frames (sampler missed it), say so explicitly in `description` and set
`qc_flags: ["no_in_shot_frame_sampled"]`.

CROSS-SHOT RELATIONSHIPS. After describing every shot individually, fill the
`relationships` block on each shot with pattern observations only (which shots
appear to be the same setup, which continue action from prior shots, which look
like alternate takes). DO NOT suggest editorial pairings (no "cuts well to" /
"cuts poorly to" — those are user-side runtime queries, not stored fields).

ENUMS ARE CLOSED. Use only the documented values. If none fits, use `unknown`.

CONFIDENCE PER GROUP. Each shot carries confidence ratings per major field
group. Default `medium` unless evidence is clearly strong (`high`) or thin
(`low`). Downstream tools weight outputs by confidence.

BEST MOMENT IS NULLABLE. Only populate `best_moment` if there is a moment within
the shot an editor would naturally point to. If the shot is a sustained flat
beat, return `null` and set `best_moment_present: false`. Forced best_moments
add noise.

CONTINUITY QC. Surface eye-line and screen-direction observations as QC
questions ("possible eye-line mismatch between shots 12 and 13") — not as
assertions. Skip prop-continuity claims (we cannot reliably track props).

V2 SCHEMA:
{
  "success": true,
  "provider": "host_chat_paths",
  "schema_version": "2.0",

  "clip_summary": "Colleague-style first-impression paragraph, 2-4 sentences. Primary editorial summary; downstream tools use this as the clip's Description.",
  "clip_summary_oneliner": "Elevator-pitch single sentence describing the clip.",

  "editorial_classification": {
    "primary_use": "action|interview|b_roll|insert|establishing|montage|screen_recording|titles|finished_video|other",
    "select_potential": "low|medium|high",
    "energy_arc": "rising|falling|flat|spiky|varied|unknown",
    "style": "documentary|narrative|experimental|commercial|mixed_genre|unknown",
    "genre_indicators": [],
    "reason": "Why this classification."
  },

  "slate": {
    "slate_visible": false,
    "scene": "", "shot": "", "take": "", "camera": "", "roll": "", "date": "", "production": "",
    "visible_text": [],
    "confidence": {
      "overall": "low|medium|high",
      "scene": "low|medium|high", "shot": "low|medium|high",
      "take": "low|medium|high", "camera": "low|medium|high"
    }
  },

  "shot_descriptions": [
    {
      "shot_index": 1,
      "time_seconds_start": 0.0,
      "time_seconds_end": 1.969,
      "frame_indices_used": [1, 2, 3],

      "visual": {
        "shot_size": "wide|medium_wide|medium|medium_close|close|extreme_close|insert|establishing|other",
        "framing": "single|two_shot|group|crowd|empty|insert|establishing|abstract",
        "camera_height": "eye_level|high_angle|low_angle|birds_eye|dutch|unknown",
        "camera_motion": "locked|pan|tilt|dolly|handheld|crane|drone|zoom|composite|other",
        "motion_direction": "left|right|up|down|in|out|clockwise|counter_clockwise|none",
        "depth_of_field": "deep|shallow|rack_focus|unknown",
        "lens_character": "wide|normal|tele|fisheye|unknown",
        "lens_format": "spherical|anamorphic|fisheye|unknown",
        "lighting": "natural|high_key|low_key|practical|backlit|silhouette|mixed|unknown",
        "color_mood": "warm|cool|neutral|desaturated|saturated|monochrome|unnatural|unknown",
        "composition_notes": "Short freeform note on composition."
      },

      "content": {
        "primary_subject": {
          "type": "person|object|landscape|interior|vehicle|animal|text_graphic|abstract",
          "description": "Short concrete description.",
          "performance": {
            "eye_line": "to_camera|off_left|off_right|down|up|closed|unknown",
            "energy": "low|medium|high",
            "emotional_register": "Short freeform observation, e.g. 'looks tense, jaw clenched'. Use null if no person."
          }
        },
        "secondary_subjects": [],
        "action": "1-sentence description of what's happening.",
        "location": "1-sentence description of where this is.",
        "visible_text": [],
        "objects_of_note": [],
        "audio_character": "silence|sync_dialogue|vo_dialogue|music|ambient|sfx|mixed|unknown"
      },

      "production": {
        "composite_shot": false,
        "composite_panels": null,
        "vfx_present": "none|minor|major|unknown"
      },

      "editorial": {
        "editorial_role": "establishing|coverage|reaction|insert|transition|b_roll|montage_element|titles_or_graphics|bumper|other",
        "select_potential": "low|medium|high",
        "best_moment_present": false,
        "best_moment": null,
        "pacing": "still|moderate|kinetic|variable",
        "stillness_type": "held_tension|quiet|contemplative|transitional|dead_air|unknown|null",
        "pacing_note": "Use when pacing is still or variable; null otherwise."
      },

      "cuttability": {
        "cut_in": {"quality": "poor|ok|clean", "notes": ""},
        "cut_out": {"quality": "poor|ok|clean", "notes": ""},
        "match_action_in": false,
        "match_action_out": false,
        "cut_compatibility_hints": "Freeform notes for downstream assembly logic."
      },

      "relationships": {
        "same_setup_as": [],
        "continues_from": [],
        "alt_take_of": []
      },

      "transition_in": {"type": "cut|fade|dissolve|wipe|unknown", "duration_seconds": 0},
      "transition_out": {"type": "cut|fade|dissolve|wipe|unknown", "duration_seconds": 0},

      "confidence": {
        "visual": "low|medium|high",
        "content": "low|medium|high",
        "audio": "low|medium|high",
        "editorial": "low|medium|high",
        "cuttability": "low|medium|high"
      },

      "description": "1-3 sentences, colleague-style note, editorially useful.",
      "qc_flags": []
    }
  ],

  "cross_shot": {
    "coverage_groups": [
      {"label": "interview master + close", "shot_indices": [3, 5, 7], "setup_description": ""}
    ],
    "continuity_chains": [
      {"label": "action continues across shots 20-25", "shot_indices": [20, 21, 22, 23, 24, 25], "action_description": ""}
    ],
    "alt_take_groups": [],
    "energy_arc": "rising|falling|flat|spiky|varied|unknown"
  },

  "editing_notes": {
    "best_moments": ["List of notable clip-wide moments (separate from per-shot best_moment)."],
    "continuity_flags": [],
    "qc_flags": [],
    "search_tags": ["Keywords for cross-clip retrieval. This is what populates the clip's Keywords metadata in Resolve."]
  },

  "analysis_keyframes": [
    {
      "time_seconds": 0.0,
      "selection_reason": "first_usable|midpoint|last_usable|scene_change|cut_before|cut_after|shot_start|shot_end|shot_representative|shot_progress|flash_candidate|motion_peak|interval",
      "description": "What is visible in this frame.",
      "editing_value": "How an editor might use this moment.",
      "qc_flags": []
    }
  ],

  "qc": {
    "warnings": [],
    "continuity_observations": [
      {"kind": "eye_line|screen_direction", "shot_indices": [12, 13], "observation": "Possible eye-line break between A's looking-left in shot 12 and looking-right in shot 13.", "confidence": "low|medium|high"}
    ],
    "coverage_gaps": []
  },

  "confidence": {
    "visual": "low|medium|high",
    "motion": "computed",
    "transcript": "unavailable|provided"
  }
}

Do not include markdown fences, prose outside JSON, or keys outside this schema.
When a field is not applicable (e.g. performance fields on a landscape shot,
composite_panels when composite_shot is false, best_moment for flat shots),
use null. When evidence is thin, use the documented `unknown` enum value and
mark confidence `low`. Never invent identity, intent, or editorial value beyond
what the frames support."""

DEPTHS = {"quick", "standard", "deep", "custom"}
DEFAULT_DEPTH = "standard"
FRAME_CAPS = {
    "quick": 0,
    "standard": 8,
    "deep": 24,
    "custom": 8,
}
HARD_FRAME_CAP = 512

# ── Frame-sampling modes ─────────────────────────────────────────────────────
# How many frames a clip gets is governed by a `sampling_mode`. `depth` still
# governs *which* analysis layers run; the mode governs frame coverage + cost.
#
#   fixed           "Economy"            — flat N frames (depth-derived / max_analysis_frames),
#                                          independent of clip length. Most predictable cost.
#   per_minute      "Balanced"           — N = clamp(minutes * frames_per_minute, floor, ceiling).
#                                          Cost is linear in footage length; content-blind.
#   adaptive_capped "Thorough"           — content-aware (per-shot boundaries + flashes), bounded
#                                          by [floor, frame_ceiling]. Best coverage, bounded cost.
#   adaptive        "Thorough (uncapped)" — content-aware, bounded only by the absolute HARD_FRAME_CAP.
#                                          Use only when clips are known to be short/few.
#
# The math-layer default is `adaptive` so any caller that doesn't thread a
# sampling config keeps the legacy demand-driven behaviour. The *product*
# default (what new analysis runs use) is resolved at the preference layer in
# server.py and recommends "adaptive_capped" (Thorough).
SAMPLING_MODES = {"fixed", "per_minute", "adaptive", "adaptive_capped"}
DEFAULT_SAMPLING_MODE = "adaptive"
RECOMMENDED_SAMPLING_MODE = "adaptive_capped"
DEFAULT_FRAMES_PER_MINUTE = 4.0
DEFAULT_FRAME_FLOOR = 3
DEFAULT_FRAME_CEILING = 80

# Thoroughness ranking — used for cache reuse: a richer prior report satisfies a
# cheaper mode, but switching *up* forces a re-sample.
SAMPLING_MODE_RANK = {"fixed": 0, "per_minute": 1, "adaptive_capped": 2, "adaptive": 3}

# User-facing labels (prompt + control panel).
SAMPLING_MODE_LABELS = {
    "fixed": "Economy",
    "per_minute": "Balanced",
    "adaptive_capped": "Thorough",
    "adaptive": "Thorough (uncapped)",
}

_SAMPLING_MODE_ALIASES = {
    "economy": "fixed", "fixed": "fixed", "flat": "fixed",
    "balanced": "per_minute", "per_minute": "per_minute", "perminute": "per_minute",
    "per-minute": "per_minute", "duration": "per_minute",
    "thorough": "adaptive_capped", "adaptive_capped": "adaptive_capped",
    "adaptive-capped": "adaptive_capped", "capped": "adaptive_capped",
    "thorough_uncapped": "adaptive", "thorough (uncapped)": "adaptive",
    "adaptive": "adaptive", "uncapped": "adaptive",
}



def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)



def _timestamp_from_analyzed_at(value: Any) -> Optional[float]:
    if not value:
        return None
    raw = str(value).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        pass
    try:
        return time.mktime(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


