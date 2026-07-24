"""Lazy DaVinci Resolve connection bootstrap (restructure epic #52, Phase 3 / #46).

Extracted from src/server.py. Owns the module-level connection state
(`resolve`, `dvr_script`, the serializing lock) and the connect/launch
lifecycle every domain's tool functions call through `get_resolve()`.
"""
from __future__ import annotations

import os
import sys
import threading
import logging

from src.core.platform import get_resolve_paths
from src.core.resolve_launch import launch_resolve
from src.core import destructive_hook as _destructive_hook
from src.domains.media_analysis.utils.clip_identity_registry import (
    resolve_output_root as resolve_media_analysis_output_root,
)
from typing import Any, Optional, Tuple

logger = logging.getLogger("resolve-mcp")

paths = get_resolve_paths()
RESOLVE_API_PATH = paths["api_path"]
RESOLVE_LIB_PATH = paths["lib_path"]
RESOLVE_MODULES_PATH = paths["modules_path"]

sys.path.insert(0, RESOLVE_MODULES_PATH)
resolve = None
dvr_script = None
_resolve_lock = threading.RLock()
# Serializes synchronous tool bodies once they run off the event loop (see
# _install_threaded_tool_dispatch): the Resolve scripting bridge executes one
# call at a time, so two sync tool bodies must never enter it concurrently. No
# body re-acquires it, so a plain Lock (not RLock) states the invariant. The
# async media_analysis tool is not wrapped and does not take this lock; it is
# assumed not to run concurrently with another tool (true for a serial client).
_bridge_lock = threading.Lock()

# On Windows the fusionscript native bridge DLL must be locatable before the
# Python import machinery attempts to load it.  Setting PYTHONHOME, prepending
# the Resolve install directory to PATH, and registering it with
# os.add_dll_directory() ensures the dynamic loader can find fusionscript.dll
# and its dependencies even when the server is launched from a virtual-env or
# a working directory that is not the Resolve install directory.  These steps
# MUST happen before `import DaVinciResolveScript` or the import triggers a
# native access-violation on Windows (crash before port bind in network mode).
if sys.platform.startswith("win") and RESOLVE_LIB_PATH:
    _resolve_install_dir = os.path.dirname(RESOLVE_LIB_PATH)
    if os.path.isdir(_resolve_install_dir):
        # Ensure Python's own runtime DLLs are discoverable by the bridge.
        if not os.environ.get("PYTHONHOME"):
            os.environ["PYTHONHOME"] = sys.base_prefix
        # Prepend Resolve's install dir to PATH so the loader finds
        # fusionscript.dll and sibling DLLs even without a system-wide install.
        _cur_path = os.environ.get("PATH", "")
        if _resolve_install_dir.lower() not in _cur_path.lower():
            os.environ["PATH"] = _resolve_install_dir + os.pathsep + _cur_path
        # os.add_dll_directory is available on Python 3.8+ (Windows only).
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(_resolve_install_dir)
            except OSError:
                pass

try:
    import DaVinciResolveScript as dvr_script
    logger.info("DaVinciResolveScript module loaded")
except ImportError as e:
    logger.error(f"Cannot import DaVinciResolveScript: {e}")

def _is_resolve_handle_live(candidate) -> bool:
    """Return True when a cached Resolve handle still answers root API calls."""
    try:
        get_version = getattr(candidate, "GetVersion", None)
        if not callable(get_version):
            return False
        return bool(get_version())
    except Exception as exc:
        logger.warning(f"Cached Resolve handle is stale: {exc}")
        return False


def _try_connect():
    """Attempt to connect to Resolve once. Returns resolve object or None."""
    global resolve
    with _resolve_lock:
        if dvr_script is None:
            return None
        try:
            candidate = dvr_script.scriptapp("Resolve")
            if candidate and _is_resolve_handle_live(candidate):
                resolve = candidate
                logger.info(f"Connected: {resolve.GetProductName()} {resolve.GetVersionString()}")
            else:
                resolve = None
            return resolve
        except Exception as e:
            logger.error(f"Connection error: {e}")
            resolve = None
            return None

def _launch_resolve():
    """Launch DaVinci Resolve and wait for it to become available.

    The spawn/poll mechanics live in src/core/resolve_launch.py so this surface
    and the granular server share one implementation (#104 finding 4). The
    lambda keeps `_try_connect` late-bound, so patching it on this module still
    works.
    """
    return launch_resolve(lambda: _try_connect(), log=logger)

def get_resolve():
    """Lazy connection to Resolve — connects on first tool call, auto-launches if needed."""
    global resolve
    with _resolve_lock:
        if resolve is not None and _is_resolve_handle_live(resolve):
            return resolve
        resolve = None
        # Try to connect to an already-running Resolve.
        if _try_connect():
            return resolve
        # Not running — launch it automatically unless the caller opted out
        # (test harnesses set DAVINCI_MCP_NO_AUTOLAUNCH=1 to fail fast with
        # NOT_CONNECTED instead of blocking up to 60s on a Resolve launch).
        if os.environ.get("DAVINCI_MCP_NO_AUTOLAUNCH"):
            logger.info("Resolve not running; auto-launch disabled by DAVINCI_MCP_NO_AUTOLAUNCH")
            return None
        logger.info("Resolve not running, attempting to launch automatically...")
        _launch_resolve()
        return resolve


def _destructive_versioning_provider() -> Optional[Tuple[Any, Any, str, Optional[str]]]:
    """Provider used by the C6 version-on-mutate hook.

    Returns (resolve, project, project_root, project_name) or None if any piece
    can't be resolved. The hook degrades silently when this returns None.
    """
    try:
        r = get_resolve()
        if r is None:
            return None
        pm = r.GetProjectManager()
        if pm is None:
            return None
        proj = pm.GetCurrentProject()
        if proj is None:
            return None
        try:
            project_name = proj.GetName()
        except Exception:
            project_name = None
        try:
            project_id = proj.GetUniqueId() if hasattr(proj, "GetUniqueId") else None
        except Exception:
            project_id = None
        root = resolve_media_analysis_output_root(
            project_name=project_name,
            project_id=project_id,
            create=True,
        )
        if not root or not root.get("success"):
            return None
        return (r, proj, root["project_root"], project_name)
    except Exception as exc:
        logger.debug("destructive versioning provider failed: %s", exc)
        return None


# Late-binding wrapper: resolve the provider attribute at call time so tests
# (and hot-patching) that replace _destructive_versioning_provider on this
# module are honored by the hook.
_destructive_hook.register_project_root_provider(
    lambda: _destructive_versioning_provider())
