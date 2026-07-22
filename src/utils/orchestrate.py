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

STAGE_STATUSES = ("pending", "running", "done", "failed", "awaiting_offline_artifact")
_STAGE_TRANSITIONS: Dict[str, set] = {
    "pending": {"running"},
    "running": {"done", "failed", "awaiting_offline_artifact"},
    # A later phase's drift-refuse re-plan resets a stale "done" back to
    # pending rather than blind-continuing past it.
    "done": {"pending"},
    "failed": {"pending", "running"},  # clean retry
    # request_offline_op parks the stage here while the host does the actual
    # quit/patch/relaunch dance (outside this module, see request_offline_op
    # below); resolve_offline_op moves it back to running (op succeeded) or
    # failed (op errored) once that's reported back.
    "awaiting_offline_artifact": {"running", "failed"},
}

# The narrow per-ACTION slice of the advanced (Node) server that needs the
# Resolve project CLOSED to touch its DB safely — everything else is pure
# file/DB-read and already runs in-band via advanced_bridge.run_advanced_tool
# (see docs/kernels/orchestration-kernel.md "Offline compute"). This whitelist
# is what request_offline_op checks against; anything not listed here is
# refused with a pointer back to the in-band bridge instead.
OFFLINE_CLOSED_ACTIONS = frozenset({
    ("conform", "fix_reverse_clip"),
    ("offline_ref", "link_in_project"),
    ("offline_ref", "unlink_in_project"),
    ("project_db", "relayout_node_graphs"),
    ("fairlight", "read_buses_from_db"),
    ("fairlight", "expand_buses"),
    ("fairlight", "export_template"),
    ("fairlight", "import_template"),
    ("fairlight", "backup"),
    ("fairlight", "restore"),
})

JOB_STATES = ("active", "finished", "aborted")

DEFAULT_DELIVERABLE = "youtube_1080p"

# ── gates ─────────────────────────────────────────────────────────────────
# G1 post-plan, G2 post-grade, G3 pre-render — fixed 1:1 onto manifest stage
# names, since a gate's job is to checkpoint the stage it names.
GATE_STAGE = {"G1": "edit", "G2": "grade", "G3": "deliver"}
GATE_NAMES = tuple(GATE_STAGE)
GATE_MODES = ("auto", "standard", "paranoid")
DEFAULT_GATES_MODE = "standard"

# Pre-stage snapshot kind per stage: grade re-versions in place (cheap,
# in-page), everything else that mutates the timeline gets a full duplicate
# so a failed stage can be rolled back by swapping timelines. Stages that
# never mutate destructively (intake, ingest — additive only) or that lean
# on Resolve's own resumable mechanism (deliver, per the locked design) or
# are read-only (review) need no pre-stage snapshot.
SNAPSHOT_KIND_BY_STAGE = {
    "edit": "timeline_duplicate",
    "conform": "timeline_duplicate",
    "grade": "grade_version",
    "audio": "timeline_duplicate",
    "fusion": "timeline_duplicate",
}


