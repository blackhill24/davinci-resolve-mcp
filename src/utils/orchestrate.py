"""orchestrate — resumable ingest-to-deliver job conductor (state + persistence).

A job sequences the existing domain tools (media_pool, media_analysis,
auto_edit, timeline, timeline_item_color, render, ...) across ten stages:

    intake ingest analysis edit conform grade audio [fusion] deliver review

This module owns only the state machine and its durable storage — the pure,
Resolve-free half (mirrors ``edit_engine``/``auto_edit``: no Resolve imports
here; stage execution and gate ceremonies live in server.py). Delegation
(``run_stage``) lands in a later phase; this phase gives resumability its
foundation: a job record that survives context death and a lease that makes
resuming safe.

Two-file persistence (record = truth, index = rebuildable cache):

    {project_root}/memory/jobs/{job_id}.json   — full record, content-fingerprinted
    {analysis_base_root}/_jobs/index.json      — thin per-job stubs, lets
                                                  ``list_jobs`` discover jobs
                                                  with Resolve closed

``project_root`` here means the analysis root, same overloaded convention
``edit_engine``/``auto_edit`` use — it need not be a live Resolve project.
The record is written (and fsynced via ``os.replace``) before the index is
touched; the index update is best-effort and always rebuildable by scanning
every project root under ``analysis_base_root`` for ``memory/jobs/*.json``.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from src.utils import analysis_memory

JOB_KIND = "orchestrate_job"
JOBS_DIR_NAME = "jobs"
GLOBAL_INDEX_DIR_NAME = "_jobs"
GLOBAL_INDEX_FILENAME = "index.json"

# A lease older than this (no heartbeat) is considered abandoned — resuming
# from a fresh session is stealing an expired lease, not a conflict.
LEASE_TTL_SECONDS = 900

ALL_STAGES = (
    "intake", "ingest", "analysis", "edit", "conform",
    "grade", "audio", "fusion", "deliver", "review",
)
# fusion is opt-in (title/motion-graphics work is not every job); every other
# stage is on by default.
DEFAULT_STAGES = tuple(s for s in ALL_STAGES if s != "fusion")

STAGE_STATUSES = ("pending", "running", "done", "failed")
_STAGE_TRANSITIONS: Dict[str, set] = {
    "pending": {"running"},
    "running": {"done", "failed"},
    # A later phase's drift-refuse re-plan resets a stale "done" back to
    # pending rather than blind-continuing past it.
    "done": {"pending"},
    "failed": {"pending", "running"},  # clean retry
}

JOB_STATES = ("active", "finished", "aborted")

DEFAULT_DELIVERABLE = "youtube_1080p"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_epoch() -> float:
    return time.time()


def _iso_to_epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


# ── stage manifest ───────────────────────────────────────────────────────────


def infer_stage_manifest(brief: Dict[str, Any]) -> List[str]:
    """Ordered stage list for a job brief. An explicit ``stages`` list wins."""
    explicit = brief.get("stages")
    if isinstance(explicit, (list, tuple)) and explicit:
        return [str(s) for s in explicit]
    manifest = list(DEFAULT_STAGES)
    if brief.get("include_fusion"):
        manifest.insert(manifest.index("deliver"), "fusion")
    return manifest


def validate_manifest(manifest: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(manifest, list) or not manifest:
        return ["manifest must be a non-empty list of stage names"]
    if manifest[0] != "intake":
        errors.append("manifest must start with 'intake'")
    unknown = [s for s in manifest if s not in ALL_STAGES]
    if unknown:
        errors.append(f"unknown stage(s): {unknown}")
    if len(set(manifest)) != len(manifest):
        errors.append("manifest must not repeat a stage")
    return errors


# ── job-brief validation ─────────────────────────────────────────────────────


def validate_job_inputs(
    *,
    files: Any,
    genre: str = "talking_head",
    deliverable: str = DEFAULT_DELIVERABLE,
    target_duration_seconds: Any = None,
    title_text: Any = None,
    stages: Any = None,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(files, (list, tuple)) or not files:
        errors.append("files must be a non-empty list of media paths")
    elif not all(isinstance(f, str) and f.strip() for f in files):
        errors.append("every entry in files must be a non-empty path string")
    if not isinstance(genre, str) or not genre.strip():
        errors.append("genre must be a non-empty string")
    if not isinstance(deliverable, str) or not deliverable.strip():
        errors.append("deliverable must be a non-empty string")
    if target_duration_seconds is not None:
        if not isinstance(target_duration_seconds, (int, float)) or target_duration_seconds <= 0:
            errors.append("target_duration_seconds must be a positive number")
    if title_text is not None and not isinstance(title_text, str):
        errors.append("title_text must be a string when given")
    if stages is not None:
        if isinstance(stages, (list, tuple)):
            errors.extend(validate_manifest(list(stages)))
        else:
            errors.append("stages must be a list of stage names when given")
    return errors


# ── record persistence ───────────────────────────────────────────────────────


def _jobs_dir(project_root: str) -> str:
    return os.path.join(analysis_memory.memory_dir(project_root), JOBS_DIR_NAME)


def _job_fingerprint(job: Dict[str, Any]) -> str:
    # Lease churn (heartbeat renewals) must not perturb the content fingerprint
    # — it's operational metadata, not job content that resume should protect.
    body = {k: v for k, v in job.items()
            if k not in ("fingerprint", "updated_at", "lease")}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _default_analysis_base_root(project_root: str) -> str:
    return os.path.dirname(os.path.normpath(project_root))


def save_job(project_root: str, job: Dict[str, Any], *, analysis_base_root: Optional[str] = None) -> Dict[str, Any]:
    analysis_memory.ensure_memory_structure(project_root)
    os.makedirs(_jobs_dir(project_root), exist_ok=True)
    job = dict(job)
    job.setdefault("job_id", uuid.uuid4().hex[:12])
    job.setdefault("kind", JOB_KIND)
    job.setdefault("job_state", "active")
    job.setdefault("created_at", _now())
    job["updated_at"] = _now()
    job["fingerprint"] = _job_fingerprint(job)
    path = os.path.join(_jobs_dir(project_root), f"{job['job_id']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(job, handle, indent=2, default=str)
    os.replace(tmp, path)
    # Index lags, never leads: the record above is durable before we touch the
    # cache. Best-effort — a failed index write just means the next
    # list_jobs(rebuild=True) (or the next successful save) repairs it.
    try:
        update_global_index(
            analysis_base_root or _default_analysis_base_root(project_root),
            job, project_root,
        )
    except OSError:
        pass
    return job


def load_job(project_root: str, job_id: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(_jobs_dir(project_root), f"{str(job_id)}.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            job = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(job, dict):
        return None
    if job.get("fingerprint") != _job_fingerprint(job):
        return {"_corrupt": True, "job_id": job_id}
    return job


def list_jobs_in_root(project_root: str, *, limit: int = 50, include_corrupt: bool = False) -> List[Dict[str, Any]]:
    directory = _jobs_dir(project_root)
    rows: List[Dict[str, Any]] = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory), reverse=True):
            if not name.endswith(".json"):
                continue
            job = load_job(project_root, name[:-5])
            if not job or job.get("_corrupt"):
                if include_corrupt:
                    rows.append({"job_id": name[:-5], "corrupt": True})
                continue
            rows.append(job)
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return rows[: max(1, int(limit))]


# ── stage state machine ──────────────────────────────────────────────────────


def _recompute_cursor(job: Dict[str, Any]) -> None:
    """Point ``cursor`` at the first non-done stage in manifest order; None
    once every stage is done (awaiting ``finish_job``, a later phase)."""
    stages = job.get("stages") or {}
    for name in job.get("manifest") or []:
        if (stages.get(name) or {}).get("status") != "done":
            job["cursor"] = name
            return
    job["cursor"] = None


def advance_stage(
    project_root: str, job_id: str, stage: str, status: str, **updates: Any
) -> Dict[str, Any]:
    """Move one stage through pending -> running -> done|failed (persisted)."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    stages = dict(job.get("stages") or {})
    if stage not in stages:
        return {"success": False, "error": f"unknown stage {stage!r} (not in this job's manifest)"}
    if status not in STAGE_STATUSES:
        return {"success": False, "error": f"unknown stage status {status!r}"}
    current = stages[stage].get("status", "pending")
    if status != current and status not in _STAGE_TRANSITIONS.get(current, set()):
        return {"success": False, "error": f"illegal stage transition {current!r} -> {status!r}"}
    stages[stage] = dict(stages[stage], status=status, **updates)
    job["stages"] = stages
    _recompute_cursor(job)
    job = save_job(project_root, job)
    return {"success": True, "job": job}


