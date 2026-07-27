"""Whether a Resolve auto-launch is allowed for the call currently running.

Deliberately tiny and dependency-free: both connection bootstraps —
``src/core/live_connection.py`` (compound server) and
``src/granular/common.py`` (granular ``--full`` server) — import it, and neither
can import the other (each owns its own module-level ``resolve`` handle).

The flag exists for MCP **resource** handlers. Hosts read resources passively,
without a user turn, so a resource that reaches Resolve while Resolve is down
would auto-launch it and block for up to 60s
(``LAUNCH_POLL_ATTEMPTS`` x poll interval) — freezing the whole server, stdio
read loop included, on a probe the user never initiated. That is the opposite of
the "all resource handlers MUST be cheap" contract those handlers are written to
(#143 finding 6). ``_install_threaded_resource_dispatch`` wraps every resource
in :func:`passive_resolve_probe`, so the suppression reaches nested helpers
(``get_project_manager``, ``get_current_project``, ...) without threading a flag
through each one.

Tool bodies are unaffected: a tool call is a deliberate action, so launching
Resolve for it stays correct.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

_PASSIVE_PROBE = threading.local()


@contextmanager
def passive_resolve_probe():
    """Within this block, ``get_resolve()`` connects but never launches.

    Re-entrant: the previous value is restored on exit, so nesting is safe. The
    flag is per-thread, which is what the worker-thread offload needs — it is
    set inside the thread that actually runs the handler.
    """
    previous = getattr(_PASSIVE_PROBE, "active", False)
    _PASSIVE_PROBE.active = True
    try:
        yield
    finally:
        _PASSIVE_PROBE.active = previous


def autolaunch_suppressed() -> bool:
    """True when this call must not start Resolve.

    Either we are inside a passive probe, or the operator opted out globally
    (test harnesses set ``DAVINCI_MCP_NO_AUTOLAUNCH=1`` to fail fast with
    NOT_CONNECTED instead of blocking up to 60s on a Resolve launch).
    """
    if getattr(_PASSIVE_PROBE, "active", False):
        return True
    return bool(os.environ.get("DAVINCI_MCP_NO_AUTOLAUNCH"))
