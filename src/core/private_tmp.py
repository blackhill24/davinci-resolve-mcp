"""Per-user private scratch directory for process state files.

Anything this server drops in a *predictable* path under bare ``/tmp`` is
plantable: on a shared box another local user creates the name first — as a
symlink, or as a file they keep readable — and whatever we do to it next lands
on their target, or hands them our contents. Two concrete cases this module
exists to close (audit round 3, #143 finding 3):

- ``davinci_resolve_mcp_page.lock`` is opened ``a+`` and then ``truncate()``d,
  so a pre-planted symlink turns a page switch into "empty the victim's file".
- ``davinci_resolve_mcp_transport.json`` carries the **networked-transport
  bearer token** and, at the default umask, was written world-readable — plus
  ``src/dashboard/state.py`` reads a ``pid`` straight out of it and SIGTERMs it,
  so a plantable file is also a "kill any of this user's processes" primitive.

The fix is the one ``src/core/proc.py`` already applies to the ALSA config it
writes for Resolve: keep the file in a per-uid ``0700`` directory rather than
bare ``/tmp``, verify that directory is really ours and really a directory
(``lstat``, not ``stat``), and open the file itself ``O_NOFOLLOW`` with mode
``0600``. That module now shares this implementation.

Every entry point degrades rather than raising: callers get ``None`` and decide
whether to continue without the file (the page lock does — it is advisory) or to
skip writing it entirely (the transport state does, because it holds a secret).
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
from typing import Optional

# Base name of the per-user directory; the uid is appended.
_DIR_PREFIX = "drm-resolve"


def private_dir(prefix: str = _DIR_PREFIX) -> Optional[str]:
    """Return a per-uid ``0700`` directory under the system temp dir.

    ``None`` when it cannot be created *or* cannot be proven to be a directory
    owned by this user — an existing path we do not own is exactly the hostile
    case, so it is a refusal, never a "use it anyway".
    """
    uid = getattr(os, "getuid", None)
    if uid is None:  # pragma: no cover - Windows: per-user temp already
        path = os.path.join(tempfile.gettempdir(), prefix)
    else:
        path = os.path.join(tempfile.gettempdir(), f"{prefix}-{uid()}")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    if uid is not None and st.st_uid != uid():
        return None
    return path


def private_path(name: str, prefix: str = _DIR_PREFIX) -> Optional[str]:
    """Path for ``name`` inside :func:`private_dir`, or ``None``."""
    base = private_dir(prefix)
    if base is None:
        return None
    return os.path.join(base, name)


def open_private(path: str, flags: int, mode: int = 0o600) -> int:
    """``os.open`` with ``O_NOFOLLOW`` forced on and a private default mode.

    ``O_NOFOLLOW`` makes the final component refuse to be a symlink, which is
    what stops a planted link from redirecting the write. Raises ``OSError``
    (``ELOOP`` on a symlink) — callers handle it like any other IO failure.
    """
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


def write_private_text(path: str, text: str) -> bool:
    """Write ``text`` to ``path`` at mode 0600, refusing to follow a symlink.

    Returns True on success, False on any OS error (including a planted
    symlink). Truncating create, since these are whole-state files.
    """
    try:
        fd = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        return False
    return True


def open_private_handle(path: str, mode: str = "a+"):
    """Open ``path`` as a text handle without following a symlink.

    Only the append/update mode the page lock needs is supported: the file must
    survive across holders (so no truncate-on-open), and an existing symlink is
    an error rather than something to follow. Returns ``None`` on any OS error.
    """
    if mode != "a+":  # pragma: no cover - guard against silent misuse
        raise ValueError(f"unsupported mode {mode!r}")
    try:
        fd = open_private(path, os.O_RDWR | os.O_CREAT)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        return None
    try:
        return os.fdopen(fd, "r+", encoding="utf-8")
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
