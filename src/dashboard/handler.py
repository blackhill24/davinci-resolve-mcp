"""HTTP request handler for the local analysis dashboard."""

from __future__ import annotations

import hashlib
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse

from src.core import brain_edits as _brain_edits
from src.core import timeline_versioning as _timeline_versioning
from src.domains.media_analysis.utils.analysis_memory import read_panel_state, write_panel_state
from src.domains.media_analysis.utils.analysis_index_query import analysis_index_status, query_analysis_index
from src.domains.media_analysis.utils.reports import analysis_root_coverage
from src.domains.media_analysis.utils.analysis_index_build import build_analysis_index
from src.domains.media_analysis.utils.capabilities_and_planning import detect_capabilities
from src.domains.media_analysis.utils.media_analysis_jobs import (
    batch_job_status,
    cancel_batch_job,
    create_batch_job_from_paths,
    list_batch_jobs,
    resume_batch_job,
    run_batch_job_slice,
)
from src.dashboard.resolve_helpers import _resolve_identity, _run_resolve_ai_op
from src.dashboard.project_context import _resolve_all_project_contexts
from src.dashboard.clip_review import apply_clip_correction, combined_clip_analysis, export_clip_selection, get_analyzed_clip, get_analyzed_clip_shot, get_analyzed_clip_shots, get_analyzed_clip_transcript, get_clip_frame_path, list_analyzed_clips, regenerate_clip_transcript, save_clip_transcript_corrections, _v2_semantic_search
from src.dashboard.timeline_versions import _v2_create_timeline_from_clips, _v2_enrich_search_results, _v2_open_clip_in_resolve, get_edit_plan_payload, get_timeline_history_payload, list_edit_plans_payload, list_timelines_with_versions, proxy_timeline_versioning_action, read_clip_corrections
from src.dashboard.media_inventory import resolve_media_inventory
from src.dashboard.state import DashboardState, _advanced_capabilities_payload, _advanced_lineage_payload, _clear_restart_marker, _dashboard_doc, _inventory_prefs, _launch_claude_code_terminal, _mcp_install_payload, _mcp_status_payload, _mcp_uninstall_payload, _mcp_version, _native_directory_picker, _read_restart_marker, _repo_root, _request_is_loopback, _request_origin_ok, _setup_defaults, _transport_start, _transport_stop, _update_apply_payload, _update_history_payload, _update_preview_payload, _update_rollback_payload, _update_status_payload


_PANEL_HTML_PATH = os.path.join(os.path.dirname(__file__), "static", "panel.html")
with open(_PANEL_HTML_PATH, "r", encoding="utf-8") as _panel_html_fh:
    HTML = _panel_html_fh.read()


