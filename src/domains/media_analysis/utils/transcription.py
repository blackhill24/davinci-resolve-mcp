"""Whisper-backed transcription (CLI + mlx) and transcript normalization."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.domains.media_analysis.utils import analysis_caps as _analysis_caps

from src.domains.media_analysis.utils.capabilities_and_planning import _which_tool
from src.domains.media_analysis.utils.caps_gating import AVG_TRANSCRIPTION_TOKENS_PER_SECOND, DEFAULT_TRANSCRIPTION_ENABLED, _check_caps_pre_call, _coerce_bool, _record_caps_usage, _resolve_active_caps
from src.domains.media_analysis.utils.subtitles_and_reuse import _write_text, segments_to_srt, segments_to_vtt
from src.domains.media_analysis.utils.technical_probe import _parse_float, _read_json, _run_command, _write_json


def _normalize_word_timestamps(raw_words: Any) -> List[Dict[str, Any]]:
    words: List[Dict[str, Any]] = []
    if not isinstance(raw_words, list):
        return words
    for raw_word in raw_words:
        if not isinstance(raw_word, dict):
            continue
        text = str(raw_word.get("word", raw_word.get("text", ""))).strip()
        start = _parse_float(raw_word.get("start"))
        end = _parse_float(raw_word.get("end"))
        word: Dict[str, Any] = {
            "word": text,
            "start": start,
            "end": end if end is not None else start,
        }
        for key in ("probability", "confidence", "score"):
            value = _parse_float(raw_word.get(key))
            if value is not None:
                word[key] = value
        words.append({key: value for key, value in word.items() if value not in (None, "")})
    return words


def _normalize_transcript_payload(raw: Dict[str, Any], backend: str, language: Optional[str] = None) -> Dict[str, Any]:
    segments = []
    all_words: List[Dict[str, Any]] = []
    for segment in raw.get("segments") or []:
        start = _parse_float(segment.get("start")) or 0.0
        end = _parse_float(segment.get("end"))
        if end is None:
            end = start
        normalized_segment = {
            "start": start,
            "end": end,
            "text": str(segment.get("text", "")).strip(),
        }
        words = _normalize_word_timestamps(segment.get("words"))
        if words:
            normalized_segment["words"] = words
            all_words.extend(words)
        segments.append(normalized_segment)
    top_level_words = _normalize_word_timestamps(raw.get("words"))
    if top_level_words:
        all_words = top_level_words
    text = raw.get("text")
    if text is None:
        text = " ".join(segment.get("text", "") for segment in segments).strip()
    payload = {
        "success": True,
        "backend": backend,
        "language": raw.get("language") or language or "unknown",
        "text": text,
        "segments": segments,
    }
    if all_words:
        payload["words"] = all_words
    return payload


def _write_transcript_artifacts(payload: Dict[str, Any], artifacts: Dict[str, Any]) -> None:
    if artifacts.get("transcript_json"):
        _write_json(artifacts["transcript_json"], payload)
    if artifacts.get("transcript_srt"):
        _write_text(artifacts["transcript_srt"], segments_to_srt(payload.get("segments", [])))
    if artifacts.get("transcript_vtt"):
        _write_text(artifacts["transcript_vtt"], segments_to_vtt(payload.get("segments", [])))


def _transcribe_with_whisper_cli(path: str, artifacts: Dict[str, Any], transcription: Dict[str, Any]) -> Dict[str, Any]:
    whisper = _which_tool("whisper")
    if not whisper:
        return {"success": False, "status": "skipped", "backend": "whisper_cli", "reason": "whisper CLI not found"}
    work_dir = os.path.join(os.path.dirname(artifacts.get("transcript_json") or artifacts["analysis_json"]), "transcript-work")
    os.makedirs(work_dir, exist_ok=True)
    # Default to capturing per-word timestamps so editor / word-snap features
    # work out of the box. Callers can opt out with word_timestamps=False.
    want_words = _coerce_bool(transcription.get("word_timestamps", True), default=True)
    cmd = [
        whisper,
        path,
        "--model",
        str(transcription.get("model") or "base"),
        "--output_format",
        "json",
        "--output_dir",
        work_dir,
        "--word_timestamps",
        "True" if want_words else "False",
    ]
    if transcription.get("language"):
        cmd.extend(["--language", str(transcription["language"])])
    # Escape hatch for hosts where torch sees a GPU but the CUDA/cuDNN stack is
    # broken: pass device explicitly (option or DRM_WHISPER_DEVICE env).
    device = transcription.get("device") or os.environ.get("DRM_WHISPER_DEVICE")
    if device:
        cmd.extend(["--device", str(device)])
    code, _, stderr = _run_command(cmd, timeout=int(transcription.get("timeout", 1800)))
    if code != 0:
        return {"success": False, "backend": "whisper_cli", "error": stderr.strip() or "whisper CLI failed"}
    json_files = sorted(Path(work_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not json_files:
        return {"success": False, "backend": "whisper_cli", "error": "whisper CLI produced no JSON output"}
    raw = _read_json(str(json_files[0]))
    payload = _normalize_transcript_payload(raw, "whisper_cli", transcription.get("language"))
    _write_transcript_artifacts(payload, artifacts)
    return payload


def _transcribe_with_mlx_whisper(path: str, artifacts: Dict[str, Any], transcription: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import mlx_whisper  # type: ignore[import-not-found]
    except ImportError:
        return {"success": False, "status": "skipped", "backend": "mlx_whisper", "reason": "mlx_whisper module not found"}
    model = transcription.get("model") or "mlx-community/whisper-large-v3-turbo"
    kwargs = {}
    if transcription.get("language"):
        kwargs["language"] = transcription["language"]
    raw = mlx_whisper.transcribe(
        path,
        path_or_hf_repo=model,
        word_timestamps=_coerce_bool(transcription.get("word_timestamps", True), default=True),
        verbose=False,
        **kwargs,
    )
    payload = _normalize_transcript_payload(raw, "mlx_whisper", transcription.get("language"))
    _write_transcript_artifacts(payload, artifacts)
    return payload


def _transcribe(path: str, artifacts: Dict[str, Any], options: Dict[str, Any], capabilities: Dict[str, Any]) -> Dict[str, Any]:
    transcription = options.get("transcription") or {}
    if not _coerce_bool(transcription.get("enabled"), default=DEFAULT_TRANSCRIPTION_ENABLED):
        return {"success": True, "status": "skipped", "reason": "transcription disabled"}
    backend = transcription.get("backend")
    if not backend:
        backends = capabilities.get("transcription", {}).get("backends") or []
        backend = backends[0] if backends else None

    # Pre-call refusal: transcription token cost roughly scales with audio
    # duration. We don't always know duration upfront, but if the caller
    # injected it via options['duration_seconds'] we can estimate and refuse.
    duration_seconds = 0
    try:
        duration_seconds = int(float(options.get("duration_seconds") or transcription.get("duration_seconds") or 0))
    except (TypeError, ValueError):
        duration_seconds = 0
    if duration_seconds > 0:
        estimated_tokens = duration_seconds * AVG_TRANSCRIPTION_TOKENS_PER_SECOND
        refusal = _check_caps_pre_call(
            project_root=options.get("project_root"),
            estimated_vision_tokens=estimated_tokens,
            clip_id=options.get("clip_id"),
            job_id=options.get("job_id"),
        )
        if refusal is not None:
            refusal["backend"] = backend
            return refusal

    # Wall-clock timeout wrapper. Whisper / mlx_whisper / ffmpeg can hang on a
    # corrupt file or take far longer than expected on a long clip; cap them.
    caps = _resolve_active_caps()
    timeout = caps.wall_clock_seconds_per_call
    started_at = time.time()

    def _run_backend() -> Dict[str, Any]:
        if backend in {"mock", "local_mock"}:
            segments = transcription.get("segments") or [{"start": 0.0, "end": 1.0, "text": "Mock local transcript segment."}]
            payload = {"success": True, "backend": backend, "language": transcription.get("language", "unknown"), "segments": segments, "text": " ".join(s.get("text", "") for s in segments)}
            _write_transcript_artifacts(payload, artifacts)
            return payload
        if backend in {"whisper_cli", "mlx_whisper"}:
            if not _coerce_bool(transcription.get("allow_model_download"), default=False):
                return {
                    "success": False,
                    "status": "skipped",
                    "backend": backend,
                    "reason": "Local transcription may download model files; set allow_model_download=true explicitly to run it.",
                }
            if backend == "whisper_cli":
                return _transcribe_with_whisper_cli(path, artifacts, transcription)
            return _transcribe_with_mlx_whisper(path, artifacts, transcription)
        return {"success": False, "status": "fallthrough", "backend": backend}

    try:
        result = _analysis_caps.run_with_timeout(_run_backend, timeout)
    except _analysis_caps.WallClockTimeout as exc:
        elapsed = round((time.time() - started_at) * 1000)
        return {
            "success": False,
            "status": "wall_clock_timeout",
            "backend": backend,
            "reason": str(exc),
            "elapsed_ms": elapsed,
        }
    # Record actual wall-clock for caps usage tracking.
    try:
        elapsed_ms = round((time.time() - started_at) * 1000)
        if options.get("project_root"):
            _record_caps_usage(
                project_root=options.get("project_root"),
                clip_id=options.get("clip_id"),
                job_id=options.get("job_id"),
                wall_clock_ms=elapsed_ms,
            )
    except Exception:
        pass

    # The fallthrough for non-(mock|whisper) backends still happens via the
    # original branches below so behaviour stays identical for those.
    if result is not None and result.get("status") != "fallthrough":
        return result
    if backend in {"mock", "local_mock", "whisper_cli", "mlx_whisper"}:
        return result if result is not None else {"success": False, "backend": backend}
    elif backend == "whisper_cpp":
        if not transcription.get("model_path"):
            return {
                "success": False,
                "status": "skipped",
                "backend": backend,
                "reason": "whisper_cpp requires an explicit model_path; no model files are downloaded automatically.",
            }
        return {
            "success": False,
            "status": "not_implemented",
            "backend": backend,
            "reason": "whisper_cpp execution needs per-install CLI validation before enabling.",
        }
    elif backend == "resolve":
        return {
            "success": False,
            "status": "skipped",
            "backend": backend,
            "reason": "Resolve-native transcription mutates Resolve project state; use explicit media_pool_item/folder transcription actions.",
        }
    else:
        return {"success": False, "status": "skipped", "reason": "No local transcription backend available"}


