"""Subprocess helpers that are safe to call while the MCP stdio server is live.

The server owns the JSON-RPC stdin/stdout while serving over stdio. A child
process that inherits stdin can race-read bytes off the protocol stream and
corrupt it; ``capture_output`` only redirects stdout/stderr. These wrappers
default ``stdin`` to ``DEVNULL`` so subprocess hygiene is centralized rather
than re-applied at every call site.
"""
import os
import re
import subprocess
from typing import Any, Dict, Optional

# Session preload libraries that must never be inherited by processes we spawn.
# NoMachine exports LD_PRELOAD=/usr/NX/lib/libnxegl.so into the whole desktop
# session; its hooks crash two classes of children: Resolve segfaults during
# NVIDIA GL context creation as soon as it races a plugin dlopen (page
# switches), and CUDA/cuDNN users (whisper, GPU ffmpeg) abort with "Cannot
# load symbol cudnnGetVersion". See tests/test_spawn_env_sanitization.py.
_CRASHY_PRELOAD_TOKENS = ("libnxegl.so",)


def sanitized_spawn_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Copy of the environment safe for spawning GPU-touching children on Linux.

    Strips known-crashy entries (e.g. NoMachine's libnxegl.so) from LD_PRELOAD,
    dropping the variable entirely if nothing survives; all other variables are
    passed through unchanged.
    """
    env = dict(os.environ if base_env is None else base_env)
    preload = env.get("LD_PRELOAD", "")
    if preload:
        kept = [
            entry
            for entry in re.split(r"[:\s]+", preload)
            if entry and not any(token in entry for token in _CRASHY_PRELOAD_TOKENS)
        ]
        if kept:
            env["LD_PRELOAD"] = ":".join(kept)
        else:
            env.pop("LD_PRELOAD", None)
    return env


def safe_run(*args: Any, **kwargs: Any) -> "subprocess.CompletedProcess":
    """subprocess.run with stdin defaulted to DEVNULL (override by passing stdin).

    If ``input`` is given, stdin is left alone — subprocess forbids passing both.
    """
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(*args, **kwargs)


def safe_popen(*args: Any, **kwargs: Any) -> "subprocess.Popen":
    """subprocess.Popen with stdin defaulted to DEVNULL (override by passing stdin)."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen(*args, **kwargs)
