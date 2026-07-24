#!/usr/bin/env python3
"""
DaVinci Resolve MCP Server - Application Control Utilities

This module provides functions for controlling DaVinci Resolve application:
- Quitting the application
- Checking application state
- Handling basic application functions
"""

import logging
import time
import sys
import platform
import subprocess
from typing import Dict, Any, List, Optional

from src.core.proc import resolve_spawn_env

# Configure logging
logger = logging.getLogger("davinci-resolve-mcp.app_control")
APP_CONTROL_TIMEOUT_SECONDS = 10


def _run_app_command(
    cmd: List[str],
    description: str,
    timeout: int = APP_CONTROL_TIMEOUT_SECONDS,
) -> bool:
    """Run a platform app-control command with a bounded wait."""
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("%s timed out after %ss: %s", description, timeout, cmd)
        return False
    except OSError as exc:
        logger.error("%s failed to launch: %s", description, exc)
        return False

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        logger.warning(
            "%s exited with code %s%s",
            description,
            result.returncode,
            f": {stderr}" if stderr else "",
        )
        return False
    return True


def quit_resolve_app(resolve_obj, force: bool = False, save_project: bool = True) -> bool:
    """
    Quit DaVinci Resolve application.
    
    Args:
        resolve_obj: DaVinci Resolve API object
        force: Whether to force quit even if unsaved changes (potentially dangerous)
        save_project: Whether to save the project before quitting
        
    Returns:
        True if the quit command was sent successfully
    """
    try:
        logger.info("Attempting to quit DaVinci Resolve")
        
        # Check if a project is open
        pm = resolve_obj.GetProjectManager()
        if pm:
            project = pm.GetCurrentProject()
            if project and save_project:
                logger.info("Saving project before quitting")
                # Try to save the project
                try:
                    project.SaveProject()
                except Exception as e:
                    logger.error(f"Failed to save project: {str(e)}")
                    if not force:
                        logger.error("Aborting quit due to save failure")
                        return False
        
        # Attempt to quit using the API
        if hasattr(resolve_obj, 'Quit') and callable(getattr(resolve_obj, 'Quit')):
            logger.info("Using Resolve.Quit() API")
            resolve_obj.Quit()
            return True
        
        # If Quit method isn't available or fails, use platform-specific methods
        sys_platform = platform.system().lower()
        
        if sys_platform == 'darwin':
            # macOS - use AppleScript
            logger.info("Using AppleScript to quit Resolve on macOS")
            cmd = [
                'osascript',
                '-e', 'tell application "DaVinci Resolve" to quit'
            ]
            if force:
                # Add force option if requested
                cmd = [
                    'osascript',
                    '-e', 'tell application "DaVinci Resolve" to quit with saving'
                ]
            
            return _run_app_command(cmd, "macOS Resolve quit command")
            
        elif sys_platform == 'windows':
            # Windows - use taskkill
            logger.info("Using taskkill to quit Resolve on Windows")
            if force:
                return _run_app_command(
                    ['taskkill', '/F', '/IM', 'Resolve.exe'],
                    "Windows Resolve force-quit command",
                )
            else:
                return _run_app_command(
                    ['taskkill', '/IM', 'Resolve.exe'],
                    "Windows Resolve quit command",
                )
            
        elif sys_platform == 'linux':
            # Linux - use pkill
            logger.info("Using pkill to quit Resolve on Linux")
            if force:
                return _run_app_command(
                    ['pkill', '-9', 'resolve'],
                    "Linux Resolve force-quit command",
                )
            else:
                return _run_app_command(
                    ['pkill', 'resolve'],
                    "Linux Resolve quit command",
                )
            
        # If all methods fail, return False
        logger.error("Failed to quit Resolve via any method")
        return False
        
    except Exception as e:
        logger.error(f"Error quitting DaVinci Resolve: {str(e)}")
        return False

def get_app_state(resolve_obj) -> Dict[str, Any]:
    """
    Get DaVinci Resolve application state information.
    
    Args:
        resolve_obj: DaVinci Resolve API object
        
    Returns:
        Dictionary with application state information
    """
    state = {
        "connected": resolve_obj is not None,
        "version": "Unknown",
        "product_name": "Unknown",
        "platform": platform.system(),
        "python_version": sys.version,
    }
    
    if resolve_obj:
        try:
            state["version"] = resolve_obj.GetVersionString()
        except Exception:
            logger.debug("Could not read Resolve version string", exc_info=True)
            
        try:
            state["product_name"] = resolve_obj.GetProductName()
        except Exception:
            logger.debug("Could not read Resolve product name", exc_info=True)
            
        try:
            state["current_page"] = resolve_obj.GetCurrentPage()
        except Exception:
            logger.debug("Could not read Resolve current page", exc_info=True)
            state["current_page"] = "Unknown"
            
        # Get project manager and project information
        try:
            pm = resolve_obj.GetProjectManager()
            if pm:
                state["project_manager_available"] = True
                
                current_project = pm.GetCurrentProject()
                if current_project:
                    state["project_open"] = True
                    state["project_name"] = current_project.GetName()
                    
                    # Check if timeline is open
                    current_timeline = current_project.GetCurrentTimeline()
                    if current_timeline:
                        state["timeline_open"] = True
                        state["timeline_name"] = current_timeline.GetName()
                    else:
                        state["timeline_open"] = False
                else:
                    state["project_open"] = False
            else:
                state["project_manager_available"] = False
        except Exception as e:
            state["project_error"] = str(e)
    
    return state

