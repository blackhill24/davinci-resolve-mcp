"""DashboardState + repo/MCP-status/install/transport/update helpers."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from src.core import timeline_brain_db as _timeline_brain_db
from src.domains.media_analysis.utils.media_analysis_jobs import (
    list_batch_jobs,
    project_root_for_dashboard,
)
from src.dashboard.project_context import _context_from_project_root, _context_payload, _current_resolve_project_context, _load_resolve_project_context, discover_project_contexts


class DashboardState:
    def __init__(self, project_name: str, project_id: str, analysis_root: str):
        self.base_analysis_root = os.path.realpath(os.path.abspath(os.path.expanduser(str(analysis_root))))
        if project_name == "Dashboard Analysis" and project_id == "dashboard":
            current = _current_resolve_project_context(self.base_analysis_root)
            if current:
                project_name = current["project_name"]
                project_id = current.get("project_id")
        self.project_name = project_name
        self.project_id = project_id
        root = project_root_for_dashboard(
            project_name=project_name,
            project_id=project_id,
            analysis_root=self.base_analysis_root,
        )
        if not root.get("success"):
            raise RuntimeError(root.get("error") or "Invalid analysis root")
        self.output_root = root
        self.project_root = root["project_root"]
        self.lock = threading.Lock()

    def context(self) -> Dict[str, Any]:
        return _context_payload(self.project_name, self.project_id, self.output_root, source="active")

    def projects(self) -> Dict[str, Any]:
        return discover_project_contexts(self.base_analysis_root, self.context())

    def related_project_roots(self) -> List[str]:
        return list(self.projects().get("related_project_roots") or [])

    def set_context(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        project_root = payload.get("project_root") or payload.get("projectRoot")
        project_name = payload.get("project_name") or payload.get("projectName")
        project_id = payload.get("project_id") or payload.get("projectId")
        load_resolve_project = bool(payload.get("load_resolve_project") or payload.get("loadResolveProject"))
        if load_resolve_project:
            resolve_project_name = payload.get("resolve_project_name") or payload.get("resolveProjectName") or project_name
            resolve_project_folder_path = payload.get("resolve_project_folder_path") or payload.get("resolveProjectFolderPath")
            loaded = _load_resolve_project_context(self.base_analysis_root, resolve_project_name, resolve_project_folder_path)
            if not loaded.get("success"):
                return loaded
            active = loaded["active"]
            self.project_name = active["project_name"]
            self.project_id = active.get("project_id")
            self.output_root = loaded["output_root"]
            self.project_root = active["project_root"]
            return {"success": True, "active": self.context(), "projects": self.projects()}
        if project_root:
            context = _context_from_project_root(self.base_analysis_root, str(project_root), source="selected")
            if not context:
                candidate_root = os.path.realpath(os.path.abspath(os.path.expanduser(str(project_root))))
                try:
                    under_base = os.path.commonpath([candidate_root, self.base_analysis_root]) == self.base_analysis_root
                except ValueError:
                    under_base = False
                if not under_base or not project_name:
                    return {"success": False, "error": "Project context must be under the analysis base root"}
                project_directory = os.path.basename(candidate_root)
                context = {
                    "project_name": project_name,
                    "project_id": project_id,
                    "project_root": candidate_root,
                    "project_directory": project_directory,
                }
            project_name = project_name or context["project_name"]
            project_id = project_id if project_id not in (None, "") else context.get("project_id")
            output_root = {
                "success": True,
                "analysis_version": None,
                "base_root": self.base_analysis_root,
                "project_root": context["project_root"],
                "project_directory": context["project_directory"],
                "project_name": project_name,
                "project_id": project_id,
                "errors": [],
            }
        else:
            if not project_name:
                return {"success": False, "error": "project_name or project_root is required"}
            output_root = project_root_for_dashboard(
                project_name=project_name,
                project_id=project_id,
                analysis_root=self.base_analysis_root,
            )
            if not output_root.get("success"):
                return output_root
        self.project_name = str(project_name or output_root.get("project_name") or "Project")
        self.project_id = str(project_id) if project_id not in (None, "") else None
        self.output_root = output_root
        self.project_root = output_root["project_root"]
        return {"success": True, "active": self.context(), "projects": self.projects()}


DOC_SOURCES = {
    "readme": {"title": "README", "path": "README.md"},
    "analysis-guide": {"title": "Media Analysis Guide", "path": "docs/guides/media-analysis-guide.md"},
    "agent-skill": {"title": "Agent Skill", "path": "docs/SKILL.md"},
    "advanced-server": {"title": "Advanced Server", "path": "resolve-advanced/README.md"},
    "release-notes": {"title": "Release Notes", "path": "CHANGELOG.md"},
}


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _mcp_version() -> str:
    try:
        from src.server import VERSION as _server_version  # type: ignore
        return str(_server_version)
    except Exception:
        try:
            pkg_path = os.path.join(_repo_root(), "package.json")
            with open(pkg_path, "r", encoding="utf-8") as handle:
                return str(json.load(handle).get("version") or "unknown")
        except Exception:
            return "unknown"


def _request_is_loopback(handler: BaseHTTPRequestHandler) -> bool:
    """Defensive guard: only allow privileged routes from loopback clients."""
    try:
        addr = (handler.client_address or ("",))[0]
    except Exception:
        return False
    return addr in {"127.0.0.1", "::1", "localhost"}


_ALLOWED_REQUEST_HOSTNAMES = {"127.0.0.1", "::1", "localhost"}


def _header_hostname(value: str) -> Optional[str]:
    """Hostname part of a Host header or Origin URL, lowercased.

    Handles ports and bracketed IPv6 (``[::1]:8899``). Returns None when the
    value cannot be parsed — callers treat that as not-localhost.
    """
    value = value.strip()
    if not value:
        return None
    try:
        if "://" in value:
            return urlparse(value).hostname
        return urlparse(f"//{value}").hostname
    except ValueError:
        return None


def _request_origin_ok(handler: BaseHTTPRequestHandler) -> bool:
    """Block DNS-rebinding and cross-site browser requests.

    A loopback *client address* is not enough on its own: the user's browser is
    itself a loopback client, so any web page it renders can fire requests at
    this server — cross-site form/fetch POSTs directly, and reads via DNS
    rebinding (an attacker hostname resolving to 127.0.0.1 puts the page
    same-origin with us). So the Host header must name localhost, and when a
    browser supplies an Origin it must be a localhost origin too. Requests
    without Host/Origin (curl, the panel launcher) pass — they are not
    browser-mediated, and loopback bind already limits who can connect.
    """
    # An explicit non-loopback bind (--host on main.py) is an operator opt-in
    # to LAN use; legitimate Host values are then unknowable here (any of the
    # machine's addresses), so the guard only enforces in the default
    # loopback-bind mode — which is also the mode DNS rebinding targets.
    try:
        bound = str(handler.server.server_address[0]).lower()
        if bound not in {"127.0.0.1", "::1", "localhost"}:
            return True
    except Exception:
        pass
    host = (handler.headers.get("Host") or "").strip()
    if host and _header_hostname(host) not in _ALLOWED_REQUEST_HOSTNAMES:
        return False
    origin = (handler.headers.get("Origin") or "").strip()
    # "null" (sandboxed iframe, file://) is still an attacker-reachable origin.
    if origin and _header_hostname(origin) not in _ALLOWED_REQUEST_HOSTNAMES:
        return False
    return True


def _launch_claude_code_terminal() -> Dict[str, Any]:
    """Open a Terminal/iTerm window at the MCP server's project root running
    the ``claude`` CLI. macOS only — other platforms return a clipboard-only
    hint so the dashboard can show a sensible message instead of silently no-op.
    """
    repo_root = _repo_root()
    if sys.platform != "darwin":
        return {
            "success": False,
            "error": "Terminal launch is macOS-only. Open your terminal at the project root and run `claude`, then paste the prompt.",
        }
    import shlex
    import shutil
    import subprocess
    claude_bin = shutil.which("claude") or "claude"
    cmd = f"cd {shlex.quote(repo_root)} && {shlex.quote(claude_bin)}"
    iterm_running = False
    try:
        check = subprocess.run(
            ["osascript", "-e", 'application "iTerm" is running'],
            capture_output=True, text=True, timeout=8,
        )
        iterm_running = (check.stdout or "").strip().lower() == "true"
    except Exception:
        iterm_running = False
    if iterm_running:
        script = (
            'tell application "iTerm"\n'
            '  activate\n'
            f'  create window with default profile command "{cmd}"\n'
            'end tell'
        )
    else:
        escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'tell application "Terminal"\n'
            '  activate\n'
            f'  do script "{escaped}"\n'
            'end tell'
        )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return {"success": False, "error": (proc.stderr or "").strip() or "osascript failed"}
        return {
            "success": True,
            "terminal": "iterm" if iterm_running else "terminal",
            "cwd": repo_root,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _native_directory_picker(initial: Optional[str] = None) -> Dict[str, Any]:
    """Open a native OS folder picker on the *server* machine and return the
    absolute path. The dashboard runs on localhost so the picker pops on the
    user's machine — works in every browser because the browser is never asked
    to expose a filesystem path.
    """
    initial_dir = initial if initial and os.path.isdir(initial) else os.path.expanduser("~")
    if sys.platform == "darwin":
        # AppleScript picker — works without a Python Tk binding installed.
        script = (
            'tell application "System Events" to activate\n'
            f'set chosenFolder to choose folder with prompt "Select a folder" default location POSIX file "{initial_dir}"\n'
            'POSIX path of chosenFolder'
        )
        try:
            import subprocess
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                if "User canceled" in stderr or "cancelled" in stderr.lower():
                    return {"success": True, "canceled": True}
                return {"success": False, "error": stderr or "AppleScript picker failed"}
            path = (proc.stdout or "").strip().rstrip("/")
            if not path:
                return {"success": True, "canceled": True}
            return {"success": True, "path": path}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
    # Fallback: tkinter on Linux/Windows. Requires a display.
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as exc:
        return {"success": False, "error": f"native picker unavailable: {exc}"}
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(initialdir=initial_dir, title="Select a folder")
        root.destroy()
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not path:
        return {"success": True, "canceled": True}
    return {"success": True, "path": path}


def _load_installer_module():
    """Lazy-import install.py from the repo root. Returns (module, error)."""
    try:
        sys.path.insert(0, _repo_root())
        import importlib
        if "install" in sys.modules:
            return importlib.reload(sys.modules["install"]), None
        import install  # type: ignore
        return install, None
    except Exception as exc:
        return None, f"installer module unavailable: {exc}"


def _resolve_mcp_paths() -> Dict[str, Any]:
    """Return the paths needed to write an MCP client config:
    python_path, server_path, api_path, lib_path. Each may be None.
    """
    installer, error = _load_installer_module()
    repo = _repo_root()
    # Server entrypoint — same one the installer wires up.
    server_path = os.path.join(repo, "src", "resolve_mcp_server.py")
    # Prefer the repo venv python so the configured client launches the
    # MCP server with all dependencies available.
    candidates = [
        os.path.join(repo, "venv", "bin", "python"),
        os.path.join(repo, "venv", "Scripts", "python.exe"),
        os.path.join(repo, ".venv", "bin", "python"),
    ]
    python_path = next((c for c in candidates if os.path.isfile(c)), None)
    if not python_path:
        python_path = sys.executable
    api_path = lib_path = None
    if installer is not None:
        try:
            api_path, lib_path = installer.find_resolve_paths()
        except Exception:
            pass
    return {
        "python_path": str(python_path) if python_path else None,
        "server_path": str(server_path) if os.path.isfile(server_path) else None,
        "api_path": str(api_path) if api_path else None,
        "lib_path": str(lib_path) if lib_path else None,
        "installer_error": error,
    }


def _mcp_status_payload() -> Dict[str, Any]:
    """Return MCP server identity + per-client install status."""
    installer, error = _load_installer_module()
    paths = _resolve_mcp_paths()
    clients_out: List[Dict[str, Any]] = []
    if installer is None:
        return {
            "success": False,
            "error": error or "installer unavailable",
            "server": {
                "version": _mcp_version(),
                "python_path": paths.get("python_path"),
                "server_path": paths.get("server_path"),
            },
            "clients": [],
        }
    for client in getattr(installer, "MCP_CLIENTS", []):
        try:
            config_path = client["get_path"]()
        except Exception:
            config_path = None
        config_key = client.get("config_key", "mcpServers")
        available = config_path is not None
        installed = False
        entry: Any = None
        if available and config_path and os.path.isfile(str(config_path)):
            try:
                existing = installer.read_json(str(config_path))
                entry = (existing.get(config_key) or {}).get("davinci-resolve")
                installed = bool(entry)
            except Exception:
                installed = False
        clients_out.append({
            "id": client["id"],
            "name": client["name"],
            "notes": client.get("notes", ""),
            "config_path": str(config_path) if config_path else None,
            "config_key": config_key,
            "available": bool(available),
            "installed": bool(installed),
        })
    return {
        "success": True,
        "server": {
            "version": _mcp_version(),
            "python_path": paths.get("python_path"),
            "server_path": paths.get("server_path"),
            "resolve_api_path": paths.get("api_path"),
            "resolve_lib_path": paths.get("lib_path"),
            "resolve_api_detected": bool(paths.get("api_path")),
            "resolve_lib_detected": bool(paths.get("lib_path")),
        },
        "clients": clients_out,
        "transport": _transport_status(),
    }


def _transport_status() -> Dict[str, Any]:
    """Live networked-transport status (or local-only) for the MCP diagnostics card."""
    try:
        from src.core.mcp_transport import read_transport_state
    except Exception:
        return {"networked": False, "mode": "stdio (local)"}
    state = read_transport_state()
    if not state:
        return {"networked": False, "mode": "stdio (local)"}
    return {
        "networked": True,
        "mode": state.get("transport"),
        "url": state.get("url"),
        "loopback": state.get("loopback", True),
        "has_token": bool(state.get("token")),
        "token": state.get("token"),
        "pid": state.get("pid"),
    }


def _transport_start() -> Dict[str, Any]:
    """Spawn a networked MCP instance (streamable-http, loopback + token)."""
    import subprocess as _sp
    from src.core.mcp_transport import read_transport_state
    if read_transport_state():
        return {"success": False, "error": "A networked transport instance is already running."}
    paths = _resolve_mcp_paths()
    py, script = paths.get("python_path"), paths.get("server_path")
    if not py or not script:
        return {"success": False, "error": "Could not resolve the Python interpreter or server script path."}
    try:
        _sp.Popen(
            [py, script, "--transport", "streamable-http"],
            stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return {"success": False, "error": f"Failed to launch: {exc}"}
    import time as _t
    for _ in range(15):
        _t.sleep(0.2)
        if read_transport_state():
            return {"success": True, "transport": _transport_status()}
    return {"success": True, "note": "Launch initiated; status will appear shortly."}


def _transport_stop() -> Dict[str, Any]:
    """Stop the running networked MCP instance via its state-file PID."""
    import signal as _sig
    from src.core.mcp_transport import read_transport_state, clear_transport_state
    state = read_transport_state()
    if not state:
        return {"success": True, "note": "No networked transport running."}
    pid = state.get("pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, _sig.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    clear_transport_state()
    return {"success": True}


def _mcp_install_payload(client_id: str) -> Dict[str, Any]:
    """Write the MCP entry for one client by delegating to install.write_client_config."""
    installer, error = _load_installer_module()
    if installer is None:
        return {"success": False, "error": error or "installer unavailable"}
    target = next((c for c in installer.MCP_CLIENTS if c["id"] == client_id), None)
    if not target:
        return {"success": False, "error": f"unknown client: {client_id}"}
    paths = _resolve_mcp_paths()
    if not paths.get("server_path"):
        return {"success": False, "error": "MCP server script not found in repo"}
    if not paths.get("python_path"):
        return {"success": False, "error": "Python interpreter not found"}
    if not paths.get("api_path") or not paths.get("lib_path"):
        return {
            "success": False,
            "error": "Resolve scripting API / library paths could not be auto-detected. Open Resolve Studio at least once or install via install.py.",
        }
    try:
        ok, message = installer.write_client_config(
            target,
            paths["python_path"],
            paths["server_path"],
            paths["api_path"],
            paths["lib_path"],
            dry_run=False,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {"success": bool(ok), "message": message, "client_id": client_id}


def _mcp_uninstall_payload(client_id: str) -> Dict[str, Any]:
    """Remove the davinci-resolve entry from a client's config file."""
    installer, error = _load_installer_module()
    if installer is None:
        return {"success": False, "error": error or "installer unavailable"}
    target = next((c for c in installer.MCP_CLIENTS if c["id"] == client_id), None)
    if not target:
        return {"success": False, "error": f"unknown client: {client_id}"}
    try:
        config_path = target["get_path"]()
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not config_path or not os.path.isfile(str(config_path)):
        return {"success": True, "message": "Nothing to remove (config file does not exist).", "client_id": client_id}
    config_key = target.get("config_key", "mcpServers")
    try:
        existing = installer.read_json(str(config_path))
        servers = existing.get(config_key) or {}
        if "davinci-resolve" not in servers:
            return {"success": True, "message": "Nothing to remove (entry not present).", "client_id": client_id}
        del servers["davinci-resolve"]
        if servers:
            existing[config_key] = servers
        else:
            existing.pop(config_key, None)
        installer.write_json(str(config_path), existing)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "message": f"Removed davinci-resolve from {config_path}", "client_id": client_id}


