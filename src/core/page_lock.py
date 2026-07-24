"""Serialize DaVinci Resolve page switches across threads and processes.

Resolve has a single globally-active page (Edit / Color / Fusion / Fairlight /
Deliver / Cut). An operation that switches the page, does work there, and reads a
result is only correct if no other operation flips the page underneath it. With a
single stdio client that never happens, but the moment two agents (threads, or
separate server processes) drive one Resolve, concurrent page switches corrupt
each other. This primitive serializes the critical section, so it must be in
place before any concurrent-agent feature ships.

- Intra-process: a reentrant lock (nested page_lock() calls are safe).
- Inter-process: a best-effort advisory file lock around the OUTERMOST section
  (fcntl). On platforms without fcntl (Windows) the inter-process guard is a
  no-op and only the intra-process lock applies.

The file lock is acquired with a bounded wait, not a blocking flock(). A *hung*
holder (as opposed to a dead one, whose lock the kernel releases) would
otherwise freeze every other MCP process's page switches forever with no
diagnostic at all. After the timeout this logs which PID is holding it and
proceeds without the inter-process guard — degraded, but consistent with the
"best-effort, never block real work" contract the rest of this module already
follows.

Usage:

    with page_lock():
        resolve.OpenPage("color")
        ... do color-page work, read results ...
"""
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager

try:
    import fcntl  # type: ignore
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAS_FCNTL = False

logger = logging.getLogger("resolve-mcp.page-lock")

_INTRA = threading.RLock()
_LOCKFILE = os.path.join(tempfile.gettempdir(), "davinci_resolve_mcp_page.lock")

# How long to wait for another process's page-switch section before giving up on
# the inter-process guard. Generous — a legitimate page switch plus its work is
# seconds, not minutes — but finite.
PAGE_LOCK_TIMEOUT_SECONDS = float(os.environ.get("DAVINCI_MCP_PAGE_LOCK_TIMEOUT", "60"))
_POLL_INTERVAL_SECONDS = 0.1

# Nesting depth and the held file handle, both guarded by _INTRA. The file lock
# is taken only at the outermost level — a second fcntl.flock() on a new fd from
# the same process would block on the first, deadlocking nested page_lock()s.
_depth = 0
_fh = None


def _holder_pid(fh) -> str:
    """Best-effort read of the PID the current holder stamped into the lock file."""
    try:
        fh.seek(0)
        return fh.read(64).strip() or "unknown"
    except OSError:
        return "unknown"


def _acquire_file_lock(timeout: float = None):
    """Take the advisory file lock, waiting at most `timeout` seconds.

    Returns the open handle on success, or None when the lock couldn't be taken
    (timed out, or the file couldn't be opened at all). None means the caller
    runs without the inter-process guard — the intra-process lock still holds.
    """
    if timeout is None:
        timeout = PAGE_LOCK_TIMEOUT_SECONDS
    try:
        # a+ rather than w: opening must not truncate the PID a live holder wrote.
        fh = open(_LOCKFILE, "a+")
    except OSError as exc:
        logger.debug("Page lock file %s unavailable (%s); continuing without it", _LOCKFILE, exc)
        return None

    deadline = time.monotonic() + timeout
    warned = False
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                logger.warning(
                    "Page lock held by PID %s for more than %.0fs — proceeding WITHOUT the "
                    "inter-process guard. Concurrent page switches may now race. If that PID "
                    "is a hung Resolve MCP process, kill it; lock file: %s",
                    _holder_pid(fh), timeout, _LOCKFILE,
                )
                fh.close()
                return None
            if not warned:
                logger.info("Waiting for the page lock (held by PID %s)", _holder_pid(fh))
                warned = True
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue
        except OSError as exc:
            # Advisory lock is best-effort; never block real work on it.
            logger.debug("Page lock flock failed (%s); continuing without it", exc)
            fh.close()
            return None

        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass  # The PID stamp is only a diagnostic; the lock itself is held.
        return fh


@contextmanager
def page_lock():
    """Hold the page-switch lock for the duration of the block (reentrant)."""
    global _depth, _fh
    _INTRA.acquire()
    _depth += 1
    try:
        if _depth == 1 and _HAS_FCNTL:
            _fh = _acquire_file_lock()
        yield
    finally:
        _depth -= 1
        if _depth == 0 and _fh is not None:
            try:
                fcntl.flock(_fh, fcntl.LOCK_UN)
            except OSError:
                pass
            _fh.close()
            _fh = None
        _INTRA.release()


def open_page_serialized(resolve, page):
    """Switch Resolve to `page` under the page lock. Returns OpenPage's result."""
    with page_lock():
        return resolve.OpenPage(page)
