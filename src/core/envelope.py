"""Response envelope + shared error taxonomy (restructure epic #52, Phase 3 / #46).

Extracted from src/server.py. The lowest-level shared module in src/core/ —
every domain action module and every other core module builds responses
through _ok()/_err(), so this file must not import from timeline_lookup,
tool_kernel, or any domain package (that would create an import cycle).
live_connection is fine to depend on: it has no dependency back on this file.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.core import resolve_busy
from src.core.live_connection import get_resolve


ERROR_CATEGORIES = (
    "precondition",          # missing current timeline/clip/project; tell user what to do
    "not_connected",         # Resolve not running or auto-launch failed
    "wrong_page",            # action requires a specific page (Color, Edit, Fairlight…)
    "invalid_input",         # caller-side fixable (bad param shape, unknown enum)
    "resolve_api_failed",    # Resolve returned None/False for unclear reasons
    "busy",                  # another long Resolve op holds the bridge; retry later
    "destructive_blocked",   # strict-mode/confirm-token refusal
    "pending_user_decision", # confirm_token required
    "unsupported",           # feature/version/method not available
    "budget_exhausted",      # caps refusal (vision/transcription/day budget)
    "timeout",               # long-running op exceeded its cap
    "batch_partial",         # mixed success in a batch op (some clips succeeded, some failed)
)

_CATEGORY_RETRYABLE_DEFAULT: Dict[str, bool] = {
    "precondition":          False,  # caller must change state first
    "not_connected":         True,   # auto-launch may succeed on next attempt
    "wrong_page":            True,   # trivial caller fix; retry after page switch
    "invalid_input":         False,  # caller fix required
    "resolve_api_failed":    True,   # often transient; retry once
    "busy":                  True,   # another long Resolve op holds the bridge
    "destructive_blocked":   False,  # user decision required
    "pending_user_decision": False,  # confirm_token required
    "unsupported":           False,  # API/version mismatch
    "budget_exhausted":      False,  # cap raise or day rollover needed
    "timeout":               True,   # may succeed if retried with more headroom
    "batch_partial":         False,  # caller must re-run only the failed subset
}

_RETRYABLE_UNSET = object()


def _err(message, *, code=None, category=None, retryable=_RETRYABLE_UNSET,
         remediation=None, reason=None, state=None):
    """Return a structured error envelope.

    Callers may pass just a message string for back-compat with the legacy shape;
    the envelope always populates code/category/retryable so the agent can route
    deterministically. Prefer naming a specific code+category at the callsite
    when the failure mode is known.

    `retryable`:
        - Omit to use the per-category default (see _CATEGORY_RETRYABLE_DEFAULT).
        - Pass True/False explicitly to override the default (rare; usually the
          default is correct).

    `state`:
        - Optional dict snapshot of the relevant values at failure time
          (e.g. {"queue_size": 0, "format": "mov"}). Machine-readable context so
          the agent doesn't have to parse `reason` prose. Omitted when empty.

    Shape:
        {"error": {"message": str, "code": str, "category": str,
                   "retryable": bool, "reason": str?, "remediation": str?,
                   "state": dict?}}
    """
    cat = category if category in ERROR_CATEGORIES else "resolve_api_failed"
    if retryable is _RETRYABLE_UNSET:
        retryable_val = _CATEGORY_RETRYABLE_DEFAULT.get(cat, False)
    else:
        retryable_val = bool(retryable)
    body = {
        "message": str(message),
        "code": code or "UNSPECIFIED",
        "category": cat,
        "retryable": retryable_val,
    }
    if reason:
        body["reason"] = str(reason)
    if remediation:
        body["remediation"] = str(remediation)
    if state:
        body["state"] = state
    return {"error": body}


def _ok(**kw):
    return {"success": True, **kw}


def _ser(obj):
    """Serialize Resolve API objects to JSON-safe values."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_ser(v) for v in obj]
    # Resolve API object — return repr
    return str(obj)


def _unknown(action, valid):
    return _err(f"Unknown action '{action}'. Valid actions: {', '.join(valid)}")


def _has_method(obj, method_name):
    # The Python bridge fabricates a callable for ANY attribute name, so
    # getattr/hasattr can never report a method as absent (verified on 21.0.0:
    # SetStart, Razor, AddNode etc. all reported present though none exist).
    # dir(obj) lists only the real methods — test membership against it.
    if obj is None:
        return False
    try:
        return method_name in dir(obj)
    except Exception:
        return False


def _requires_method(obj, method_name, min_version):
    if _has_method(obj, method_name):
        return None
    return _err(f"{method_name} requires DaVinci Resolve {min_version}+")


def _callable_method_names(obj, names: List[str]):
    out = {}
    for name in names:
        out[name] = callable(getattr(obj, name, None))
    return out


def _safe_get_property(item, key: Optional[str] = None):
    try:
        if key is None:
            return _ser(item.GetProperty()), None
        return _ser(item.GetProperty(key)), None
    except Exception as exc:
        return None, str(exc)


def _check():
    busy = resolve_busy.wait_until_free()
    if busy:
        return None, None, _err(
            f"Resolve is busy with a long operation: {busy['label']} "
            f"(running for {busy['age_seconds']}s). Retry after it completes.",
            code="RESOLVE_BUSY", category="busy",
            remediation="Wait for the named operation to finish, then retry this call.",
            state={
                "busy_with": busy["label"],
                "age_seconds": busy["age_seconds"],
                "same_process": busy["same_process"],
            },
        )
    resolve = get_resolve()
    if resolve is None:
        return None, None, _err(
            "Not connected to DaVinci Resolve. Is Resolve running?",
            code="NOT_CONNECTED", category="not_connected", retryable=True,
            remediation="Open DaVinci Resolve Studio and set Preferences > General > 'External scripting using' to Local.",
        )
    pm = resolve.GetProjectManager()
    if pm is None:
        return None, None, _err(
            "Could not get ProjectManager from Resolve",
            code="NO_PROJECT_MANAGER", category="resolve_api_failed", retryable=True,
        )
    proj = pm.GetCurrentProject()
    if not proj:
        return pm, None, _err(
            "No project open",
            code="NO_PROJECT", category="precondition",
            remediation="Open a project via project_manager(action='load', params={'name': ...}) or in the Resolve UI.",
        )
    return pm, proj, None


def _safe_clip_call(clip, method_name: str, *args):
    method = getattr(clip, method_name, None)
    if not callable(method):
        return None, f"{method_name} unavailable"
    try:
        return _ser(method(*args)), None
    except Exception as exc:
        return None, str(exc)
