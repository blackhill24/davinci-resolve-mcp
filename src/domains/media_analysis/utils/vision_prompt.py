"""Vision-analysis prompt construction + the host-chat/local vision-analysis call."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from src.domains.media_analysis.utils.caps_gating import ANALYSIS_VERSION, AVG_VISION_TOKENS_PER_FRAME, DEFAULT_VISION_ANALYSIS_PROMPT, HOST_CHAT_PATHS_PROVIDER, HOST_CHAT_VISION_PROVIDERS, VISION_SCHEMA_REFERENCE, _cap_frames_for_active_caps, _check_caps_pre_call, _coerce_bool, _resolve_source_trust
from src.domains.media_analysis.utils.clip_identity_registry import normalize_path, short_hash, slugify
from src.domains.media_analysis.utils.technical_probe import _parse_float, _write_json


_SUMMARY_STYLE_DIRECTIVES = {
    "full": (
        "Use the full schema as written. Populate every applicable field; "
        "do not trim narrative content or omit observations to save words."
    ),
    "concise": (
        "Bias narrative fields toward brevity while keeping every schema field "
        "populated. Targets: `clip_summary` is 1-2 sentences (not 2-4); each "
        "shot `description` is 1 sentence; `composition_notes`, `pacing_note`, "
        "and `emotional_register` reduce to the single most important "
        "observation or `null` if there is nothing distinct to say. Do not "
        "drop fields; do not fabricate detail to fill them either."
    ),
    "creative": (
        "Bias narrative fields toward editorial vibes — tone, atmosphere, "
        "intent, performance, and how the shot might earn its place in a cut. "
        "`clip_summary` and shot `description` should read like an assistant "
        "editor's first-impression note (concrete imagery + editorial read), "
        "not a forensic inventory. `editorial_role`, `select_potential`, "
        "`best_moment`, `pacing`, and `emotional_register` deserve full "
        "attention. Keep `confidence` values honest and continue to hedge "
        "identity / intent claims when frame evidence is thin."
    ),
    "technical": (
        "Bias narrative fields toward camera, exposure, lighting, and QC. "
        "`composition_notes`, `framing`, `camera_height`, `camera_motion`, "
        "`lens_character`, `lens_format`, `lighting`, `color_mood`, "
        "`audio_character`, and `qc_flags` deserve full attention. Subject "
        "performance / emotional register stays terse (one observation or "
        "`null`). `clip_summary` reads like a camera operator's or QC pass "
        "note — what's in the frame technically, what works or doesn't, "
        "what an editor needs to know to use this shot."
    ),
}


def _build_summary_style_directive(value: Any) -> Optional[str]:
    """Map an analysis_summary_style value to a short narrative-tone directive
    that biases the vision model's wording without changing the schema.

    Returns the directive string, or None for `full` (default behavior).
    """
    style = (str(value).strip().lower() if value else "")
    if not style or style == "full":
        return None
    # Backwards-compat: legacy enum values get folded into the new four-option
    # scheme. Saved prefs files from older installs may still have these.
    legacy = {
        "assistant_editor": "creative",
        "assistant": "creative",
        "editor": "creative",
        "producer": "creative",
        "qc": "technical",
        "qc_focus": "technical",
        "qc_focused": "technical",
    }
    style = legacy.get(style, style)
    return _SUMMARY_STYLE_DIRECTIVES.get(style)


def _build_vision_prompt_with_source_trust(
    *,
    base_prompt: str,
    source_trust: Optional[str],
    file_path: Optional[str],
    clip_name: Optional[str],
    summary_style: Optional[str] = None,
) -> str:
    """V2 P11: Prepend a source-trust preamble when the caller has signaled
    that the clip's filename / context can be used as supporting evidence.

    Trust levels:
      - None / "auto" (default) — no preamble; conservative-by-default
      - "filename" — filename may corroborate frame evidence; hedge if uncorroborated
      - "low" / "medium" / "high" — explicit trust for all available context

    The preamble adjusts conservative-by-default tone *upward* (allows using
    available context) without raising the per-field confidence ceiling
    (vision still hedges via confidence values when evidence is thin).
    """
    trust = (str(source_trust).strip().lower() if source_trust else "auto")
    style_directive = _build_summary_style_directive(summary_style)

    if trust in ("", "auto", "none"):
        if not style_directive:
            return base_prompt
        style_preamble = (
            f"\n=== NARRATIVE STYLE ===\n"
            f"analysis_summary_style: {str(summary_style).strip().lower()}\n\n"
            f"{style_directive}\n"
            f"=== END NARRATIVE STYLE ===\n\n"
        )
        return style_preamble + base_prompt

    if trust == "filename":
        explanation = (
            "The clip filename and any visible on-screen text may be used as supporting "
            "evidence for identity, location, and editorial classification claims. Still "
            "hedge in the `confidence` fields when frame evidence alone wouldn't support "
            "the claim — the trust override raises the floor for using available context, "
            "not the ceiling for asserting facts."
        )
    elif trust == "low":
        explanation = (
            "Source trust is LOW. Treat frames as the primary evidence; ignore filename and "
            "outside context. Hedge identity / intent / value claims aggressively; default "
            "confidence to `low` unless frame evidence is unambiguous."
        )
    elif trust == "medium":
        explanation = (
            "Source trust is MEDIUM. Use frames as primary evidence; filename and visible "
            "text may corroborate. Cultural recognition (well-known people, locations, "
            "brands) is allowed when frames support it. Maintain conservative confidence."
        )
    elif trust == "high":
        explanation = (
            "Source trust is HIGH. The clip is from a known archival or trusted source. "
            "Use filename, visible text, frame evidence, and cultural recognition together "
            "to make confident editorial claims. Hedge only when sources actively conflict "
            "(e.g. filename says X but frames clearly show Y)."
        )
    else:
        # Unknown trust level — pass through with a note instead of failing
        explanation = (
            f"Source trust level '{trust}' is not a recognized value (use one of: "
            "auto, filename, low, medium, high). Defaulting to conservative-by-default."
        )

    filename_line = ""
    if file_path or clip_name:
        import os as _os
        basename = _os.path.basename(file_path) if file_path else None
        filename_line = f"\nClip filename: {basename or clip_name}"

    preamble = (
        f"\n=== SOURCE TRUST CONTEXT ===\n"
        f"source_trust: {trust}{filename_line}\n\n"
        f"{explanation}\n"
        f"=== END SOURCE TRUST CONTEXT ===\n\n"
    )
    if style_directive:
        style_preamble = (
            f"\n=== NARRATIVE STYLE ===\n"
            f"analysis_summary_style: {str(summary_style).strip().lower()}\n\n"
            f"{style_directive}\n"
            f"=== END NARRATIVE STYLE ===\n\n"
        )
        return style_preamble + preamble + base_prompt
    return preamble + base_prompt


def build_host_chat_paths_payload(
    record: Dict[str, Any],
    motion: Dict[str, Any],
    options: Dict[str, Any],
    artifacts: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the deferred-vision payload the host chat must complete via commit_vision.

    Returns a dict with absolute frame_paths, per-frame metadata, the analysis prompt,
    the response schema, and a commit_action describing the follow-up tool call.
    """
    vision = options.get("vision") or {}
    frame_metadata: List[Dict[str, Any]] = []
    frame_paths: List[str] = []
    for index, frame in enumerate(motion.get("analysis_keyframes") or [], 1):
        frame_path = frame.get("frame_path")
        if not frame_path or not os.path.isfile(frame_path):
            continue
        absolute = normalize_path(frame_path)
        frame_paths.append(absolute)
        row: Dict[str, Any] = {
            "frame_index": index,
            "frame_path": absolute,
            "time_seconds": frame.get("time_seconds"),
            "selection_reason": frame.get("selection_reason"),
            "delta_from_previous": frame.get("delta_from_previous"),
        }
        for key in (
            "cut_index", "cut_time_seconds", "boundary_role",
            "shot_index", "shot_start", "shot_end",
            "motion_peak", "motion_peak_source_reason",
        ):
            if frame.get(key) not in (None, ""):
                row[key] = frame.get(key)
        frame_metadata.append(row)

    clip_dir = artifacts.get("clip_dir") or ""
    project_root = normalize_path(os.path.dirname(os.path.dirname(clip_dir))) if clip_dir else None
    clip_id = record.get("clip_id") or record.get("media_id")
    file_path = record.get("file_path")
    vision_token = short_hash(json.dumps({
        "clip_id": clip_id,
        "file_path": file_path,
        "clip_dir": clip_dir,
        "frame_paths": frame_paths,
        "analysis_version": ANALYSIS_VERSION,
    }, sort_keys=True), length=16)

    motion_summary = {
        key: motion.get(key)
        for key in (
            "overall_motion_level",
            "average_frame_delta",
            "max_frame_delta",
            "requested_sample_budget",
            "effective_sample_budget",
            "cut_boundary_pairs_total",
            "cut_boundary_pairs_sampled",
            "cut_boundary_pair_coverage",
            "cut_boundary_sampling_capped",
        )
        if motion.get(key) is not None
    }
    cut_analysis = motion.get("cut_analysis") if isinstance(motion.get("cut_analysis"), dict) else {}
    cut_summary = {
        "cut_count": cut_analysis.get("cut_count", 0),
        "cut_density_per_minute": cut_analysis.get("cut_density_per_minute"),
        "likely_edited_sequence": bool(cut_analysis.get("likely_edited_sequence")),
        "flash_frame_candidates": cut_analysis.get("flash_frame_candidates") or [],
        "cut_points": (cut_analysis.get("cut_points") or [])[:48],
        "notes": cut_analysis.get("notes") or [],
    }

    shot_table: List[Dict[str, Any]] = []
    for shot in cut_analysis.get("shot_ranges") or []:
        if not isinstance(shot, dict):
            continue
        s_index = shot.get("index")
        s_start = _parse_float(shot.get("start"))
        s_end = _parse_float(shot.get("end"))
        if s_index in (None, "") or s_start is None or s_end is None:
            continue
        frame_indices: List[int] = []
        for row in frame_metadata:
            t = _parse_float(row.get("time_seconds"))
            if t is None:
                continue
            if s_start <= t < s_end or (s_end == t and row is frame_metadata[-1]):
                frame_indices.append(int(row.get("frame_index")))
        shot_table.append({
            "shot_index": int(s_index),
            "time_seconds_start": float(s_start),
            "time_seconds_end": float(s_end),
            "duration_seconds": float(s_end) - float(s_start),
            "frame_indices": frame_indices,
            "has_in_shot_frame": bool(frame_indices),
        })

    commit_params: Dict[str, Any] = {
        "vision_token": vision_token,
        "visual": "<host chat: fill this with JSON matching `schema`>",
    }
    if clip_id:
        commit_params["clip_id"] = str(clip_id)
    if file_path:
        commit_params["file_path"] = file_path
    if project_root:
        commit_params["analysis_root"] = project_root

    effective_source_trust = _resolve_source_trust(options)
    # Apply caps: clip frame_paths to caps.frames_per_clip and downscale each
    # frame to caps.max_frame_dim_pixels (in place). frame_metadata stays a
    # superset — the host can still see what would have been sent at higher caps.
    frame_paths_capped = _cap_frames_for_active_caps(frame_paths)
    if len(frame_paths_capped) != len(frame_paths):
        # Drop metadata rows for frames we excluded; host shouldn't be told to read
        # files we're not actually sending.
        kept_set = set(frame_paths_capped)
        frame_metadata = [m for m in frame_metadata if m.get("frame_path") in kept_set]

    # Pre-call budget refusal: estimate tokens this call WILL spend if the host
    # processes it, and refuse if any cumulative cap is exhausted. The host
    # might have a cheaper tokenizer, but estimating high is the safe default —
    # the alternative is "discovering" the overrun after the fact.
    estimated_tokens = len(frame_paths_capped) * AVG_VISION_TOKENS_PER_FRAME
    refusal = _check_caps_pre_call(
        project_root=project_root,
        estimated_vision_tokens=estimated_tokens,
        clip_id=clip_id,
        job_id=options.get("job_id") if isinstance(options, dict) else None,
    )
    if refusal is not None:
        return refusal

    payload: Dict[str, Any] = {
        "success": True,
        "status": "pending_host_analysis",
        "provider": HOST_CHAT_PATHS_PROVIDER,
        "vision_token": vision_token,
        "source_trust": effective_source_trust,
        "frame_count": len(frame_paths_capped),
        "frame_paths": frame_paths_capped,
        "frame_metadata": frame_metadata,
        "clip": {
            "clip_id": clip_id,
            "clip_name": record.get("clip_name"),
            "file_path": file_path,
        },
        "motion_summary": motion_summary,
        "cut_analysis": cut_summary,
        "shot_table": shot_table,
        "prompt": _build_vision_prompt_with_source_trust(
            base_prompt=str(vision.get("prompt") or DEFAULT_VISION_ANALYSIS_PROMPT),
            source_trust=effective_source_trust,
            summary_style=(
                options.get("analysis_summary_style") or options.get("analysisSummaryStyle")
                or vision.get("analysis_summary_style") or vision.get("analysisSummaryStyle")
            ),
            file_path=file_path,
            clip_name=record.get("clip_name"),
        ),
        "schema_reference": VISION_SCHEMA_REFERENCE,
        "commit_action": {
            "tool": "media_analysis",
            "action": "commit_vision",
            "params": commit_params,
        },
        # C3 — Host tool_choice hint. Hosts that respect this can hard-lock the
        # next API turn to media_analysis(action=commit_vision) so the agent
        # can't drift away from the deferred-vision flow. Hosts that don't
        # respect it ignore the field; the deferred-payload flow is unchanged.
        "host_tool_choice_hint": {
            "type": "tool",
            "name": "media_analysis",
            "params_template": {"action": "commit_vision", **commit_params},
            "rationale": (
                "Pending visual analysis on clip {clip}. Reading frame_paths and calling "
                "commit_vision is the only correct next action; skipping it leaves the run "
                "in pending_host_vision_analysis."
            ).format(clip=record.get("clip_id") or record.get("clip_name") or "<clip>"),
        },
        "instructions": (
            "Read every file under frame_paths as a local image using your client's "
            "image-reading capability (Claude Code's Read tool handles JPG/PNG natively). "
            "Produce a single JSON object that matches the structure of `prompt`/`schema` "
            "(no markdown fences, no prose outside JSON). The response MUST include a "
            "`shot_descriptions` entry for every `shot_index` listed in `shot_table` — "
            "each description should be grounded in the frames whose indices appear in "
            "`shot_table[i].frame_indices`, never in unrelated shots. Then call the tool in "
            "`commit_action` with `visual` set to that JSON object — the server will merge it "
            "into the analysis report, rebuild Media Pool clip markers, and publish "
            "vision-dependent metadata to Resolve. Non-vision layers "
            "(technical/loudness/scenes/motion/transcription) are already persisted under "
            "the clip's analysis directory; commit_vision finishes the run."
        ),
    }
    # Phase B — deep depth: each shot_descriptions entry must additionally
    # carry the per-shot field groups. The extra keys flow through
    # commit_vision → canonical blob → subjective_fields rows unchanged.
    if str(options.get("depth") or "").lower() == "deep":
        from src.domains.media_analysis.utils import deep_vision as _deep_vision

        payload["deep_shot_schema"] = _deep_vision.deep_shot_schema()
        payload["deep_schema_reference"] = _deep_vision.DEEP_SHOT_SCHEMA_REFERENCE
        payload["instructions"] += (
            " DEEP PASS: in addition to `description`, every shot_descriptions "
            "entry MUST include the field groups in `deep_shot_schema` (visual, "
            "content, production, editorial, cuttability, confidence), using the "
            "enum values verbatim and 'unknown'/null when frame evidence is thin."
        )
    return payload