class Handler(BaseHTTPRequestHandler):
    state: DashboardState

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json_etag(self, payload: Dict[str, Any]) -> None:
        """JSON response with an ETag so unchanged polls short-circuit to 304.

        The Resolve media inventory is re-fetched every few seconds; when the
        serialized payload is byte-identical to what the client already holds we
        skip both the body transfer and the client-side re-render of the table.
        """
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        etag = '"' + hashlib.md5(raw).hexdigest() + '"'
        if self.headers.get("If-None-Match") == etag:
            tiny = json.dumps({"success": True, "unchanged": True, "etag": etag}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(tiny)))
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(tiny)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(raw)

    def _serve_file(self, path: str, content_type: str = "application/octet-stream") -> None:
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            self._json({"success": False, "error": "File not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        self.wfile.write(raw)

    def _html(self) -> None:
        raw = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_clip_export(self, body: Dict[str, Any]) -> None:
        clip_ids = body.get("clip_ids")
        fmt = body.get("format") or "json"
        if not isinstance(clip_ids, list) or not clip_ids:
            self._json({"success": False, "error": "clip_ids must be a non-empty list"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            raw, content_type, filename = export_clip_selection(self.state.project_root, list(clip_ids), str(fmt))
        except Exception as exc:  # noqa: BLE001
            self._json({"success": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_GET(self) -> None:
        if not _request_origin_ok(self):
            self._json({"success": False, "error": "forbidden: non-localhost Host/Origin"}, HTTPStatus.FORBIDDEN)
            return
        try:
            self._route_get()
        except Exception as exc:  # pragma: no cover - runtime safety for dashboard users
            self._json({"success": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        if not _request_origin_ok(self):
            self._json({"success": False, "error": "forbidden: non-localhost Host/Origin"}, HTTPStatus.FORBIDDEN)
            return
        try:
            self._route_post()
        except Exception as exc:  # pragma: no cover
            self._json({"success": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/":
            self._html()
            return
        if path == "/api/boot":
            self._json(
                {
                    "success": True,
                    "project_name": self.state.project_name,
                    "project_id": self.state.project_id,
                    "project_root": self.state.project_root,
                    "repo_root": _repo_root(),
                    "codex_workspace": _repo_root(),
                    "output_root": self.state.output_root,
                    "active_context": self.state.context(),
                    "related_project_roots": self.state.related_project_roots(),
                    "capabilities": detect_capabilities(),
                    "resolve": _resolve_identity(),
                    "mcp_version": _mcp_version(),
                }
            )
            return
        if path == "/api/projects":
            self._json(self.state.projects())
            return
        if path == "/api/update/status":
            force = (query.get("force") or ["0"])[0].lower() in {"1", "true", "yes"}
            self._json(_update_status_payload(self.state.project_root, force=force))
            return
        if path == "/api/update/history":
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            self._json(_update_history_payload(limit=limit))
            return
        if path == "/api/restart_needed":
            self._json(_read_restart_marker(_repo_root()))
            return
        if path == "/api/update/preview":
            self._json(_update_preview_payload())
            return
        if path == "/api/advanced/capabilities":
            self._json(_advanced_capabilities_payload())
            return
        if path == "/api/advanced/lineage":
            op = (query.get("op") or [""])[0]
            params = {k: (query.get(k) or [""])[0] for k in ("db", "reel", "snapshot", "a", "b", "ref")}
            self._json(_advanced_lineage_payload(op, params))
            return
        if path == "/api/mcp/status":
            self._json(_mcp_status_payload())
            return
        if path == "/api/projects/all":
            self._json(_resolve_all_project_contexts(self.state.base_analysis_root))
            return
        if path == "/api/jobs":
            self._json(list_batch_jobs(self.state.project_root))
            return
        if path == "/api/docs":
            doc = (query.get("doc") or ["readme"])[0]
            payload = _dashboard_doc(doc)
            self._json(payload, 200 if payload.get("success") else 404)
            return
        if path.startswith("/api/doc_asset/"):
            rel = unquote(path[len("/api/doc_asset/"):])
            base = os.path.realpath(os.path.join(_repo_root(), "docs", "images"))
            full = os.path.realpath(os.path.join(base, rel))
            if not (full.startswith(base + os.sep) or full == base):
                self._json({"success": False, "error": "path escape"}, HTTPStatus.FORBIDDEN)
                return
            content_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                             ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp"}
            ext = os.path.splitext(full)[1].lower()
            if ext not in content_types or not os.path.isfile(full):
                self._json({"success": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._serve_file(full, content_types[ext])
            return
        if path == "/api/setup/schema":
            self._json(_setup_defaults("schema"))
            return
        if path == "/api/setup/defaults":
            self._json(_setup_defaults("get_defaults"))
            return
        if path == "/api/resolve/media":
            pref_limit, exclude_bins = _inventory_prefs()
            self._json_etag(
                resolve_media_inventory(
                    self.state.project_root,
                    limit=(query.get("limit") or [pref_limit])[0],
                    exclude_bins=exclude_bins,
                    recursive=(query.get("recursive") or ["true"])[0].lower() not in {"0", "false", "no"},
                    probe_paths=(query.get("probe") or ["1"])[0].lower() not in {"0", "false", "no"},
                    reuse_cached=(query.get("reuse") or ["0"])[0].lower() in {"1", "true", "yes"},
                )
            )
            return
        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[3]
            self._json(batch_job_status(self.state.project_root, job_id))
            return
        if path == "/api/index/status":
            self._json(analysis_index_status(self.state.project_root))
            return
        if path == "/api/coverage":
            # Standalone readiness rollup — no live Resolve required. The
            # `coverage_report` action gives target-vs-records detail; this
            # endpoint summarizes what the analysis directory already knows.
            self._json(analysis_root_coverage(self.state.project_root))
            return
        if path == "/api/index/query":
            q = (query.get("q") or [""])[0]
            payload = query_analysis_index(self.state.project_root, q, limit=(query.get("limit") or [20])[0])
            if payload.get("success") and payload.get("results"):
                payload["results"] = _v2_enrich_search_results(self.state.project_root, payload["results"])
            self._json(payload)
            return
        if path == "/api/entities":
            try:
                from src.domains.media_analysis.utils import entities as _entities

                self._json(_entities.list_entities(self.state.project_root))
            except Exception as exc:  # noqa: BLE001 — panel reads fail soft
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/search/semantic":
            q = (query.get("q") or [""])[0]
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            self._json(_v2_semantic_search(self.state.project_root, q, limit=limit))
            return
        # ─── C6 timeline-history surface ───────────────────────────────
        if path == "/api/timeline_versions":
            self._json(list_timelines_with_versions(self.state.project_root))
            return
        if path == "/api/timeline_versions/diff":
            timeline_name = (query.get("timeline_name") or [""])[0]
            try:
                from_version = int((query.get("from_version") or [""])[0])
                to_version = int((query.get("to_version") or [""])[0])
            except (ValueError, TypeError):
                self._json({"success": False, "error": "from_version and to_version (ints) required"},
                           HTTPStatus.BAD_REQUEST)
                return
            if not timeline_name:
                self._json({"success": False, "error": "timeline_name required"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"success": True, **_timeline_versioning.diff_versions(
                project_root=self.state.project_root,
                timeline_name=timeline_name,
                from_version=from_version,
                to_version=to_version,
            )})
            return
        if path.startswith("/api/timeline_versions/"):
            timeline_name = unquote(path[len("/api/timeline_versions/"):])
            if not timeline_name:
                self._json({"success": False, "error": "timeline_name required"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(get_timeline_history_payload(self.state.project_root, timeline_name))
            return
        # ─── Edit-engine plan browser (DB/file only — no Resolve) ───────
        if path == "/api/edit_plans":
            self._json(list_edit_plans_payload(self.state.project_root))
            return
        if path.startswith("/api/edit_plans/"):
            plan_id = unquote(path[len("/api/edit_plans/"):])
            if not plan_id:
                self._json({"success": False, "error": "plan_id required"}, HTTPStatus.BAD_REQUEST)
                return
            payload = get_edit_plan_payload(self.state.project_root, plan_id)
            if not payload.get("success") and "not found" in str(payload.get("error", "")):
                self._json(payload, HTTPStatus.NOT_FOUND)
                return
            self._json(payload)
            return
        if path == "/api/brain_edits/registry":
            self._json({"success": True, **_brain_edits.read_brain_edits_registry(self.state.project_root)})
            return
        if path == "/api/caps/history":
            try:
                from src.domains.media_analysis.utils import analysis_caps as _ac
                days = int((query.get("days") or ["30"])[0])
                self._json({
                    "success": True,
                    "history": _ac.get_usage_history(project_root=self.state.project_root, days=days),
                })
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/caps/refusals":
            try:
                from src.domains.media_analysis.utils import analysis_caps as _ac
                limit = int((query.get("limit") or ["20"])[0])
                self._json({
                    "success": True,
                    "events": _ac.get_caps_events(
                        project_root=self.state.project_root,
                        event_type=(query.get("event_type") or ["refusal"])[0] or "refusal",
                        limit=limit,
                    ),
                })
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/media_pool_changes":
            try:
                from src.domains.media_pool_ingest.utils import media_pool_changes as _mpc
                limit = int((query.get("limit") or ["50"])[0])
                self._json({
                    "success": True,
                    "changes": _mpc.get_media_pool_change_history(
                        project_root=self.state.project_root, limit=limit,
                    ),
                })
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/runs":
            try:
                from src.core import analysis_runs as _ar
                limit = int((query.get("limit") or ["50"])[0])
                self._json({
                    "success": True,
                    "runs": _ar.list_runs(project_root=self.state.project_root, limit=limit),
                    "current_run_id": _ar.current_run_id(),
                })
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/caps":
            # Effective caps + per-project usage rollup. Proxies into the
            # media_analysis tool's get_caps action which already does the
            # preference lookup + DB rollup.
            try:
                from src.server import media_analysis as _ma_tool
                import asyncio
                result = asyncio.run(_ma_tool("get_caps", params={}))
                self._json(result)
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/resolve_ai_usage":
            # Ledger of Resolve-local 21.0 AI ops (read straight from this
            # project's brain DB — no Resolve round-trip needed).
            try:
                from src.core import resolve_ai_ledger as _ledger
                root = self.state.project_root
                self._json({
                    "success": True,
                    "summary": _ledger.get_summary(project_root=root),
                    "recent": _ledger.get_usage(project_root=root, limit=50),
                })
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/resolve_ai/governance":
            # Effective governance tier + this project's render usage. Proxies
            # into the media_analysis get_ai_governance action.
            try:
                from src.server import media_analysis as _ma_tool
                import asyncio
                self._json(asyncio.run(_ma_tool("get_ai_governance", params={})))
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path.startswith("/api/timeline_thumbnail/"):
            rel = unquote(path[len("/api/timeline_thumbnail/"):])
            # Path is <slug>/<vNN.png>; constrain it to live under _soul/timeline_versions
            base = os.path.join(self.state.project_root, "_soul", "timeline_versions")
            full = os.path.realpath(os.path.join(base, rel))
            if not full.startswith(os.path.realpath(base) + os.sep) and full != os.path.realpath(base):
                self._json({"success": False, "error": "path escape"}, HTTPStatus.FORBIDDEN)
                return
            if not os.path.isfile(full):
                self._json({"success": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError as exc:
                self._json({"success": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        # ─── V2 Review API ──────────────────────────────────────────────
        if path == "/api/clips":
            self._json(list_analyzed_clips(self.state.project_root))
            return
        if path == "/api/panel_state":
            state_payload = read_panel_state(self.state.project_root) or {}
            self._json({"success": True, "state": state_payload})
            return
        if path.startswith("/api/clips/"):
            parts = path.split("/")
            # /api/clips/<clip_id>  → parts = ["", "api", "clips", "<id>"]
            if len(parts) >= 4:
                clip_id = parts[3]
                tail = parts[4:]
                if not tail:
                    self._json(get_analyzed_clip(self.state.project_root, clip_id))
                    return
                if tail == ["shots"]:
                    self._json(get_analyzed_clip_shots(self.state.project_root, clip_id))
                    return
                if tail == ["transcript"]:
                    self._json(get_analyzed_clip_transcript(self.state.project_root, clip_id))
                    return
                if len(tail) == 2 and tail[0] == "shots":
                    try:
                        shot_index = int(tail[1])
                    except ValueError:
                        self._json({"success": False, "error": "shot_index must be an integer"}, HTTPStatus.BAD_REQUEST)
                        return
                    self._json(get_analyzed_clip_shot(self.state.project_root, clip_id, shot_index))
                    return
                if len(tail) == 2 and tail[0] == "frames":
                    try:
                        frame_index = int(tail[1])
                    except ValueError:
                        self._json({"success": False, "error": "frame_index must be an integer"}, HTTPStatus.BAD_REQUEST)
                        return
                    frame_path = get_clip_frame_path(self.state.project_root, clip_id, frame_index)
                    if not frame_path:
                        self._json({"success": False, "error": f"Frame {frame_index} not found for clip {clip_id}"}, HTTPStatus.NOT_FOUND)
                        return
                    self._serve_file(frame_path, content_type="image/jpeg")
                    return
                if tail == ["corrections"]:
                    self._json(read_clip_corrections(self.state.project_root, clip_id))
                    return
        self._json({"success": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._body()
        if path == "/api/browse/directory":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Native folder picker is only available to loopback clients."}, HTTPStatus.FORBIDDEN)
                return
            initial = body.get("initial") or body.get("current") or None
            self._json(_native_directory_picker(initial=str(initial) if initial else None))
            return
        if path == "/api/launch/claude-code":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Terminal launch is loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            self._json(_launch_claude_code_terminal())
            return
        if path == "/api/update/apply":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Self-update is only available to loopback clients."}, HTTPStatus.FORBIDDEN)
                return
            strategy = (body.get("strategy") or "refuse_on_dirty").strip().lower()
            if strategy not in {"refuse_on_dirty", "stash_if_needed"}:
                strategy = "refuse_on_dirty"
            force_active_jobs = bool(body.get("force_active_jobs") or body.get("force"))
            self._json(_update_apply_payload(
                strategy=strategy,
                force_active_jobs=force_active_jobs,
                project_root=self.state.project_root,
            ))
            return
        if path == "/api/restart_needed/clear":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Loopback only."}, HTTPStatus.FORBIDDEN)
                return
            self._json(_clear_restart_marker(_repo_root()))
            return
        if path == "/api/update/rollback":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Rollback is only available to loopback clients."}, HTTPStatus.FORBIDDEN)
                return
            self._json(_update_rollback_payload())
            return
        if path.startswith("/api/clips/") and path.endswith("/transcript/regenerate"):
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Transcript regeneration is loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            clip_id = path.split("/")[3]
            # Serialize the analysis.json read-modify-write: this server is
            # threaded, so concurrent regenerations (or a regen racing a batch
            # job's report write) would interleave and the last writer would drop
            # the other's updates (PS9).
            with self.state.lock:
                result = regenerate_clip_transcript(
                    self.state.project_root,
                    clip_id,
                    with_words=bool(body.get("with_words", True)),
                    backend=body.get("backend") or None,
                    language=body.get("language") or None,
                    model=body.get("model") or None,
                )
            self._json(result)
            return
        if path.startswith("/api/clips/") and path.endswith("/transcript/corrections"):
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Transcript edits are loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            clip_id = path.split("/")[3]
            self._json(save_clip_transcript_corrections(self.state.project_root, clip_id, body))
            return
        if path == "/api/mcp/install":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "MCP install routes are loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            client_id = str(body.get("client_id") or "").strip()
            if not client_id:
                self._json({"success": False, "error": "client_id is required"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(_mcp_install_payload(client_id))
            return
        if path == "/api/mcp/uninstall":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "MCP install routes are loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            client_id = str(body.get("client_id") or "").strip()
            if not client_id:
                self._json({"success": False, "error": "client_id is required"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(_mcp_uninstall_payload(client_id))
            return
        if path == "/api/mcp/transport/start":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Transport management is loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            self._json(_transport_start())
            return
        if path == "/api/mcp/transport/stop":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Transport management is loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            self._json(_transport_stop())
            return
        if path == "/api/jobs":
            paths = body.get("paths") or []
            if isinstance(paths, str):
                paths = [line.strip() for line in paths.splitlines() if line.strip()]
            params = {
                "depth": body.get("depth") or "standard",
                "max_analysis_frames": body.get("max_analysis_frames", 8),
                "vision": body.get("vision") or {"enabled": False},
                "transcription": body.get("transcription") or {"enabled": True, "allow_model_download": True},
                "cleanup_frames": True,
                "reuse_project_roots": self.state.related_project_roots(),
            }
            # Honor the saved frame-sampling mode (or an explicit per-job override)
            # so batch runs match the user's chosen coverage/cost. Falls back to the
            # recommended mode when the user hasn't set a default yet (batch jobs
            # shouldn't block on the first-run prompt).
            try:
                from src.server import (
                    _media_analysis_effective_preferences as _ma_eff_prefs,
                )
                from src.domains.media_analysis.utils import caps_gating as _ma_mod
                _ma_prefs = _ma_eff_prefs()
                params["sampling_mode"] = (
                    body.get("sampling_mode")
                    or _ma_prefs.get("sampling_mode_default")
                    or _ma_mod.RECOMMENDED_SAMPLING_MODE
                )
                params["frames_per_minute"] = body.get("frames_per_minute") or _ma_prefs.get("sampling_frames_per_minute")
                params["frame_floor"] = body.get("frame_floor") or _ma_prefs.get("sampling_frame_floor")
                params["frame_ceiling"] = body.get("frame_ceiling") or _ma_prefs.get("sampling_frame_ceiling")
            except Exception:
                # Best-effort; the engine still applies its own defaults.
                pass
            with self.state.lock:
                created = create_batch_job_from_paths(
                    project_name=self.state.project_name,
                    project_id=self.state.project_id,
                    paths=paths,
                    analysis_root=self.state.output_root["base_root"],
                    recursive=bool(body.get("recursive", True)),
                    params=params,
                    name=body.get("name"),
                )
            self._json(created, 200 if created.get("success") else 400)
            return
        if path.startswith("/api/jobs/") and path.endswith("/run"):
            job_id = path.split("/")[3]
            with self.state.lock:
                result = run_batch_job_slice(
                    self.state.project_root,
                    job_id,
                    max_clips=body.get("max_clips", 1),
                    max_seconds=body.get("max_seconds"),
                )
            self._json(result, 200 if result.get("success") else 400)
            return
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path.split("/")[3]
            self._json(cancel_batch_job(self.state.project_root, job_id))
            return
        if path.startswith("/api/jobs/") and path.endswith("/resume"):
            job_id = path.split("/")[3]
            self._json(resume_batch_job(self.state.project_root, job_id))
            return
        if path == "/api/index/build":
            with self.state.lock:
                self._json(build_analysis_index(self.state.project_root))
            return
        if path == "/api/setup/defaults":
            payload = _setup_defaults("set_defaults", body)
            self._json(payload, 200 if payload.get("success") else 400)
            return
        if path == "/api/setup/clear":
            payload = _setup_defaults("clear_defaults", body)
            self._json(payload, 200 if payload.get("success") else 400)
            return
        if path == "/api/context":
            with self.state.lock:
                payload = self.state.set_context(body)
            self._json(payload, 200 if payload.get("success") else 400)
            return
        # ─── C6 timeline-history write actions (loopback only) ─────────
        if path == "/api/timeline_versions/action":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Timeline versioning writes are loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            self._json(proxy_timeline_versioning_action(body))
            return
        if path == "/api/caps":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Caps writes are loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            try:
                from src.server import media_analysis as _ma_tool
                import asyncio
                result = asyncio.run(_ma_tool("set_caps_preset", params=body))
                self._json(result, 200 if result.get("success") else 400)
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/resolve_ai/run":
            # Run a Resolve 21 AI op from the panel. Loopback-only because it
            # mutates Resolve (and the media-creators write new files). The
            # confirm-token two-step is handled by the consolidated tool; the
            # 'confirmation_required' shape is relayed to the panel as 200.
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Loopback only."}, HTTPStatus.FORBIDDEN)
                return
            try:
                result = _run_resolve_ai_op(body)
                ok = bool(result.get("success")) or result.get("status") == "confirmation_required"
                self._json(result, 200 if ok else 400)
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/resolve_ai/governance":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Loopback only."}, HTTPStatus.FORBIDDEN)
                return
            try:
                from src.server import media_analysis as _ma_tool
                import asyncio
                result = asyncio.run(_ma_tool("set_ai_governance", params=body))
                self._json(result, 200 if result.get("success") else 400)
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/caps/reset_day":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Loopback only."}, HTTPStatus.FORBIDDEN)
                return
            try:
                from src.domains.media_analysis.utils import analysis_caps as _ac
                result = _ac.reset_day_usage(
                    project_root=self.state.project_root,
                    day_bucket=body.get("day_bucket"),
                )
                self._json(result)
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/runs/begin":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Loopback only."}, HTTPStatus.FORBIDDEN)
                return
            try:
                from src.core import analysis_runs as _ar
                result = _ar.begin_run(
                    project_root=self.state.project_root,
                    label=body.get("label"),
                    initiator=body.get("initiator") or "dashboard",
                )
                self._json(result)
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/runs/end":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Loopback only."}, HTTPStatus.FORBIDDEN)
                return
            try:
                from src.core import analysis_runs as _ar
                result = _ar.end_run(
                    project_root=self.state.project_root,
                    analysis_run_id=body.get("analysis_run_id"),
                )
                self._json(result)
            except Exception as exc:
                self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        if path == "/api/update/channel":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Loopback only."}, HTTPStatus.FORBIDDEN)
                return
            channel = (body.get("channel") or "stable").strip().lower()
            if channel not in {"stable", "beta", "dev"}:
                self._json({"success": False, "error": "channel must be stable | beta | dev"}, HTTPStatus.BAD_REQUEST)
                return
            os.environ["DAVINCI_RESOLVE_MCP_UPDATE_CHANNEL"] = channel
            self._json({"success": True, "channel": channel,
                        "note": "Set for this process; persist via env var to survive restart."})
            return
        # ─── V2 Review API (writes) ─────────────────────────────────────
        if path == "/api/panel_state":
            merge = body.pop("__merge__", True)
            written_by = body.pop("__written_by__", "control_panel")
            result = write_panel_state(
                self.state.project_root,
                {k: v for k, v in body.items() if not k.startswith("__")},
                written_by=written_by,
                merge=bool(merge),
            )
            self._json(result, 200 if result.get("success") else 400)
            return
        if path == "/api/resolve/open_clip":
            result = _v2_open_clip_in_resolve(body)
            self._json(result, 200 if result.get("success") else 400)
            return
        if path == "/api/resolve/create_timeline_from_clips":
            if not _request_is_loopback(self):
                self._json({"success": False, "error": "Timeline creation is loopback-only."}, HTTPStatus.FORBIDDEN)
                return
            result = _v2_create_timeline_from_clips(body)
            self._json(result, 200 if result.get("success") else 400)
            return
        if path == "/api/clips/combined":
            result = combined_clip_analysis(self.state.project_root, body)
            self._json(result, 200 if result.get("success") else 400)
            return
        if path == "/api/clips/export":
            return self._serve_clip_export(body)
        if path.startswith("/api/clips/"):
            parts = path.split("/")
            if len(parts) >= 5 and parts[4] == "corrections":
                clip_id = parts[3]
                result = apply_clip_correction(self.state.project_root, clip_id, body)
                self._json(result, 200 if result.get("success") else 400)
                return
        self._json({"success": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)


