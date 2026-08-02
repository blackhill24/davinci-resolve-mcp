"""Shared test configuration — the repo's first conftest (#119 task 1/3).

Two jobs:

1. Put the repo root on ``sys.path`` once, centrally. Every test file currently
   does its own ``sys.path.insert(0, ...parents[N])`` dance; new tests do not
   need to, and the existing ones keep working (the insert is idempotent).
2. Expose the faithful Resolve bridge double as fixtures, so a test never reaches
   for ``MagicMock`` to stand in for a ``PyRemoteObject``. See
   ``tests/bridge_double.py`` for why a MagicMock is actively harmful here.

`unittest.TestCase`-style tests cannot take fixtures; those import
``tests.bridge_double`` directly. The fixtures below are for plain pytest tests.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# No test may restart the developer's audio session manager. The launch tests
# patch subprocess.Popen but not resolve_spawn_env(), so on a Linux box with a
# free hw pair the spawn really does hand out an ALSA config and really does
# arm the post-exit audio-restore watcher — against a mock whose wait() returns
# instantly. Set before src.core.proc is imported, and before the per-test
# os.environ snapshot, so it is not seen as a leak (#129).
os.environ.setdefault("RESOLVE_MCP_NO_AUDIO_RESTORE", "1")

# Belt to the guard below's braces: even if a connection bootstrap is reached
# through a path the guard does not cover, it must fail fast with NOT_CONNECTED
# rather than *starting* Resolve. Before #224 the offline suite really did spawn
# /opt/resolve/bin/resolve on a developer box mid-run. Set before the per-test
# os.environ snapshot so it is not seen as a leak.
os.environ.setdefault("DAVINCI_MCP_NO_AUTOLAUNCH", "1")

from tests.bridge_double import (  # noqa: E402
    RESOLVE_EXPORT_CONSTANTS,
    ResolveBridgeDouble,
    make_resolve,
)


# Not leaks:
#   PYTEST_CURRENT_TEST — pytest rewrites it around every phase of every test.
#   RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB — src/resolve_mcp_server.py sets these
#     at import time; they are how the DaVinciResolveScript module is located at
#     all. Whichever test imports the entry point first "leaks" them, and the
#     value is the same one every subsequent import would compute.
_ENV_LEAK_EXEMPT = frozenset({
    "PYTEST_CURRENT_TEST",
    "RESOLVE_SCRIPT_API",
    "RESOLVE_SCRIPT_LIB",
})


@pytest.fixture(autouse=True)
def _no_env_leak():
    """Fail the test that leaves ``os.environ`` modified (#121 task 4).

    Deliberately a *failure*, not a silent restore. Silent restoration keeps the
    suite passing while the leak is still there, so the next test to depend on
    the leaked value stays order-dependent and nothing points at the culprit.
    Failing names the exact test that leaked, on the run that introduced it.

    Tests that need a different environment set it with
    ``mock.patch.dict(os.environ, ...)`` / ``monkeypatch.setenv``, both of which
    unwind before this fixture's teardown runs.
    """
    def snapshot():
        return {k: v for k, v in os.environ.items() if k not in _ENV_LEAK_EXEMPT}

    before = snapshot()
    yield
    after = snapshot()
    if after == before:
        return
    added = {k: after[k] for k in after.keys() - before.keys()}
    removed = sorted(before.keys() - after.keys())
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    raise AssertionError(
        "test leaked os.environ changes into the rest of the run "
        f"(added={added}, removed={removed}, changed={changed}); "
        "use mock.patch.dict(os.environ, ...) or monkeypatch.setenv instead"
    )


def _reset_module_globals():
    """Clear the process-global caches/registries in ``src/`` (#121 task 4).

    These are the module-level mutables a test can write to and a later test can
    then read. Before this fixture, individual files cleared some of them in
    their own ``setUp`` (``_CONFIRM_TOKENS`` in four files, the dashboard caches
    in one) — which meant the cleanup ran only where somebody had remembered it,
    and a *new* test that minted a confirm token silently handed it to whatever
    ran next.

    Imports are lazy and failures are swallowed on purpose: conftest must not
    make an unrelated import error look like a collection failure of every test.
    """
    try:
        from src.core import tool_kernel

        tool_kernel._CONFIRM_TOKENS.clear()
    except Exception:
        pass
    try:
        from src.core import failure_tracker

        failure_tracker._FAILURES.clear()
    except Exception:
        pass
    try:
        from src.dashboard import media_inventory

        media_inventory._PATH_EXISTS_CACHE.clear()
        media_inventory._INVENTORY_CACHE.clear()
    except Exception:
        pass
    try:
        # Cached sqlite handles keyed by db path; connect() reopens on demand, so
        # closing them between tests is safe and stops a temp-dir DB from being
        # reused after its directory is gone.
        from src.core import timeline_brain_db

        timeline_brain_db.close_all()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_process_state():
    _reset_module_globals()
    yield
    _reset_module_globals()


@pytest.fixture
def bridge_double():
    """Factory for a faithful ``PyRemoteObject`` double.

        def test_x(bridge_double):
            tl = bridge_double(methods={"Export": True})
    """
    return ResolveBridgeDouble


@pytest.fixture
def resolve_double():
    """A top-level ``resolve`` double carrying the real EXPORT_* constant names."""
    return make_resolve()


@pytest.fixture
def export_constants():
    """The EXPORT_* name -> value mapping the ``resolve`` double exposes."""
    return dict(RESOLVE_EXPORT_CONSTANTS)


# ── the offline suite must not reach a real Resolve (#224) ──────────────────
#
# Every "green for the wrong reason" failure in #224 has the same shape: a test
# patches `get_resolve` on the wrong module, the real bootstrap runs instead,
# and on the developer's box — where Resolve is running — the call *succeeds*,
# so the assertion passes without ever exercising the code it names. On a clean
# CI runner the same test fails with a connection error. The test was never
# testing what it claimed, on either machine; only one of the two was honest
# about it.
#
# The fix is to make the outcome impossible to depend on: in the offline suite
# `_try_connect()` always reports "not running", whatever is on the box. A test
# that needs a connected Resolve now fails identically everywhere instead of
# passing on one machine and failing on another.
#
# The patch target is deliberate, and one layer lower than it looks like it
# should be. `get_resolve()` reaches its helper as a module global:
#
#     if _try_connect(): return resolve
#
# so replacing the *module attribute* is honoured no matter how many domain
# modules hold their own `from ... import get_resolve` binding — the trap that
# makes patching `get_resolve` itself unreliable (#138/#139). One patch here
# covers every binding; a patch on `get_resolve` covers only one module.
#
# Not stubbed: `_launch_resolve`. `DAVINCI_MCP_NO_AUTOLAUNCH=1` above already
# stops `get_resolve()` from ever reaching it, and the two launch-path tests
# (`test_spawn_env_sanitization`) call it directly on purpose with `Popen`
# patched — replacing it would break the tests that cover the spawn.
#
# Tests deliberately exercising the disconnected path (the disconnected-smoke
# suites) are unaffected: "not running" is precisely what they want, and now
# they get it on a developer box too.
_LIVE_CONNECTION_MODULES = ("src.core.live_connection", "src.granular.common")


def _install_connection_guard() -> None:
    """Pin both connection bootstraps to "Resolve is not running".

    Session-scoped and never undone: nothing in an *offline* suite may connect,
    so there is no window in which the real helper should be reachable. Tests
    that patch `_try_connect` themselves still work — ``mock.patch`` restores
    this stub, not the real function.
    """
    import importlib

    def _not_running():
        """No-op stub installed by tests/conftest.py (#224)."""
        return None

    for module_name in _LIVE_CONNECTION_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            # An import failure here is not this guard's business to report —
            # the tests that need the module fail on their own terms.
            continue
        if hasattr(module, "_try_connect"):
            module._try_connect = _not_running
            module.resolve = None


_install_connection_guard()
