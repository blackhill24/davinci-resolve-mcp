"""Read-only Resolve connection helpers used by the local analysis dashboard."""

from __future__ import annotations

import functools
import os
import sys
import threading
from typing import Any, Dict, Optional, Tuple

from src.core.platform import setup_environment


def _safe_call(obj: Any, method_name: str, *args: Any) -> Tuple[Any, Optional[str]]:
    if obj is None or not hasattr(obj, method_name):
        return None, f"{method_name} unavailable"
    try:
        return getattr(obj, method_name)(*args), None
    except Exception as exc:
        return None, str(exc)


def _safe_name(obj: Any, fallback: str = "Untitled") -> str:
    value, _ = _safe_call(obj, "GetName")
    return str(value or fallback)


def _safe_id(obj: Any) -> Optional[str]:
    value, _ = _safe_call(obj, "GetUniqueId")
    return str(value) if value else None


# ── Resolve scripting API serialization ─────────────────────────────────────
# The dashboard runs on a ThreadingHTTPServer, so /api/boot, /api/projects and
# /api/resolve/media can land on separate threads concurrently (especially at
# startup). DaVinci's scripting API is not thread-safe, so every entry point that
# talks to it acquires this re-entrant lock for the full duration of its calls.
_RESOLVE_API_LOCK = threading.RLock()
_RESOLVE_ENV_READY = False