def _list_active_batch_jobs(project_root: str) -> List[Dict[str, Any]]:
    """Across the analysis base root, find batch jobs with status='running'.

    Used by the update apply path: refuse to update mid-job because it would
    corrupt in-flight clip analysis state (the new build's schema may not match
    what the running batch was started against).
    """
    out: List[Dict[str, Any]] = []
    if not project_root:
        return out
    base = os.path.dirname(os.path.normpath(project_root))
    if not os.path.isdir(base):
        return out
    for entry in sorted(os.listdir(base)):
        candidate = os.path.join(base, entry)
        if not os.path.isdir(candidate):
            continue
        try:
            payload = list_batch_jobs(candidate, limit=200)
        except Exception:
            continue
        for job in payload.get("jobs") or []:
            if (job.get("status") or "").lower() == "running":
                out.append({
                    "project_root": candidate,
                    "job_id": job.get("job_id"),
                    "status": job.get("status"),
                    "started_at": job.get("started_at"),
                })
    return out


def _update_apply_payload(*, strategy: str = "refuse_on_dirty", force_active_jobs: bool = False,
                          project_root: Optional[str] = None) -> Dict[str, Any]:
    """Apply a guarded git fast-forward update by delegating to install.py's
    existing apply_safe_self_update. Returns a structured result for the UI.
    """
    # Active-job lock — refuse to update if a batch analysis job is mid-flight.
    # Pass force_active_jobs=true to override (the user explicitly accepts the
    # risk of in-flight state being inconsistent with the new build).
    if not force_active_jobs and project_root:
        active = _list_active_batch_jobs(project_root)
        if active:
            return {
                "success": False,
                "reason": "active_jobs",
                "message": f"{len(active)} batch analysis job(s) are currently running. Cancel them or pass force=true to override.",
                "active_jobs": active,
            }
    try:
        sys.path.insert(0, _repo_root())
        from install import apply_safe_self_update  # type: ignore
    except Exception as exc:
        return {"success": False, "error": f"update helper unavailable: {exc}"}
    try:
        result = apply_safe_self_update(_repo_root(), dry_run=False, initiator="dashboard", strategy=strategy)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    out: Dict[str, Any] = {
        "success": bool(result.get("success")),
        "changed": bool(result.get("changed")),
        "reason": result.get("reason"),
        "message": result.get("message"),
        "current_version": _mcp_version(),
        "from_version": result.get("from_version"),
        "to_version": result.get("to_version"),
        "from_sha": result.get("from_sha"),
        "to_sha": result.get("to_sha"),
    }
    if result.get("success") and result.get("changed"):
        out["restart_required"] = True
        # Eagerly migrate per-project DBs so schema bumps in the new build
        # surface immediately instead of waiting for the next analysis call.
        out["db_migrations"] = _eager_migrate_after_update(project_root)
        # Drop a restart-needed marker the host / dashboard can poll for.
        _write_restart_marker(_repo_root(), result)
    # Surface stash status on the result.
    for k in ("stash_ref", "stash_pop_conflict", "remediation"):
        if k in result and result[k] is not None:
            out[k] = result[k]
    return out