def snapshot_label(job_id: str, stage: str) -> str:
    return f"_orch_{job_id}_{stage}"


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
    gates: Any = None,
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
    if gates is not None and gates not in GATE_MODES:
        errors.append(f"gates must be one of {GATE_MODES}")
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
    gates: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate + persist a new job. Intake is marked done here: reaching this
    call means the caller (server.py) already ran the live pre-flight (file
    existence/ffprobe, source-safety lock, bin scaffold) — same split as
    ``auto_edit.create_brief`` vs its tool's ``start_brief`` action."""
    errors = validate_job_inputs(
        files=files, genre=genre, deliverable=deliverable,
        target_duration_seconds=target_duration_seconds, title_text=title_text,
        stages=stages, gates=gates,
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
        "gates_mode": gates or DEFAULT_GATES_MODE,
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


# ── fingerprints + drift-refuse ──────────────────────────────────────────────
#
# The coarse fingerprint is {timeline_item_count, grade_version_id,
# media_path_set_hash} — cheap, project-wide, and monotonically built up
# stage by stage. That last property is why resume only ever compares
# against ONE checkpoint, not every historical stage: each done stage's
# recorded fingerprint is the project's state *at that moment*, and normal
# forward progress changes it by design (ingest adds media, edit/conform
# change item counts, grade adds versions). Comparing "now" against every
# historical snapshot would false-positive on ordinary progress. The only
# checkpoint a fresh run_stage(cursor) call actually depends on is the last
# done stage's fingerprint (the frontier just behind cursor) — so that's
# what gets re-probed on resume. "Stop at the first mismatch" degenerates to
# a single comparison because there is only one checkpoint that matters.

_FINGERPRINT_KEYS = ("timeline_item_count", "grade_version_id", "media_path_set_hash")


def fingerprints_equal(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return all(a.get(k) == b.get(k) for k in _FINGERPRINT_KEYS)


def _last_done_fingerprint(job: Dict[str, Any]):
    """(stage_name, fingerprint) of the last done stage that recorded one, or
    (None, None) if no baseline exists yet (nothing to verify)."""
    manifest = job.get("manifest") or []
    stages = job.get("stages") or {}
    baseline_stage, baseline_fp = None, None
    for name in manifest:
        st = stages.get(name) or {}
        if st.get("status") != "done":
            break
        if st.get("fingerprint"):
            baseline_stage, baseline_fp = name, st["fingerprint"]
    return baseline_stage, baseline_fp


def check_resume(job: Dict[str, Any], current_fingerprint: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure drift check against the frontier checkpoint. Never mutates."""
    baseline_stage, baseline_fp = _last_done_fingerprint(job)
    if baseline_fp is None:
        return {"success": True, "drifted": False, "checked_stage": None,
                "message": "no fingerprint baseline recorded yet — nothing to verify"}
    drifted = not fingerprints_equal(baseline_fp, current_fingerprint)
    return {
        "success": True,
        "drifted": drifted,
        "checked_stage": baseline_stage,
        "expected": baseline_fp,
        "actual": current_fingerprint,
        "message": (
            f"drift detected since {baseline_stage!r} completed — refuse to resume; "
            f"re-plan {baseline_stage!r} before continuing"
        ) if drifted else (
            f"state matches the fingerprint recorded after {baseline_stage!r} — safe to resume"
        ),
    }


def capture_stage_fingerprint(
    project_root: str, job_id: str, stage: str, fingerprint: Dict[str, Any]
) -> Dict[str, Any]:
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    stages = dict(job.get("stages") or {})
    if stage not in stages:
        return {"success": False, "error": f"unknown stage {stage!r}"}
    stages[stage] = dict(stages[stage], fingerprint=fingerprint)
    job["stages"] = stages
    job = save_job(project_root, job)
    return {"success": True, "job": job}


def force_replan_stage(project_root: str, job_id: str, stage: str) -> Dict[str, Any]:
    """Concrete remediation for a drifted checkpoint: reset a done stage back
    to pending (advance_stage already allows done -> pending) and void its
    gate, since an approval over a state that no longer exists is worthless."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    stages = job.get("stages") or {}
    if stage not in stages:
        return {"success": False, "error": f"unknown stage {stage!r}"}
    if stages[stage].get("status") != "done":
        return {"success": False, "error": f"stage {stage!r} is not done — nothing to re-plan"}
    return advance_stage(project_root, job_id, stage, "pending", gate=None, fingerprint=None)


# ── snapshots (record-level bookkeeping; live creation/deletion is server.py's job) ──


def record_snapshot(
    project_root: str, job_id: str, stage: str, snapshot_id: str, *, kind: str
) -> Dict[str, Any]:
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    stages = dict(job.get("stages") or {})
    if stage not in stages:
        return {"success": False, "error": f"unknown stage {stage!r}"}
    existing = list(stages[stage].get("snapshot_ids") or [])
    existing.append({"id": snapshot_id, "kind": kind, "created_at": _now()})
    stages[stage] = dict(stages[stage], snapshot_ids=existing)
    job["stages"] = stages
    job = save_job(project_root, job)
    return {"success": True, "job": job}


def _clear_stage_snapshots(stages: Dict[str, Any], stage: str) -> "tuple[Dict[str, Any], List[Dict[str, Any]]]":
    cleared = list(stages[stage].get("snapshot_ids") or [])
    stages = dict(stages)
    stages[stage] = dict(stages[stage], snapshot_ids=[])
    return stages, cleared


# ── gates ─────────────────────────────────────────────────────────────────


def gates_mode(job: Dict[str, Any]) -> str:
    return job.get("gates_mode") or DEFAULT_GATES_MODE


def gate_is_valid(gate: Optional[Dict[str, Any]], current_fingerprint: Optional[Dict[str, Any]]) -> bool:
    """Fingerprint-bound: a stale approval (state drifted since it was granted)
    is void. A forced approval explicitly bypassed the drift check, so it
    never voids on its own say-so."""
    if not gate or not gate.get("approved_at"):
        return False
    if gate.get("forced"):
        return True
    return fingerprints_equal(gate.get("fingerprint"), current_fingerprint)


def _gate_precondition_ok(job: Dict[str, Any], gate: str, stage: str) -> bool:
    """Each gate checkpoints a different POINT in its stage's lifecycle —
    "done" is the right precondition for exactly one of them:

    - G1 (post-plan, PRE-build): the edit stage is mid-flight (a plan
      exists) when this fires — requiring "done" would be a chicken-and-egg
      deadlock, since the stage can't finish building without G1 first.
    - G3 (pre-render): same shape — deliver can't be "done" without having
      rendered, and it can't render without G3 first. The real precondition
      is that everything BEFORE deliver is done (the pipeline has actually
      reached it).
    - G2 (post-grade): grade fully executes, then G2 checkpoints the
      result before anything downstream proceeds — "done" is correct as-is.
    """
    stages = job.get("stages") or {}
    if gate == "G1":
        return bool((stages.get(stage) or {}).get("foreign_keys", {}).get("plan_id"))
    if gate == "G3":
        manifest = job.get("manifest") or []
        if stage not in manifest:
            return False
        idx = manifest.index(stage)
        return all((stages.get(s) or {}).get("status") == "done" for s in manifest[:idx])
    return (stages.get(stage) or {}).get("status") == "done"


def evaluate_gate_request(
    job: Dict[str, Any],
    gate: str,
    *,
    current_fingerprint: Optional[Dict[str, Any]],
    vision_assessment: Optional[str] = None,
    preview_frame_path: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Pure decision layer for ``approve_gate``. One of:
      {"success": False, "error"}                                  — refuse
      {"success": True, "already_approved": True, "gate"}          — idempotent no-op
      {"success": True, "record", "needs_confirm": bool}           — caller
        mints/consumes a confirm-token (needs_confirm=True) or records the
        approval directly (adopted inner gate, or auto mode without force).
    """
    if gate not in GATE_STAGE:
        return {"success": False, "error": f"unknown gate {gate!r} (must be one of {GATE_NAMES})"}
    stage = GATE_STAGE[gate]
    stages = job.get("stages") or {}
    st = stages.get(stage)
    if st is None:
        return {"success": False, "error": f"stage {stage!r} (gated by {gate}) is not in this job's manifest"}
    if not _gate_precondition_ok(job, gate, stage):
        return {"success": False,
                "error": f"{gate} precondition not met for {stage!r} (status={st.get('status')!r})"}

    if not force:
        drift = check_resume(job, current_fingerprint)
        if drift.get("drifted") and drift.get("checked_stage") == stage:
            return {"success": False, "error": drift["message"], "drift": drift}

    # G2's vision requirement is evidence, not a confirmation click — force
    # only bypasses the drift-halt (per the locked design), never this.
    if gate == "G2":
        if not (vision_assessment and str(vision_assessment).strip()):
            return {"success": False,
                    "error": "G2 requires a host-supplied look assessment of a rendered frame "
                             "(vision_assessment) — no blind grade approval."}
        if not (preview_frame_path and str(preview_frame_path).strip()):
            return {"success": False,
                    "error": "G2 requires preview_frame_path — render a frame before approving."}

    mode = gates_mode(job)
    existing_gate = st.get("gate")
    if mode != "paranoid" and gate_is_valid(existing_gate, current_fingerprint):
        return {"success": True, "already_approved": True, "gate": existing_gate}

    adopted = bool((st.get("foreign_keys") or {}).get("inner_gate_approved_at"))
    record: Dict[str, Any] = {
        "fingerprint": current_fingerprint,
        "mode": mode,
        "adopted": adopted,
        "forced": bool(force),
    }
    if gate == "G2":
        record["vision_assessment"] = vision_assessment
        record["preview_frame_path"] = preview_frame_path

    if adopted:
        # The inner tool (e.g. auto_edit.approve_cut) already ran its own
        # confirm-token ceremony — minting a second one would double-gate.
        return {"success": True, "record": record, "needs_confirm": False}
    if mode == "auto" and not force:
        # Pre-authorized: still passed the drift check above, just skips the
        # human confirm-token click.
        return {"success": True, "record": record, "needs_confirm": False}
    return {"success": True, "record": record, "needs_confirm": True}


def record_gate_approval(project_root: str, job_id: str, gate: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """Persist an approved gate and GC the gated stage's snapshot bookkeeping
    (approval = committing forward, so the pre-mutation rollback snapshot is
    no longer needed). Returns the cleared snapshot list so the caller
    (server.py) can delete the live artifacts."""
    if gate not in GATE_STAGE:
        return {"success": False, "error": f"unknown gate {gate!r} (must be one of {GATE_NAMES})"}
    stage = GATE_STAGE[gate]
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    stages = job.get("stages") or {}
    if stage not in stages:
        return {"success": False, "error": f"unknown stage {stage!r}"}
    record = dict(record)
    record["approved_at"] = _now()
    stages, cleared = _clear_stage_snapshots(stages, stage)
    stages[stage] = dict(stages[stage], gate=record)
    job["stages"] = stages
    job = save_job(project_root, job)
    return {"success": True, "job": job, "gate": record, "snapshots_to_clean": cleared}


def void_stage_gate(project_root: str, job_id: str, stage: str) -> Dict[str, Any]:
    """A stage revision (e.g. revise_stage on the edit plan) invalidates any
    prior gate approval — it approved a plan that no longer exists."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    stages = dict(job.get("stages") or {})
    if stage not in stages:
        return {"success": False, "error": f"unknown stage {stage!r}"}
    stages[stage] = dict(stages[stage], gate=None)
    job["stages"] = stages
    job = save_job(project_root, job)
    return {"success": True, "job": job}


def can_run_stage(job: Dict[str, Any], stage: str) -> Optional[str]:
    """None if `stage` is runnable now, else a refusal message. Manifest-order
    only — run_stage never lets a later stage jump ahead of the cursor, and
    the P2 gates already guard the checkpoints between stages."""
    manifest = job.get("manifest") or []
    if stage not in manifest:
        return f"stage {stage!r} is not in this job's manifest"
    if job.get("cursor") != stage:
        return f"stage {stage!r} is not the current cursor (cursor={job.get('cursor')!r})"
    status = (job.get("stages") or {}).get(stage, {}).get("status")
    if status not in ("pending", "failed", "running"):
        return f"stage {stage!r} is {status!r} — nothing to run"
    return None


# ── offline (Resolve-closed) op pause/resume ─────────────────────────────────
#
# A narrow slice of the advanced server needs the Resolve project CLOSED
# (OFFLINE_CLOSED_ACTIONS above) — request_offline_op parks the current
# cursor stage at "awaiting_offline_artifact" and records exactly what the
# host still needs to do; resolve_offline_op un-parks it once that's done.
# Neither function touches Resolve's process lifecycle itself — quitting and
# relaunching stay on the existing, separately-permissioned tools
# (resolve_control.quit_app/restart_app, server.py's launch). This module
# only makes the pause survive a context reset: the job record captures the
# pending op so a fresh session can see exactly what's outstanding via
# job_status, same as any other stage.


def default_offline_op_instruction(tool: str, action: str) -> str:
    """Human-readable handoff text for a paused stage — the actual quit/
    relaunch stays on the existing, permission-gated tools (resolve_control
    quit_app/restart_app or server.py's launch); this module never calls
    them itself."""
    return (
        f"Resolve-closed op requested: {tool}.{action}. To resume: "
        "1) quit_app() (or otherwise close the project) 2) call the "
        f"resolve-advanced '{tool}' tool, action '{action}', with the "
        "project CLOSED 3) relaunch Resolve (launch()) 4) call "
        "orchestrate.resolve_offline_op(job_id=..., result=<the op's "
        "returned payload>) to resume this stage."
    )


def request_offline_op(
    project_root: str,
    job_id: str,
    stage: str,
    *,
    tool: str,
    action: str,
    args: Optional[Dict[str, Any]] = None,
    instruction: Optional[str] = None,
) -> Dict[str, Any]:
    """Park the current cursor stage on a Resolve-closed advanced-server op.

    Refuses outright if `(tool, action)` isn't in OFFLINE_CLOSED_ACTIONS —
    that's a pure file/DB-read action and belongs on the in-band
    advanced_bridge.run_advanced_tool path instead (no pause needed)."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    manifest = job.get("manifest") or []
    if stage not in manifest:
        return {"success": False, "error": f"stage {stage!r} is not in this job's manifest"}
    if job.get("cursor") != stage:
        return {"success": False, "error": f"stage {stage!r} is not the current cursor (cursor={job.get('cursor')!r})"}
    key = (str(tool), str(action))
    if key not in OFFLINE_CLOSED_ACTIONS:
        return {
            "success": False,
            "error": (
                f"{tool}.{action} does not require Resolve closed — call it in-band "
                "via advanced_bridge.run_advanced_tool instead (no pause needed)"
            ),
        }
    status = (job.get("stages") or {}).get(stage, {}).get("status")
    if status == "pending":
        started = advance_stage(project_root, job_id, stage, "running")
        if not started.get("success"):
            return started
        job = started["job"]
        status = "running"
    if status != "running":
        return {"success": False, "error": f"stage {stage!r} is {status!r} — cannot request an offline op"}
    pending_op = {
        "stage": stage,
        "tool": str(tool),
        "action": str(action),
        "args": dict(args or {}),
        "requested_at": _now(),
        "instruction": instruction or default_offline_op_instruction(tool, action),
    }
    job = dict(job)
    job["pending_offline_op"] = pending_op
    job = save_job(project_root, job)
    parked = advance_stage(project_root, job_id, stage, "awaiting_offline_artifact")
    if not parked.get("success"):
        return parked
    return {"success": True, "job": parked["job"], "pending_offline_op": pending_op}


def resolve_offline_op(project_root: str, job_id: str, *, result: Dict[str, Any]) -> Dict[str, Any]:
    """Un-park the stage a prior request_offline_op parked, per the host-
    reported `result` of actually running the op (project closed, advanced
    tool called, project reopened). Success resumes "running" so the stage's
    normal run_stage delegate can pick up and finish; failure marks the
    stage "failed" (same clean-retry path a live-mutation failure gets)."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    pending = job.get("pending_offline_op")
    if not pending:
        return {"success": False, "error": "no pending offline op recorded for this job"}
    stage = pending["stage"]
    stages = job.get("stages") or {}
    if (stages.get(stage) or {}).get("status") != "awaiting_offline_artifact":
        return {"success": False, "error": f"stage {stage!r} is not awaiting an offline op"}
    ok = bool(isinstance(result, dict) and result.get("success"))
    note = (
        f"offline op {pending['tool']}.{pending['action']} succeeded" if ok
        else f"offline op {pending['tool']}.{pending['action']} failed: "
             f"{result.get('error') if isinstance(result, dict) else result}"
    )
    job = dict(job)
    job["pending_offline_op"] = None
    job = save_job(project_root, job)
    resumed = advance_stage(project_root, job_id, stage, "running" if ok else "failed", notes=[note])
    if not resumed.get("success"):
        return resumed
    return {"success": True, "job": resumed["job"], "stage": stage, "resumed": ok, "note": note}


def rollback_stage(
    project_root: str, job_id: str, stage: str, *, snapshot_consumed: bool
) -> Dict[str, Any]:
    """Bookkeeping half of a rollback: reset the stage to pending (clean
    retry) after the live layer has restored the snapshot. `snapshot_consumed`
    tells us whether the restore used up the recorded snapshot (a
    timeline_duplicate is renamed back into place — gone) or left it reusable
    (a grade_version LoadVersion doesn't consume the version)."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    stages = job.get("stages") or {}
    if stage not in stages:
        return {"success": False, "error": f"unknown stage {stage!r}"}
    if stages[stage].get("status") not in ("failed", "running"):
        return {"success": False,
                "error": f"stage {stage!r} is {stages[stage].get('status')!r} — nothing to roll back"}
    result = advance_stage(project_root, job_id, stage, "pending")
    if not result.get("success"):
        return result
    if snapshot_consumed:
        stages, _cleared = _clear_stage_snapshots(result["job"].get("stages") or {}, stage)
        job = dict(result["job"])
        job["stages"] = stages
        job = save_job(project_root, job)
        result = {"success": True, "job": job}
    return result


def finish_job(project_root: str, job_id: str, *, output_path: Optional[str] = None) -> Dict[str, Any]:
    """Bookkeeping half of finish_job: refuses unless every manifest stage is
    done, then marks the job finished and hands back every stage's remaining
    snapshot bookkeeping (across the WHOLE job, not just one gated stage) for
    the live layer to purge — the final sweep P2's per-gate GC doesn't reach
    (e.g. a stage the user never explicitly gated)."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    manifest = job.get("manifest") or []
    stages = job.get("stages") or {}
    incomplete = [name for name in manifest if (stages.get(name) or {}).get("status") != "done"]
    if incomplete:
        return {"success": False, "error": f"job is not finishable — stage(s) not done: {incomplete}"}
    all_snapshots: List[Dict[str, Any]] = []
    cleared_stages = dict(stages)
    for name in manifest:
        snaps = list(cleared_stages.get(name, {}).get("snapshot_ids") or [])
        if snaps:
            all_snapshots.extend(snaps)
            cleared_stages[name] = dict(cleared_stages[name], snapshot_ids=[])
    job = dict(job)
    job["stages"] = cleared_stages
    job["job_state"] = "finished"
    job["finished_at"] = _now()
    if output_path is not None:
        job["output_path"] = output_path
    job = save_job(project_root, job)
    return {"success": True, "job": job, "snapshots_to_clean": all_snapshots}


def set_stage_foreign_keys(project_root: str, job_id: str, stage: str, **foreign_keys: Any) -> Dict[str, Any]:
    """Merge foreign keys (e.g. auto_edit brief_id/plan_id) into a stage —
    never copies the sub-tool's own state, just points at it."""
    job = load_job(project_root, job_id)
    if not job:
        return {"success": False, "error": f"job not found: {job_id!r}"}
    if job.get("_corrupt"):
        return {"success": False, "error": "job fingerprint mismatch — record corrupted or tampered"}
    stages = dict(job.get("stages") or {})
    if stage not in stages:
        return {"success": False, "error": f"unknown stage {stage!r}"}
    merged = dict(stages[stage].get("foreign_keys") or {})
    merged.update(foreign_keys)
    stages[stage] = dict(stages[stage], foreign_keys=merged)
    job["stages"] = stages
    job = save_job(project_root, job)
    return {"success": True, "job": job}
