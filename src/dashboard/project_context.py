"""Resolve project-folder discovery and dashboard project-context bookkeeping."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from src.domains.media_analysis.utils.media_analysis_jobs import project_root_for_dashboard
from src.domains.media_analysis.utils.clip_identity_registry import resolve_output_root
from src.dashboard.resolve_helpers import _connect_resolve_read_only, _safe_call, _safe_id, _safe_name, _serialize_resolve


_PROJECT_CONTEXT_RE = re.compile(r"^(?P<slug>.+)-(?P<hash>[0-9a-f]{10})$")


def _project_context_family_slug(project_directory: Any) -> str:
    name = os.path.basename(str(project_directory or "")).strip()
    match = _PROJECT_CONTEXT_RE.match(name)
    return match.group("slug") if match else name


def _project_context_label(project_directory: Any) -> str:
    family = _project_context_family_slug(project_directory)
    return family.replace("-", " ").strip().title() or "Project"


def _context_payload(project_name: Any, project_id: Any, output_root: Dict[str, Any], *, source: str = "dashboard") -> Dict[str, Any]:
    project_directory = output_root.get("project_directory") or os.path.basename(str(output_root.get("project_root") or ""))
    return {
        "project_name": str(project_name or _project_context_label(project_directory)),
        "project_id": str(project_id) if project_id not in (None, "") else None,
        "project_root": output_root.get("project_root"),
        "base_root": output_root.get("base_root"),
        "project_directory": project_directory,
        "family_slug": _project_context_family_slug(project_directory),
        "source": source,
    }


def _context_from_project_root(base_root: str, project_root: str, *, source: str = "analysis_root") -> Optional[Dict[str, Any]]:
    root = os.path.realpath(os.path.abspath(os.path.expanduser(str(project_root))))
    base = os.path.realpath(os.path.abspath(os.path.expanduser(str(base_root))))
    try:
        if os.path.commonpath([root, base]) != base:
            return None
    except ValueError:
        return None
    if not os.path.isdir(root):
        return None
    project_directory = os.path.basename(root)
    return {
        "project_name": _project_context_label(project_directory),
        "project_id": None,
        "project_root": root,
        "base_root": base,
        "project_directory": project_directory,
        "family_slug": _project_context_family_slug(project_directory),
        "source": source,
    }


@_serialize_resolve
def _current_resolve_project_context(base_root: str) -> Optional[Dict[str, Any]]:
    resolve, resolve_error = _connect_resolve_read_only()
    if resolve_error:
        return None
    pm, pm_error = _safe_call(resolve, "GetProjectManager")
    if not pm or pm_error:
        return None
    project, _ = _safe_call(pm, "GetCurrentProject")
    if not project:
        return None
    project_name = _safe_name(project, "Resolve Project")
    project_id = _safe_id(project)
    root = project_root_for_dashboard(
        project_name=project_name,
        project_id=project_id,
        analysis_root=base_root,
    )
    if not root.get("success"):
        return None
    return _context_payload(project_name, project_id, root, source="resolve")


def _project_folder_name(folder: Any) -> str:
    if isinstance(folder, str):
        return folder
    name, _ = _safe_call(folder, "GetName")
    return str(name or folder or "").strip()


def _normalize_project_folder_path(folder_path: Any) -> List[str]:
    if folder_path is None:
        return []
    if isinstance(folder_path, (list, tuple)):
        raw_parts = [str(part or "").strip() for part in folder_path]
    else:
        raw_parts = re.split(r"[\\/]+", str(folder_path or ""))
    parts = [part for part in raw_parts if part and part not in {".", "/"}]
    if parts and parts[0].lower() in {"root", "master"}:
        parts = parts[1:]
    return parts


def _goto_project_folder(pm: Any, folder_path: Any) -> Tuple[bool, Optional[str]]:
    parts = _normalize_project_folder_path(folder_path)
    _, root_error = _safe_call(pm, "GotoRootFolder")
    if root_error:
        return False, root_error
    for part in parts:
        opened, open_error = _safe_call(pm, "OpenFolder", part)
        if open_error:
            return False, open_error
        if not opened:
            return False, f"Resolve project folder not found: {part}"
    return True, None


def _project_folder_label(folder_path: List[str]) -> str:
    return " / ".join(folder_path) if folder_path else "Root"


@_serialize_resolve
def _resolve_all_project_contexts(base_root: str, *, max_depth: int = 12, max_projects: int = 2000) -> Dict[str, Any]:
    resolve, resolve_error = _connect_resolve_read_only()
    if resolve_error:
        return {
            "success": True,
            "available": False,
            "error": resolve_error,
            "projects": [],
            "database": None,
            "current_folder": None,
        }
    pm, pm_error = _safe_call(resolve, "GetProjectManager")
    if not pm or pm_error:
        return {
            "success": True,
            "available": False,
            "error": pm_error or "ProjectManager unavailable",
            "projects": [],
            "database": None,
            "current_folder": None,
        }

    current_project, _ = _safe_call(pm, "GetCurrentProject")
    current_name = _safe_name(current_project, "") if current_project else None
    current_id = _safe_id(current_project) if current_project else None
    current_folder, _ = _safe_call(pm, "GetCurrentFolder")
    database, _ = _safe_call(pm, "GetCurrentDatabase")
    projects: List[Dict[str, Any]] = []
    errors: List[str] = []
    active_folder_path: Optional[List[str]] = None
    seen_locations = set()

    ok, root_error = _goto_project_folder(pm, [])
    if not ok:
        return {
            "success": True,
            "available": False,
            "error": root_error or "Failed to open Resolve project root folder",
            "projects": [],
            "database": database if isinstance(database, dict) else None,
            "current_folder": current_folder,
        }

    def visit(folder_path: List[str], depth: int = 0) -> None:
        nonlocal active_folder_path
        if depth > max_depth or len(projects) >= max_projects:
            return
        names, names_error = _safe_call(pm, "GetProjectListInCurrentFolder")
        if names_error:
            errors.append(f"{_project_folder_label(folder_path)}: {names_error}")
            names = []
        for raw_name in names or []:
            if len(projects) >= max_projects:
                break
            project_name = str(raw_name or "").strip()
            if not project_name:
                continue
            location_key = (tuple(folder_path), project_name)
            if location_key in seen_locations:
                continue
            seen_locations.add(location_key)
            project_id = current_id if current_name and project_name == current_name else None
            root = resolve_output_root(
                project_name=project_name,
                project_id=project_id,
                analysis_root=base_root,
                create=False,
            )
            project_directory = root.get("project_directory") if root.get("success") else ""
            is_active = bool(current_name and project_name == current_name)
            if is_active:
                active_folder_path = list(folder_path)
            projects.append({
                "project_name": project_name,
                "project_id": project_id,
                "project_directory": project_directory,
                "folder_path": list(folder_path),
                "folder_label": _project_folder_label(folder_path),
                "database_label": (database or {}).get("DbName") if isinstance(database, dict) else None,
                "active": is_active,
                "can_load_resolve": True,
            })

        folders, folders_error = _safe_call(pm, "GetFolderListInCurrentFolder")
        if folders_error:
            errors.append(f"{_project_folder_label(folder_path)} folders: {folders_error}")
            return
        for folder in folders or []:
            if len(projects) >= max_projects:
                break
            folder_name = _project_folder_name(folder)
            if not folder_name:
                continue
            opened, open_error = _safe_call(pm, "OpenFolder", folder_name)
            if open_error or not opened:
                errors.append(open_error or f"Failed to open folder {folder_name}")
                continue
            visit([*folder_path, folder_name], depth + 1)
            _, parent_error = _safe_call(pm, "GotoParentFolder")
            if parent_error:
                errors.append(f"Failed to return from {folder_name}: {parent_error}")
                _goto_project_folder(pm, folder_path)

    visit([])
    restore_path = active_folder_path if active_folder_path is not None else []
    _goto_project_folder(pm, restore_path)
    projects.sort(key=lambda row: (str(row.get("folder_label") or "").lower(), str(row.get("project_name") or "").lower()))
    return {
        "success": True,
        "available": True,
        "projects": projects,
        "count": len(projects),
        "database": database if isinstance(database, dict) else None,
        "current_folder": current_folder,
        "active_project": current_name,
        "active_folder_path": active_folder_path,
        "truncated": len(projects) >= max_projects,
        "errors": errors,
        "warning": "Project list truncated" if len(projects) >= max_projects else None,
    }


@_serialize_resolve
def _resolve_project_contexts(base_root: str) -> Dict[str, Any]:
    resolve, resolve_error = _connect_resolve_read_only()
    if resolve_error:
        return {
            "available": False,
            "error": resolve_error,
            "contexts": [],
            "current": None,
            "database": None,
            "folder": None,
        }
    pm, pm_error = _safe_call(resolve, "GetProjectManager")
    if not pm or pm_error:
        return {
            "available": False,
            "error": pm_error or "ProjectManager unavailable",
            "contexts": [],
            "current": None,
            "database": None,
            "folder": None,
        }
    current_project, _ = _safe_call(pm, "GetCurrentProject")
    current_name = _safe_name(current_project, "") if current_project else None
    current_id = _safe_id(current_project) if current_project else None
    names, names_error = _safe_call(pm, "GetProjectListInCurrentFolder")
    if names_error:
        names = []
    database, _ = _safe_call(pm, "GetCurrentDatabase")
    folder, _ = _safe_call(pm, "GetCurrentFolder")
    contexts: List[Dict[str, Any]] = []
    seen_names = set()
    for raw_name in names or []:
        project_name = str(raw_name or "").strip()
        if not project_name or project_name in seen_names:
            continue
        seen_names.add(project_name)
        project_id = current_id if current_name and project_name == current_name else None
        root = resolve_output_root(
            project_name=project_name,
            project_id=project_id,
            analysis_root=base_root,
            create=False,
        )
        if not root.get("success"):
            continue
        context = _context_payload(project_name, project_id, root, source="resolve")
        context["resolve_project_name"] = project_name
        context["can_load_resolve"] = True
        context["resolve_current"] = bool(current_name and project_name == current_name)
        contexts.append(context)
    if current_project and current_name and current_name not in seen_names:
        root = resolve_output_root(
            project_name=current_name,
            project_id=current_id,
            analysis_root=base_root,
            create=False,
        )
        if root.get("success"):
            context = _context_payload(current_name, current_id, root, source="resolve")
            context["resolve_project_name"] = current_name
            context["can_load_resolve"] = True
            context["resolve_current"] = True
            contexts.insert(0, context)
    return {
        "available": True,
        "error": names_error,
        "contexts": contexts,
        "current": next((context for context in contexts if context.get("resolve_current")), None),
        "database": database if isinstance(database, dict) else None,
        "folder": folder,
    }


@_serialize_resolve
def _load_resolve_project_context(base_root: str, project_name: Any, folder_path: Any = None) -> Dict[str, Any]:
    target_name = str(project_name or "").strip()
    if not target_name:
        return {"success": False, "error": "Resolve project name is required"}
    resolve, resolve_error = _connect_resolve_read_only()
    if resolve_error:
        return {"success": False, "error": resolve_error}
    pm, pm_error = _safe_call(resolve, "GetProjectManager")
    if not pm or pm_error:
        return {"success": False, "error": pm_error or "ProjectManager unavailable"}
    current_project, _ = _safe_call(pm, "GetCurrentProject")
    current_name = _safe_name(current_project, "") if current_project else None
    loaded_project = current_project
    target_folder = _normalize_project_folder_path(folder_path)
    if target_folder:
        ok, folder_error = _goto_project_folder(pm, target_folder)
        if not ok:
            return {"success": False, "error": folder_error or "Failed to open Resolve project folder"}
    if current_name != target_name or target_folder:
        loaded_project, load_error = _safe_call(pm, "LoadProject", target_name)
        if load_error:
            return {"success": False, "error": f"LoadProject failed: {load_error}"}
        if not loaded_project:
            folder_label = _project_folder_label(target_folder) if target_folder else "current project folder"
            return {"success": False, "error": f"Resolve project not found in {folder_label}: {target_name}"}
    project, _ = _safe_call(pm, "GetCurrentProject")
    project = project or loaded_project
    loaded_name = _safe_name(project, target_name)
    project_id = _safe_id(project)
    root = project_root_for_dashboard(
        project_name=loaded_name,
        project_id=project_id,
        analysis_root=base_root,
    )
    if not root.get("success"):
        return root
    context = _context_payload(loaded_name, project_id, root, source="resolve")
    context["resolve_project_name"] = loaded_name
    context["can_load_resolve"] = True
    context["resolve_current"] = True
    return {"success": True, "active": context, "output_root": root}


def discover_project_contexts(base_root: str, active: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = os.path.realpath(os.path.abspath(os.path.expanduser(str(base_root))))
    contexts: List[Dict[str, Any]] = []
    resolve_projects = _resolve_project_contexts(base)
    current = resolve_projects.get("current")
    contexts.extend(resolve_projects.get("contexts") or [])
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            context = _context_from_project_root(base, path)
            if context:
                context["can_load_resolve"] = False
                contexts.append(context)
    if active:
        contexts.append(dict(active, source=active.get("source") or "active"))

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for context in contexts:
        root = context.get("project_root")
        if not root or root in seen:
            continue
        seen.add(root)
        context["active"] = bool(active and root == active.get("project_root"))
        deduped.append(context)

    active_family = (active or {}).get("family_slug")
    related_roots = [
        context["project_root"]
        for context in deduped
        if active_family and context.get("family_slug") == active_family
        and (context["project_root"] == (active or {}).get("project_root") or os.path.isdir(context["project_root"]))
    ]
    return {
        "success": True,
        "base_root": base,
        "active": active,
        "current_resolve_project": current,
        "resolve_projects": resolve_projects,
        "contexts": deduped,
        "related_project_roots": related_roots,
    }


# ─── V2 Review API: clip / shot read endpoints + frame serving ──────────────
#
# These helpers power the bin grid, clip detail, and shot detail views.
# All read directly from disk artifacts (analysis.json + corrections.json
# sidecar + sampled_NNNN.jpg). When C1 (DB-as-truth) lands, swap these to
# query the V2 DB — the HTTP API contract does not change.