def _write_restart_marker(repo_root: str, update_result: Dict[str, Any]) -> None:
    """Drop a `.mcp_restart_needed` marker file with update metadata.

    The MCP server is a child process of the host (Claude Code, etc.) so we
    can't restart it ourselves. The marker is a hint the host can poll via
    `/api/restart_needed` or by reading the file directly.
    """
    log_dir = os.path.join(repo_root, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        marker = {
            "needed": True,
            "from_version": update_result.get("from_version"),
            "to_version": update_result.get("to_version"),
            "from_sha": update_result.get("from_sha"),
            "to_sha": update_result.get("to_sha"),
            "applied_at": _now_iso_safe(),
        }
        with open(os.path.join(log_dir, ".mcp_restart_needed"), "w", encoding="utf-8") as fh:
            json.dump(marker, fh, indent=2)
    except OSError:
        pass


def _now_iso_safe() -> str:
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def _read_restart_marker(repo_root: str) -> Dict[str, Any]:
    path = os.path.join(repo_root, "logs", ".mcp_restart_needed")
    if not os.path.isfile(path):
        return {"needed": False}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            payload.setdefault("needed", True)
            return payload
    except (OSError, json.JSONDecodeError):
        pass
    return {"needed": True, "marker_path": path}


def _clear_restart_marker(repo_root: str) -> Dict[str, Any]:
    path = os.path.join(repo_root, "logs", ".mcp_restart_needed")
    try:
        os.remove(path)
    except OSError:
        return {"success": False, "error": "marker not present or unreadable"}
    return {"success": True}


def _eager_migrate_after_update(project_root: Optional[str] = None) -> Dict[str, Any]:
    """Walk every project under the analysis base root and open + migrate its
    timeline_brain.sqlite. Surfaces schema bumps from the new build right after
    `git pull` instead of on next per-project work."""
    if project_root:
        base = os.path.dirname(os.path.normpath(project_root))
    else:
        base = os.path.expanduser("~/Documents/davinci-resolve-mcp-analysis")
    migrated: List[Dict[str, Any]] = []
    if not os.path.isdir(base):
        return {"success": True, "migrated": migrated, "note": "no base root found"}
    for entry in sorted(os.listdir(base)):
        candidate = os.path.join(base, entry)
        if not os.path.isdir(os.path.join(candidate, "_soul")):
            continue
        try:
            _timeline_brain_db.connect(candidate)
            migrated.append({"project_root": candidate, "ok": True})
        except Exception as exc:
            migrated.append({"project_root": candidate, "ok": False, "error": str(exc)})
    return {"success": True, "migrated": migrated}


def _update_rollback_payload() -> Dict[str, Any]:
    try:
        sys.path.insert(0, _repo_root())
        from install import rollback_to_previous_build  # type: ignore
    except Exception as exc:
        return {"success": False, "error": f"rollback helper unavailable: {exc}"}
    try:
        result = rollback_to_previous_build(_repo_root(), initiator="dashboard")
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    out: Dict[str, Any] = dict(result)
    out["current_version"] = _mcp_version()
    if result.get("success"):
        out["restart_required"] = True
    return out


def _update_history_payload(limit: int = 20) -> Dict[str, Any]:
    try:
        sys.path.insert(0, _repo_root())
        from install import read_update_history  # type: ignore
    except Exception as exc:
        return {"success": False, "error": f"history helper unavailable: {exc}", "entries": []}
    try:
        return read_update_history(_repo_root(), limit=limit)
    except Exception as exc:
        return {"success": False, "error": str(exc), "entries": []}


def _update_preview_payload() -> Dict[str, Any]:
    """Render the about-to-apply update for user confirmation.

    Returns release notes, flagged breaking changes, channel, prerelease flag,
    and the target SHA so the dashboard can show a meaningful modal before
    `git pull` actually runs.
    """
    try:
        sys.path.insert(0, _repo_root())
        from install import preview_update  # type: ignore
    except Exception as exc:
        return {"success": False, "error": f"preview helper unavailable: {exc}"}
    try:
        return preview_update(_repo_root())
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _update_status_payload(project_root: Optional[str], *, force: bool = False) -> Dict[str, Any]:
    current = _mcp_version()
    base = {
        "success": True,
        "current_version": current,
        "update_available": False,
        "status": "unknown",
    }
    # Always use the repo root, not the analysis project root — that's where the
    # MCP server's startup check writes its state file, so dashboard and server
    # share cache instead of running independent checks.
    update_project_dir = _repo_root()
    try:
        from src.core.update_check import (
            check_for_updates,
            get_cached_update_status,
        )
    except Exception as exc:
        base["error"] = f"update check unavailable: {exc}"
        return base
    try:
        if force:
            payload = check_for_updates(current, update_project_dir, force=True)
        else:
            payload = get_cached_update_status(update_project_dir, current_version=current)
            # Cache miss → run a real check now so the UI gets a useful answer
            # on first load instead of "hasn't run yet".
            if isinstance(payload, dict) and payload.get("status") == "unknown":
                payload = check_for_updates(current, update_project_dir)
    except Exception as exc:
        base["error"] = str(exc)
        return base
    if not isinstance(payload, dict):
        return base
    status = str(payload.get("status") or "unknown")
    latest = payload.get("latest_version") or payload.get("latest")
    update_available = status == "update_available"
    return {
        "success": True,
        "current_version": current,
        "latest_version": str(latest) if latest else None,
        "status": status,
        "update_available": bool(update_available),
        "checked_at": payload.get("checked_at_iso") or payload.get("checked_at"),
        "snooze_until": payload.get("snooze_until_iso") or payload.get("snooze_until"),
        "update_mode": payload.get("update_mode"),
        "release_url": payload.get("release_url") or payload.get("html_url"),
        "release_notes": payload.get("release_notes") or payload.get("body"),
    }


def _dashboard_doc(doc_id: Any) -> Dict[str, Any]:
    key = str(doc_id or "readme")
    source = DOC_SOURCES.get(key)
    if not source:
        return {"success": False, "error": "Unknown document"}
    repo_root = _repo_root()
    rel_path = str(source["path"])
    path = os.path.abspath(os.path.join(repo_root, rel_path))
    if not path.startswith(repo_root + os.sep):
        return {"success": False, "error": "Document path escaped repository root"}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "doc": key,
        "title": source["title"],
        "path": rel_path,
        "content": content,
    }



