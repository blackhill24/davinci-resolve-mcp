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

import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.bridge_double import (  # noqa: E402
    RESOLVE_EXPORT_CONSTANTS,
    ResolveBridgeDouble,
    make_resolve,
)


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