# ── lease (crash recovery) ───────────────────────────────────────────────────


def _lease_expired(lease: Optional[Dict[str, Any]], *, now: float, ttl: float = LEASE_TTL_SECONDS) -> bool:
    if not lease:
        return True
    hb = _iso_to_epoch(lease.get("heartbeat_at"))
    if hb is None:
        return True
    return (now - hb) > ttl


def acquire_or_steal_lease(
    job: Dict[str, Any], holder_id: str, *, now_epoch: Optional[float] = None
) -> "tuple[bool, Dict[str, Any], Dict[str, Any]]":
    """Pure lease decision: (ok, updated_job, info). Caller persists on ok.

    Resuming a job is stealing an expired lease, not a special case — if the
    current holder's heartbeat is stale, any holder_id may take over. A live
    lease held by a DIFFERENT holder refuses (one active job per project is
    the documented posture; a genuine conflict surfaces as a refusal here,
    never a silent double-run).
    """
    now_epoch = _now_epoch() if now_epoch is None else now_epoch
    lease = job.get("lease") or {}
    current_holder = lease.get("holder_id")
    if current_holder and current_holder != holder_id and not _lease_expired(lease, now=now_epoch):
        return False, job, {
            "reason": "held_by_other",
            "holder_id": current_holder,
            "heartbeat_at": lease.get("heartbeat_at"),
        }
    stolen = bool(current_holder and current_holder != holder_id)
    job = dict(job)
    job["lease"] = {"holder_id": holder_id, "acquired_at": _now(), "heartbeat_at": _now()}
    return True, job, {"stolen": stolen, "previous_holder": current_holder}