# ── Advanced server (Node) — read-only panel bridge ─────────────────────────
# The panel inspects advanced-server state through resolve-advanced/scripts/
# panel-bridge.mjs (one-shot JSON; capabilities + a read-only lineage subset).
# Mutations (ingest, QC runs, patches) stay with the MCP tools by design.

_ADVANCED_LINEAGE_OPS = {"list", "show", "diff", "verdicts"}


def _advanced_root() -> str:
    from src.core.advanced_bridge import advanced_root

    return advanced_root()


def _run_advanced_bridge(surface: str, op: str, args: Optional[Dict[str, Any]] = None,
                         timeout: float = 30.0) -> Dict[str, Any]:
    # Read-only inspection path (capabilities|lineage). The reusable subprocess
    # machinery lives in src/core/advanced_bridge — this stays a thin wrapper so
    # the panel and the auto_edit drt surgery share one source of truth.
    from src.core.advanced_bridge import run_panel_bridge

    return run_panel_bridge(surface, op, args, timeout=timeout)


def _advanced_capabilities_payload() -> Dict[str, Any]:
    from src.core.advanced_bridge import node_path

    payload = _run_advanced_bridge("capabilities", "get")
    payload["node"] = node_path()
    payload["root"] = _advanced_root()
    return payload


def _advanced_lineage_payload(op: str, params: Mapping[str, str]) -> Dict[str, Any]:
    if op not in _ADVANCED_LINEAGE_OPS:
        return {"success": False, "error": f"unknown lineage op '{op}' (read-only: list|show|diff|verdicts)"}
    db = str(params.get("db") or "").strip()
    if not db:
        return {"success": False, "error": "db (path to the lineage SQLite sidecar) is required"}
    db = os.path.abspath(os.path.expanduser(db))
    # Never create a store from a browse UI — the sidecar must already exist.
    if not os.path.isfile(db):
        return {"success": False, "error": f"lineage db not found: {db}"}
    args: Dict[str, Any] = {"lineageDb": db}
    for src, dst in (("reel", "reel"), ("snapshot", "snapshotId"),
                     ("a", "aId"), ("b", "bId"), ("ref", "referenceRef")):
        value = str(params.get(src) or "").strip()
        if value:
            args[dst] = value
    payload = _run_advanced_bridge("lineage", op, args)
    if op == "verdicts" and payload.get("success"):
        verdicts = ((payload.get("result") or {}).get("verdicts")) or []
        tallies: Dict[str, int] = {}
        for v in verdicts:
            key = str(v.get("verdict") or "?")
            tallies[key] = tallies.get(key, 0) + 1
        payload["tallies"] = tallies
    return payload


def _setup_defaults(action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from src.server import setup as server_setup

    return server_setup(action, params or {})


def _inventory_prefs() -> Tuple[int, Optional[set]]:
    """Return (limit, exclude_bins) for the inventory walk from media-analysis
    preferences. ``exclude_bins`` is None when nothing is configured, so the walk
    indexes every folder by default."""
    try:
        from src.server import _media_analysis_effective_preferences

        prefs = _media_analysis_effective_preferences()
    except Exception:  # noqa: BLE001 — fall back to built-in defaults
        prefs = {}
    try:
        limit = max(1, min(int(prefs.get("inventory_limit", 500)), 10000))
    except (TypeError, ValueError):
        limit = 500
    raw = prefs.get("inventory_exclude_bins")
    exclude = {part.strip() for part in str(raw).split(",") if part.strip()} if raw else None
    return limit, (exclude or None)


