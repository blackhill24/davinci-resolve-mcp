"""Undo the C-locale reset that Resolve's scripting bridge performs on connect.

Found while auditing the test suite for #121: `fusionscript.scriptapp("Resolve")`
resets the process's C locale to POSIX/C. Measured on Resolve Studio 21.0.2.4,
Linux, in a shell whose LANG is en_GB.UTF-8:

    >>> locale.getpreferredencoding(False)
    'UTF-8'
    >>> import fusionscript; fusionscript.scriptapp("Resolve")
    >>> locale.getpreferredencoding(False)
    'ANSI_X3.4-1968'          # i.e. ASCII

Loading the shared object is harmless; the reset happens inside `scriptapp()`.

Why that matters far beyond `locale`: Python resolves the default encoding for
`open()`, `pathlib.read_text()/write_text()` and `subprocess(text=True)` from
that same locale, at call time. So after the first connection, every one of
those calls that did not name an encoding silently switches from UTF-8 to
ASCII — and then raises `UnicodeDecodeError` on the first non-ASCII byte:

  * an ffprobe/ffmpeg run over a clip whose path has an accent in it,
  * a subtitle or SRT file with anything outside 7-bit ASCII,
  * a project or timeline name typed in any non-English language.

The reset has to happen *mid-process* to bite, and that is exactly the shape of
this one. CPython decides UTF-8 mode once, from the locale the interpreter
started in: a process that starts in C/POSIX gets UTF-8 mode, and then reports
utf-8 from `getpreferredencoding()` whatever the live locale says. A process
that starts in a UTF-8 locale — every real launch of this server — does not, so
its encoding tracks the live locale and a later reset genuinely switches it to
ASCII. So the live trigger is a native library resetting the locale under a
running interpreter, not an environment that merely starts without a `LANG`
(#127).

The offline test suite cannot see any of this, because it never connects to
Resolve and therefore never leaves UTF-8. That is exactly the blind spot #119
and #121 are about, which is why the fix lives next to a regression test rather
than in a comment.

`restore()` puts the locale back to what the environment asked for, and is
called immediately after every `scriptapp()` in this repo.
"""
from __future__ import annotations

import locale
import logging

logger = logging.getLogger(__name__)


def restore() -> str:
    """Re-apply the environment's locale. Returns the resulting encoding name.

    Safe to call any number of times, and safe to call when nothing broke it.
    Never raises: a process that cannot set its locale should still be able to
    drive Resolve, it just keeps whatever encoding it had.
    """
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error as exc:  # unusual environments (empty LANG, C.UTF-8 absent)
        logger.debug("could not restore locale after Resolve connect: %s", exc)
    return locale.getpreferredencoding(False)