# How often to re-check whether the old Resolve process is gone, and how long
# to give it by default. Resolve is single-instance: relaunching while the old
# process is still shutting down makes the new one abort silently, so the
# restart polls for the exit instead of sleeping a fixed guess.
EXIT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_EXIT_WAIT_SECONDS = 30

# Per-platform "is Resolve still up?" query. pgrep exits 0 when a match exists
# and 1 when none does; tasklist always exits 0, so its output is matched.
_PROCESS_QUERIES = {
    'darwin': ['pgrep', '-x', 'DaVinci Resolve'],
    'linux': ['pgrep', '-x', 'resolve'],
    'windows': ['tasklist', '/FI', 'IMAGENAME eq Resolve.exe', '/NH'],
}


def resolve_process_running() -> Optional[bool]:
    """Is a Resolve process still up?

    Returns True/False when it could be determined, and None when it could not
    (unsupported platform, or pgrep/tasklist missing or erroring) so callers can
    tell "definitely gone" apart from "no idea".
    """
    sys_platform = platform.system().lower()
    cmd = _PROCESS_QUERIES.get(sys_platform)
    if cmd is None:
        return None
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=APP_CONTROL_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not query Resolve process state (%s): %s", cmd[0], exc)
        return None

    if sys_platform == 'windows':
        return 'Resolve.exe' in (result.stdout or '')
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    logger.warning("pgrep returned %s while querying Resolve", result.returncode)
    return None


def wait_for_resolve_exit(timeout: float, poll: float = EXIT_POLL_INTERVAL_SECONDS) -> bool:
    """Block until no Resolve process remains, up to `timeout` seconds.

    Returns True when Resolve is confirmed gone. When the process state can't be
    determined at all, falls back to the old fixed sleep and reports True — the
    caller is no worse off than before, and refusing to restart on an
    unsupported platform would be a regression.
    """
    steps = max(1, int(timeout / poll)) if poll > 0 else 1
    for _ in range(steps):
        running = resolve_process_running()
        if running is None:
            logger.warning(
                "Can't determine whether Resolve exited on this platform; "
                "falling back to a fixed %ss wait", timeout,
            )
            time.sleep(timeout)
            return True
        if not running:
            return True
        time.sleep(poll)
    return resolve_process_running() is False


def restart_resolve_app(resolve_obj, wait_seconds: int = DEFAULT_EXIT_WAIT_SECONDS) -> bool:
    """
    Restart DaVinci Resolve application.

    Args:
        resolve_obj: DaVinci Resolve API object
        wait_seconds: Maximum seconds to wait for the old process to exit before
            giving up. This is a ceiling, not a fixed delay — the relaunch fires
            as soon as the old process is confirmed gone.

    Returns:
        True if restart was initiated successfully
    """
    try:
        # Get Resolve executable path for restart
        if platform.system().lower() == 'darwin':
            resolve_path = '/Applications/DaVinci Resolve/DaVinci Resolve.app'
        elif platform.system().lower() == 'windows':
            # Default path, may need to be customized
            resolve_path = r'C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe'
        elif platform.system().lower() == 'linux':
            # Default path, may need to be customized
            resolve_path = '/opt/resolve/bin/resolve'
        else:
            return False

        # Quit Resolve
        if not quit_resolve_app(resolve_obj, force=False, save_project=True):
            logger.error("Failed to quit Resolve for restart")
            return False

        # Wait for the app to actually close. Relaunching while the old process
        # is still winding down hits Resolve's single-instance guard and the new
        # process aborts, so a slow shutdown used to make restart a silent no-op.
        logger.info("Waiting up to %ss for Resolve to close", wait_seconds)
        if not wait_for_resolve_exit(wait_seconds):
            logger.error(
                "Resolve is still running %ss after quit; not relaunching "
                "(a second instance would abort against the single-instance guard)",
                wait_seconds,
            )
            return False

        # Start Resolve again
        logger.info("Attempting to start Resolve")

        if platform.system().lower() == 'darwin':
            subprocess.Popen(['open', resolve_path])
        elif platform.system().lower() == 'windows':
            subprocess.Popen([resolve_path])
        elif platform.system().lower() == 'linux':
            subprocess.Popen(
                [resolve_path],
                stdin=subprocess.DEVNULL,
                env=resolve_spawn_env(),
                start_new_session=True,
            )

        return True
    except Exception as e:
        logger.error(f"Error restarting DaVinci Resolve: {str(e)}")
        return False
