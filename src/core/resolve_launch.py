"""The single implementation of the "start Resolve and wait for it" path.

Both live surfaces need it: `src/core/live_connection.py` (compound server) and
`src/granular/common.py` (granular server). They used to carry near-identical
copies, which had already drifted — one logged a missing-app-path error, the
other returned False silently — and meant every launch-path fix (the ALSA
spawn-env work in #93/#94, the sanitized env, `start_new_session`) had to be
applied twice or one surface silently regressed.

Callers supply their own `try_connect` probe because each owns its own
module-level `resolve` handle; everything else — where the app lives, how it is
spawned per platform, and how long to wait for it to answer — lives here.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from typing import Callable, Dict, Optional

from src.core.proc import resolve_spawn_env

logger = logging.getLogger("resolve-mcp.launch")

# Default install locations. `get_resolve_paths()` covers the scripting API
# paths; these are the *application* binaries, which Resolve does not expose.
DEFAULT_APP_PATHS: Dict[str, str] = {
    "darwin": "/Applications/DaVinci Resolve/DaVinci Resolve.app",
    "windows": r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe",
    "linux": "/opt/resolve/bin/resolve",
}

# Poll for at most attempts × interval seconds (60s) after spawning.
LAUNCH_POLL_ATTEMPTS = 30
LAUNCH_POLL_INTERVAL_SECONDS = 2.0


def resolve_app_path(sys_name: Optional[str] = None) -> Optional[str]:
    """Return the Resolve application path for a platform, or None if unsupported."""
    if sys_name is None:
        sys_name = platform.system().lower()
    return DEFAULT_APP_PATHS.get(sys_name)


def spawn_resolve(log: Optional[logging.Logger] = None) -> bool:
    """Spawn the Resolve application. Returns False if it can't be started.

    Does not wait for it to answer the scripting API — see `launch_resolve()`.
    """
    log = log or logger
    sys_name = platform.system().lower()
    app_path = DEFAULT_APP_PATHS.get(sys_name)
    if app_path is None:
        log.error("Unsupported platform for Resolve launch: %s", sys_name)
        return False
    if not os.path.exists(app_path):
        log.error("DaVinci Resolve not found at %s", app_path)
        return False

    if sys_name == "darwin":
        subprocess.Popen(["open", app_path], stdin=subprocess.DEVNULL)
    elif sys_name == "windows":
        subprocess.Popen([app_path], stdin=subprocess.DEVNULL)
    else:
        # Linux: a sanitized env (no crashy LD_PRELOAD, raw-hw ALSA conf) and a
        # detached session, so Resolve doesn't die with the MCP server and
        # doesn't inherit an audio config that hangs headless renders (#28).
        subprocess.Popen(
            [app_path],
            stdin=subprocess.DEVNULL,
            env=resolve_spawn_env(),
            start_new_session=True,
        )
    return True


def launch_resolve(
    try_connect: Callable[[], object],
    log: Optional[logging.Logger] = None,
    attempts: int = LAUNCH_POLL_ATTEMPTS,
    interval: float = LAUNCH_POLL_INTERVAL_SECONDS,
) -> bool:
    """Launch Resolve and poll `try_connect` until it answers.

    Args:
        try_connect: caller's single-shot connect probe; truthy means connected.
        log: logger to report through (defaults to this module's).
        attempts / interval: polling budget after the spawn.

    Returns:
        True once `try_connect()` returns something truthy, False if the app
        couldn't be spawned or never answered within attempts × interval.
    """
    log = log or logger
    if not spawn_resolve(log):
        return False
    log.info("Launched DaVinci Resolve, waiting for it to respond...")
    for i in range(attempts):
        time.sleep(interval)
        if try_connect():
            log.info("Resolve responded after %ss", (i + 1) * interval)
            return True
    log.warning("Resolve did not respond within %ss after launch", attempts * interval)
    return False