# ── global index (rebuildable cache) ─────────────────────────────────────────


def _global_index_dir(analysis_base_root: str) -> str:
    return os.path.join(analysis_base_root, GLOBAL_INDEX_DIR_NAME)


def _global_index_path(analysis_base_root: str) -> str:
    return os.path.join(_global_index_dir(analysis_base_root), GLOBAL_INDEX_FILENAME)


def _index_stub(job: Dict[str, Any], project_root: str) -> Dict[str, Any]:
    stage_counts: Dict[str, int] = {}
    for st in (job.get("stages") or {}).values():
        status = st.get("status", "pending")
        stage_counts[status] = stage_counts.get(status, 0) + 1
    return {
        "job_id": job.get("job_id"),
        "project_root": project_root,
        "job_state": job.get("job_state"),
        "genre": (job.get("brief") or {}).get("genre"),
        "cursor": job.get("cursor"),
        "manifest_len": len(job.get("manifest") or []),
        "stage_counts": stage_counts,
        "updated_at": job.get("updated_at"),
    }


def read_global_index(analysis_base_root: str) -> Dict[str, Any]:
    path = _global_index_path(analysis_base_root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def update_global_index(analysis_base_root: str, job: Dict[str, Any], project_root: str) -> Dict[str, Any]:
    os.makedirs(_global_index_dir(analysis_base_root), exist_ok=True)
    index = read_global_index(analysis_base_root)
    job_id = job.get("job_id")
    if not job_id:
        return {"success": False, "error": "job has no job_id"}
    index[job_id] = _index_stub(job, project_root)
    path = _global_index_path(analysis_base_root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(index, handle, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)
    return {"success": True, "path": path}


def rebuild_global_index(analysis_base_root: str) -> Dict[str, Any]:
    """Re-scan every project root under analysis_base_root for job records.

    The index is a cache; this is its only source of truth reconciliation —
    directly answers "which projects have jobs" without touching Resolve.
    """
    index: Dict[str, Any] = {}
    scanned = 0
    if os.path.isdir(analysis_base_root):
        for name in sorted(os.listdir(analysis_base_root)):
            if name.startswith("_"):
                continue
            project_root = os.path.join(analysis_base_root, name)
            jobs_dir = _jobs_dir(project_root)
            if not os.path.isdir(jobs_dir):
                continue
            for job in list_jobs_in_root(project_root, limit=10_000):
                scanned += 1
                index[job["job_id"]] = _index_stub(job, project_root)
    os.makedirs(_global_index_dir(analysis_base_root), exist_ok=True)
    path = _global_index_path(analysis_base_root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(index, handle, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)
    return {"success": True, "path": path, "count": len(index), "jobs_scanned": scanned}


def list_jobs(
    analysis_base_root: str, *, limit: int = 20, rebuild: bool = False, job_state: Optional[str] = None
) -> Dict[str, Any]:
    if rebuild or not os.path.isfile(_global_index_path(analysis_base_root)):
        rebuild_global_index(analysis_base_root)
    index = read_global_index(analysis_base_root)
    rows = list(index.values())
    if job_state:
        rows = [r for r in rows if r.get("job_state") == job_state]
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return {"success": True, "jobs": rows[: max(1, int(limit))]}


# ── job creation ─────────────────────────────────────────────────────────────


def create_job(
    project_root: str,
    *,
    files: List[str],
    music: Optional[str] = None,
    target_duration_seconds: Optional[float] = None,
    genre: str = "talking_head",
    deliverable: str = DEFAULT_DELIVERABLE,
    title_text: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    stages: Optional[List[str]] = None,
    include_fusion: bool = False,
    holder_id: Optional[str] = None,
    analysis_base_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate + persist a new job. Intake is marked done here: reaching this
    call means the caller (server.py) already ran the live pre-flight (file
    existence/ffprobe, source-safety lock, bin scaffold) — same split as
    ``auto_edit.create_brief`` vs its tool's ``start_brief`` action."""
    errors = validate_job_inputs(
        files=files, genre=genre, deliverable=deliverable,
        target_duration_seconds=target_duration_seconds, title_text=title_text,
        stages=stages,
    )
    if errors:
        return {"success": False, "error": "invalid job brief", "problems": errors}
    manifest = infer_stage_manifest({"stages": stages, "include_fusion": include_fusion})
    manifest_errors = validate_manifest(manifest)
    if manifest_errors:
        return {"success": False, "error": "invalid stage manifest", "problems": manifest_errors}

    now = _now()
    stages_state = {
        name: {
            "status": "done" if name == "intake" else "pending",
            "fingerprint": None,
            "started_at": now if name == "intake" else None,
            "finished_at": now if name == "intake" else None,
            "gate": None,
            "snapshot_ids": [],
            "foreign_keys": {},
            "notes": [],
        }
        for name in manifest
    }
    holder_id = holder_id or uuid.uuid4().hex[:12]
    job: Dict[str, Any] = {
        "kind": JOB_KIND,
        "job_state": "active",
        "brief": {
            "files": list(files),
            "music": music,
            "target_duration_seconds": target_duration_seconds,
            "genre": genre,
            "deliverable": deliverable,
            "title_text": title_text,
            "options": options or {},
        },
        "manifest": manifest,
        "stages": stages_state,
        "source_safety_lock": True,
        "lease": {"holder_id": holder_id, "acquired_at": now, "heartbeat_at": now},
    }
    _recompute_cursor(job)
    job = save_job(project_root, job, analysis_base_root=analysis_base_root)
    return {"success": True, "job_id": job["job_id"], "job": job, "holder_id": holder_id}


def job_status(project_root: str, job_id: str) -> Dict[str, Any]:
    """Read-only. Never touches the lease (resume/steal is run_stage's job)."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered",
                 "job_id": job_id}
    lease = job.get("lease") or {}
    return {
        "success": True,
        "job": job,
        "cursor": job.get("cursor"),
        "manifest": job.get("manifest"),
        "lease_expired": _lease_expired(lease, now=_now_epoch()),
    }
