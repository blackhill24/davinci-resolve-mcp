"""In-process registry for long DaVinci Resolve operations run off-thread.

Some Resolve calls (transcription, subtitle generation, scene-cut and Dolby
analysis, timeline export/import) block for minutes — longer than an MCP
client's tool-window timeout. start_job runs such a call on a daemon thread and
returns a short id immediately; the caller polls job_status until the job
reports "done" (with its result) or "error" (with the message).

The worker holds the same `_bridge_lock` that synchronous tool bodies take, so a
background job and a sync tool can never be inside the single-threaded scripting
bridge at once. That used to be untrue: `_run` wrapped only
`resolve_busy.long_resolve_op(label)`, which is an ADVISORY registration, not a
mutex — and its one enforcement point, `wait_until_free()`, is called from
`envelope._check()`, which 32 `get_resolve()` call sites across the compound
domains never reach (the granular server and the dashboard process never consult
the sidecar at all). So `background=true` plus any of those put two threads into
fusionscript, which per `src/core/resolve_busy.py` "simply hangs with no
feedback" (#141 finding 8).

The advisory record is kept on top of the lock: it is what makes a colliding
caller get a "busy" ANSWER rather than blocking, and it names the owner.

Ordering note: a tool body that starts a job is itself already holding
`_bridge_lock` (the threaded-dispatch wrapper takes it). There is no deadlock
because `start_job` returns immediately without waiting on the worker — the
parent releases the lock as it returns, and the worker then acquires it.

Finished jobs are pruned once older than MAX_JOB_AGE_SECONDS on any access, so
the registry cannot grow without bound.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from src.core.live_connection import _bridge_lock
from src.core.resolve_busy import long_resolve_op

# A finished job is dropped from the registry this long after it ended.
MAX_JOB_AGE_SECONDS = 60 * 60

_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


def _prune_locked(now: float) -> None:
    stale = [
        job_id
        for job_id, job in _jobs.items()
        if job["status"] != "running"
        and job["ended_at"] is not None
        and now - job["ended_at"] > MAX_JOB_AGE_SECONDS
    ]
    for job_id in stale:
        del _jobs[job_id]


def _run(job_id: str, label: str, fn: Callable[[], Any]) -> None:
    try:
        # _bridge_lock FIRST, then the advisory record: the lock is the real
        # mutual exclusion, and publishing "busy" only once we actually hold the
        # bridge keeps the record honest.
        with _bridge_lock, long_resolve_op(label):
            result = fn()
    except Exception as exc:
        with _lock:
            job = _jobs.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = f"{type(exc).__name__}: {exc}"
                job["ended_at"] = time.time()
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["status"] = "done"
            job["result"] = result
            job["ended_at"] = time.time()


def start_job(label: str, fn: Callable[[], Any]) -> str:
    """Run fn on a daemon thread; return a short job id immediately."""
    now = time.time()
    with _lock:
        _prune_locked(now)
        job_id = uuid.uuid4().hex[:8]
        while job_id in _jobs:
            job_id = uuid.uuid4().hex[:8]
        _jobs[job_id] = {
            "id": job_id,
            "label": str(label),
            "status": "running",
            "result": None,
            "error": None,
            "started_at": now,
            "ended_at": None,
        }
    threading.Thread(
        target=_run,
        args=(job_id, str(label), fn),
        name=f"bgjob-{job_id}",
        daemon=True,
    ).start()
    return job_id


def job_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Snapshot of one job, or None when the id is unknown."""
    with _lock:
        _prune_locked(time.time())
        job = _jobs.get(job_id)
        return dict(job) if job is not None else None


def list_jobs() -> List[Dict[str, Any]]:
    """Compact status of every known job."""
    with _lock:
        _prune_locked(time.time())
        return [
            {k: job[k] for k in ("id", "label", "status", "started_at", "ended_at")}
            for job in _jobs.values()
        ]