def _vision_analysis(record: Dict[str, Any], motion: Dict[str, Any], options: Dict[str, Any], artifacts: Dict[str, Any], capabilities: Dict[str, Any]) -> Dict[str, Any]:
    vision = options.get("vision") or {}
    if not _coerce_bool(vision.get("enabled"), default=False):
        return {"success": True, "status": "skipped", "reason": "vision disabled"}
    provider = vision.get("provider") or capabilities.get("vision", {}).get("provider") or HOST_CHAT_PATHS_PROVIDER
    if provider in HOST_CHAT_VISION_PROVIDERS:
        payload = build_host_chat_paths_payload(record, motion, options, artifacts)
        # Pre-call caps refusal is returned as {success: False, status: "caps_exhausted", ...}
        # with no frame_paths key. Pass it through unchanged so the manifest-level
        # _annotate_clip_vision_failure can surface a CAPS_REFUSAL envelope instead
        # of getting overwritten by the no-frames fallthrough below.
        if not payload.get("success", True):
            return payload
        if not payload.get("frame_paths"):
            return {
                "success": False,
                "status": "skipped",
                "provider": HOST_CHAT_PATHS_PROVIDER,
                "reason": "No sampled analysis frames were available for host-chat vision.",
            }
        if artifacts.get("visual_json"):
            _write_json(artifacts["visual_json"], payload)
        return payload
    if provider not in {"mock", "local_mock"}:
        return {
            "success": False,
            "status": "skipped",
            "provider": provider,
            "reason": f"Unknown vision provider '{provider}'. Set DAVINCI_RESOLVE_MCP_VISION_PROVIDER to '{HOST_CHAT_PATHS_PROVIDER}' or use the 'mock' provider for tests.",
        }
    keyframes = []
    for frame in motion.get("analysis_keyframes", []):
        frame_row = {
            "time_seconds": frame.get("time_seconds"),
            "selection_reason": frame.get("selection_reason"),
            "description": "Local mock vision description for representative frame.",
            "editing_value": "Use as a searchable representative moment.",
            "qc_flags": [],
        }
        for key in ("cut_index", "cut_time_seconds", "boundary_role", "shot_index", "shot_start", "shot_end", "motion_peak", "motion_peak_source_reason"):
            if frame.get(key) not in (None, ""):
                frame_row[key] = frame.get(key)
        keyframes.append(frame_row)
    cut_analysis = motion.get("cut_analysis") if isinstance(motion.get("cut_analysis"), dict) else {}
    payload = {
        "success": True,
        "provider": provider,
        "clip_summary": f"Local mock visual analysis for {record.get('clip_name') or record.get('file_path')}.",
        "editorial_classification": {
            "primary_use": "unknown",
            "select_potential": "medium" if motion.get("overall_motion_level") != "low" else "low",
            "reason": "Derived from local motion/variance evidence only.",
        },
        "content": {
            "locations": [],
            "people_visible": "unknown",
            "actions": [],
            "objects": [],
            "visible_text": [],
            "notable_audio_context": [],
        },
        "shot_and_style": {
            "shot_sizes": [],
            "camera_motion": [motion.get("overall_motion_level", "unknown")],
            "composition_notes": "",
            "lighting_mood": "",
            "color_mood": "",
        },
        "motion": {
            "overall_level": motion.get("overall_motion_level", "unknown"),
            "motion_events": [],
            "quiet_regions": [],
        },
        "cut_understanding": {
            "cut_count": cut_analysis.get("cut_count", 0),
            "likely_edited_sequence": bool(cut_analysis.get("likely_edited_sequence")),
            "flash_frame_candidates": cut_analysis.get("flash_frame_candidates", []),
            "notes": cut_analysis.get("notes", []),
        },
        "analysis_keyframes": keyframes,
        "editing_notes": {
            "best_moments": [],
            "continuity_flags": [],
            "qc_flags": [],
            "search_tags": [slugify(record.get("clip_name"), "clip")],
        },
        "confidence": {
            "visual": "low",
            "motion": "computed",
            "transcript": "unavailable",
        },
    }
    if artifacts.get("visual_json"):
        _write_json(artifacts["visual_json"], payload)
    return payload