def _serialize_resolve(func):
    """Decorator: hold the Resolve API lock for the whole call."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _RESOLVE_API_LOCK:
            return func(*args, **kwargs)
    return wrapper


def _connect_resolve_read_only() -> Tuple[Any, Optional[str]]:
    global _RESOLVE_ENV_READY
    with _RESOLVE_API_LOCK:
        # Environment + sys.path setup is pure overhead and never goes stale, so
        # run it once per process rather than on every connection.
        if not _RESOLVE_ENV_READY:
            try:
                setup_environment()
                modules_path = os.environ.get("RESOLVE_SCRIPT_API")
                if modules_path:
                    candidate = os.path.join(modules_path, "Modules")
                    if candidate not in sys.path:
                        sys.path.append(candidate)
                _RESOLVE_ENV_READY = True
            except Exception as exc:
                return None, f"Resolve scripting API unavailable: {exc}"
        try:
            import DaVinciResolveScript as dvr_script  # type: ignore
        except Exception as exc:
            return None, f"Resolve scripting API unavailable: {exc}"
        try:
            resolve = dvr_script.scriptapp("Resolve")
        except Exception as exc:
            return None, f"Resolve connection failed: {exc}"
        if resolve is None:
            return None, "DaVinci Resolve is not connected. Open Resolve Studio with a project loaded."
        return resolve, None


@_serialize_resolve
def _current_resolve_project_id() -> Tuple[Optional[str], Optional[str]]:
    """(project_id, error) for the currently-open Resolve project.

    A handful of cheap API calls — used by the media-poll reuse path to detect
    when the user has switched projects in Resolve since the inventory was cached,
    without paying for a full Media Pool walk.
    """
    resolve, error = _connect_resolve_read_only()
    if error or resolve is None:
        return None, error or "Resolve unavailable"
    pm, pm_error = _safe_call(resolve, "GetProjectManager")
    if not pm or pm_error:
        return None, pm_error or "Project manager unavailable"
    project, _ = _safe_call(pm, "GetCurrentProject")
    if not project:
        return None, "No Resolve project open"
    return _safe_id(project), None


@_serialize_resolve
def _resolve_ai_features(resolve: Any) -> Dict[str, Any]:
    """Report which Resolve 21.0 AI scripting methods are available on the
    connected build, plus the Extra each AI-gated method requires. Presence is
    detected via getattr (no Resolve round-trips beyond fetching the handles),
    so this stays cheap enough for the boot handshake.
    """
    def has(obj: Any, name: str) -> bool:
        return bool(obj) and callable(getattr(obj, name, None))

    project = folder = None
    try:
        pm = resolve.GetProjectManager()
        project = pm.GetCurrentProject() if pm else None
        mp = project.GetMediaPool() if project else None
        folder = mp.GetRootFolder() if mp else None
    except Exception:
        pass

    features = {
        "disable_background_tasks": has(resolve, "DisableBackgroundTasksForCurrentResolveSession"),
        "generate_speech": has(project, "GenerateSpeech"),
        "perform_audio_classification": has(folder, "PerformAudioClassification"),
        "clear_audio_classification": has(folder, "ClearAudioClassification"),
        "analyze_for_intellisearch": has(folder, "AnalyzeForIntellisearch"),
        "analyze_for_slate": has(folder, "AnalyzeForSlate"),
        "remove_motion_blur": has(folder, "RemoveMotionBlur"),
    }
    return {
        "features": features,
        "available_count": sum(1 for v in features.values() if v),
        # Methods that additionally need an Extras download to actually run.
        "requires_extra": {
            "analyze_for_intellisearch": "AI IntelliSearch",
            "analyze_for_slate": "AI Slate ID",
            "generate_speech": "AI Speech Generator",
        },
    }


def _resolve_identity() -> Dict[str, Any]:
    resolve, error = _connect_resolve_read_only()
    if not resolve:
        return {"available": False, "error": error}
    product, _ = _safe_call(resolve, "GetProductName")
    version_string, _ = _safe_call(resolve, "GetVersionString")
    version_tuple, _ = _safe_call(resolve, "GetVersion")
    page, _ = _safe_call(resolve, "GetCurrentPage")
    return {
        "available": True,
        "product": str(product) if product else "DaVinci Resolve",
        "version_string": str(version_string) if version_string else None,
        "version": list(version_tuple) if isinstance(version_tuple, (list, tuple)) else None,
        "page": str(page) if page else None,
        "ai_features": _resolve_ai_features(resolve),
    }


# ── Resolve 21 AI Console: op dispatch ──────────────────────────────────────
# Folder/clip-level ops are routed to the consolidated `folder` /
# `media_pool_item` tools; project/resolve-level ops to their tools. The
# consolidated tools own the confirm-token gate for the two media-creators, so
# this dispatcher just relays params (incl. confirm_token) and the result.

_AI_CONSOLE_FOLDER_OPS = frozenset({
    "perform_audio_classification", "clear_audio_classification",
    "analyze_for_intellisearch", "analyze_for_slate", "remove_motion_blur",
    "transcribe_audio", "clear_transcription",
})


def _run_resolve_ai_op(body: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch one AI Console op to the right consolidated server tool.

    body = {op, target?, params?}. target is 'folder' (current Media Pool
    folder, default) or 'clip' (params.clip_id required). Returns the tool's
    response verbatim — including a {status:'confirmation_required', confirm_token,
    preview} shape for the gated media-creating ops.
    """
    op = (body.get("op") or "").strip()
    target = (body.get("target") or "folder").strip()
    params = dict(body.get("params") or {})
    if not op:
        return {"success": False, "error": "op is required"}
    try:
        from src.server import (
            folder as _folder_tool,
            media_pool_item as _mpi_tool,
            project_settings as _ps_tool,
            resolve_control as _rc_tool,
        )
    except Exception as exc:  # pragma: no cover - import guard
        return {"success": False, "error": f"server tools unavailable: {exc}"}

    if op == "disable_background_tasks":
        return _rc_tool("disable_background_tasks_for_current_session", {})
    if op == "generate_speech":
        return _ps_tool("generate_speech", params)
    if op not in _AI_CONSOLE_FOLDER_OPS:
        return {"success": False, "error": f"unknown op {op!r}"}
    if target == "clip":
        clip_id = params.get("clip_id") or body.get("clip_id")
        if not clip_id:
            return {"success": False, "error": "clip target requires a clip_id"}
        params["clip_id"] = clip_id
        return _mpi_tool(op, params)
    # default: operate on the current Media Pool folder
    return _folder_tool(op, params)


def _clip_props(clip: Any) -> Dict[str, Any]:
    props, _ = _safe_call(clip, "GetClipProperty", "")
    return props if isinstance(props, dict) else {}


def _first_prop(props: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        value = props.get(key)
        if value not in (None, ""):
            return value
    return None


