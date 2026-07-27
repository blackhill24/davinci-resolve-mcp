"""Caller-supplied tool params that fail as an envelope, not a traceback.

Compound tool bodies read required params as ``p["clip_id"]``. An AST sweep found
88+ such reads with no ``"clip_id" in p`` check, no ``p.get(...)``, no
``_validate_params`` rule naming them and no enclosing ``try`` (#142 finding 3),
concentrated in ``media_pool_ingest``, ``color_grade``, ``fusion_composition``,
``render_deliver`` and ``timeline_edit``. There is no global exception wrapper on
tool bodies — ``_install_threaded_tool_dispatch`` only offloads to a worker
thread — so an ordinary caller mistake surfaced as a raw ``KeyError`` traceback
instead of the ``invalid_input`` envelope the rest of the codebase maintains.
Concretely: ``timeline_item_takes(action="get_by_index")`` with no ``index``, or
``timeline_item_color(action="copy_grades")`` with no ``target_ids``.

Fixing 88 read sites one at a time would be a large, risky diff that the next
new tool re-opens anyway. Instead the *dict* knows it holds caller input:
:func:`tool_params` wraps it so a missing key raises :class:`MissingParam`, and
:func:`missing_param_envelope` turns that into the standard envelope at the tool
boundary.

The distinction matters: only reads against the caller's params dict convert. A
``KeyError`` from any internal dict is a genuine bug and still propagates, so
this cannot mask one as "bad input".
"""
from __future__ import annotations

import functools
from typing import Any, Dict, Mapping, Optional

from src.core.envelope import _err


class MissingParam(KeyError):
    """A required caller-supplied param was not provided.

    Subclasses KeyError so any pre-existing ``except KeyError`` around a param
    read keeps working exactly as before.
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:  # pragma: no cover - repr detail
        return str(self.key)


class ToolParams(Dict[str, Any]):
    """The caller's params dict; a missing key is a MissingParam."""

    def __missing__(self, key):
        raise MissingParam(key)


def tool_params(params: Optional[Mapping[str, Any]]) -> ToolParams:
    """Normalize a tool's ``params`` argument (``None`` -> empty)."""
    if params is None:
        return ToolParams()
    return ToolParams(params)


def missing_param_error(key: str) -> Dict[str, Any]:
    return _err(
        f"'{key}' is required",
        code="MISSING_PARAM",
        category="invalid_input",
        remediation=f"Pass {key!r} in params.",
        state={"missing_param": key},
    )


def missing_param_envelope(fn):
    """Turn a MissingParam raised inside ``fn`` into an invalid_input envelope.

    Apply as the INNERMOST decorator on a compound tool, below @mcp.tool and any
    governance decorator, so the envelope it returns still passes through those
    layers like any other return value.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except MissingParam as exc:
            return missing_param_error(exc.key)

    return wrapper
