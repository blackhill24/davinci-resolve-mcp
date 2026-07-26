"""The one faithful double for `BlackmagicFusion.PyRemoteObject`.

`unittest.mock.MagicMock` is an **unfaithful** stand-in for the Resolve bridge, and
that unfaithfulness is the root cause class behind #119: production code probes the
bridge with `_has_method()` / `_api_constant()`, a `MagicMock` answers those probes
the wrong way round, so the test drives the *capability-missing* fallback branch and
asserts on the resulting error envelope — never executing the code under test.

The real bridge, verified live on Resolve Studio 21.0.2.4, has four load-bearing
behaviours:

| behaviour                        | real bridge                              |
|----------------------------------|------------------------------------------|
| ``dir(obj)``                     | real **methods** only, **no** constants  |
| ``getattr(obj, 'MadeUpName')``   | ``None`` — fabricated, never raises      |
| ``hasattr(obj, anything)``       | always ``True`` (so it is useless)       |
| ``getattr(obj, 'EXPORT_DRT')``   | ``1.0`` — real, and absent from ``dir()``|

`ResolveBridgeDouble` reproduces all four. `tests/core/test_bridge_double_fidelity.py`
is the meta-test that pins them, so the double itself cannot silently drift.

A `MagicMock` reproduces none of them, and its failure mode is quiet: `dir()` on a
mock lists only the children a test has *touched*, so any method the test did not
explicitly configure reads as absent to `_has_method`, the capability gate closes,
and production code takes its fallback branch. The test then asserts on the
fallback's output — which is usually plausible — and passes without ever executing
the path it was written for.

Usage
-----

    from tests.bridge_double import ResolveBridgeDouble, calls_of

    resolve = ResolveBridgeDouble(
        methods={"GetProjectManager": lambda: pm, "OpenPage": True},
        constants={"EXPORT_DRT": 1.0, "EXPORT_NONE": 0.0},
    )
    resolve.OpenPage("color")             # -> True
    calls_of(resolve)                     # -> [("OpenPage", ("color",), {})]

A *method* entry whose value is callable is invoked with the call's arguments; any
other value is returned verbatim. A name in neither mapping fabricates ``None``,
exactly like the bridge — which is why `_has_method` must test ``dir()``.

Internal state lives under the ``_rbd_`` prefix so it cannot collide with a Resolve
API name; use the module-level helpers (`calls_of`, `reset_calls`) rather than
touching those attributes.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "ResolveBridgeDouble",
    "calls_of",
    "call_names",
    "reset_calls",
    "RESOLVE_EXPORT_CONSTANTS",
]

# EXPORT_* constant names a real `resolve` object answers to. The *values* here are
# synthetic distinct floats, NOT Blackmagic's real numbering — nothing in the repo
# may depend on a specific number, only on "a number came back rather than the
# literal name". Two properties are load-bearing and must be preserved:
#   * every value is a non-callable number, so `_api_constant` accepts it;
#   * EXPORT_NONE is 0.0, so a truthiness test (the bug shape that drops a valid
#     zero-valued constant) still fails here.
_EXPORT_NAMES = (
    "EXPORT_AAF", "EXPORT_DRT", "EXPORT_EDL", "EXPORT_FCP_7_XML",
    "EXPORT_FCPXML_1_3", "EXPORT_FCPXML_1_4", "EXPORT_FCPXML_1_5",
    "EXPORT_FCPXML_1_6", "EXPORT_FCPXML_1_7", "EXPORT_FCPXML_1_8",
    "EXPORT_FCPXML_1_9", "EXPORT_FCPXML_1_10",
    "EXPORT_HDR_10_PROFILE_A", "EXPORT_HDR_10_PROFILE_B",
    "EXPORT_TEXT_CSV", "EXPORT_TEXT_TAB",
    "EXPORT_DOLBY_VISION_VER_2_9", "EXPORT_DOLBY_VISION_VER_4_0",
    "EXPORT_DOLBY_VISION_VER_5_1",
    "EXPORT_OTIO", "EXPORT_ALE", "EXPORT_ALE_CDL",
    "EXPORT_AAF_NEW", "EXPORT_AAF_EXISTING",
    "EXPORT_LUT_17PTCUBE", "EXPORT_LUT_33PTCUBE", "EXPORT_LUT_65PTCUBE",
    "EXPORT_LUT_PANASONICVLUT",
)
RESOLVE_EXPORT_CONSTANTS: Dict[str, float] = {"EXPORT_NONE": 0.0}
RESOLVE_EXPORT_CONSTANTS.update(
    {name: float(i + 1) for i, name in enumerate(_EXPORT_NAMES)}
)


class ResolveBridgeDouble:
    """A stand-in for any `PyRemoteObject` — resolve, project, timeline, item, ...

    Parameters
    ----------
    methods:
        Name -> return value, or name -> callable invoked with the call arguments.
        These, and only these, are what ``dir()`` reports.
    constants:
        Name -> value. Reachable via ``getattr`` but deliberately **absent** from
        ``dir()``, mirroring the bridge.
    name:
        Label used in ``repr`` and in assertion messages.
    """

    def __init__(
        self,
        methods: Optional[Mapping[str, Any]] = None,
        constants: Optional[Mapping[str, Any]] = None,
        name: str = "PyRemoteObject",
    ) -> None:
        object.__setattr__(self, "_rbd_methods", dict(methods or {}))
        object.__setattr__(self, "_rbd_constants", dict(constants or {}))
        object.__setattr__(self, "_rbd_name", name)
        object.__setattr__(self, "_rbd_calls", [])

    # -- bridge behaviour ---------------------------------------------------

    def __getattr__(self, attr: str) -> Any:
        # Only reached for names not already on the instance/class, i.e. every
        # Resolve API name. The bridge NEVER raises AttributeError.
        constants = object.__getattribute__(self, "_rbd_constants")
        if attr in constants:
            return constants[attr]

        methods = object.__getattribute__(self, "_rbd_methods")
        if attr in methods:
            spec = methods[attr]
            calls = object.__getattribute__(self, "_rbd_calls")

            def _invoke(*args: Any, **kwargs: Any) -> Any:
                calls.append((attr, args, kwargs))
                return spec(*args, **kwargs) if callable(spec) else spec

            _invoke.__name__ = attr
            return _invoke

        # Fabricated. This is the behaviour that makes hasattr() useless and that
        # forces capability detection through dir().
        return None

    def __dir__(self) -> List[str]:
        # Methods only — never the constants. `_has_method` depends on this.
        return sorted(object.__getattribute__(self, "_rbd_methods"))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ResolveBridgeDouble {object.__getattribute__(self, '_rbd_name')}>"

    # -- test-side helpers (never named like a Resolve API member) -----------

    def _rbd_add_methods(self, methods: Mapping[str, Any]) -> "ResolveBridgeDouble":
        """Extend the method set in place; returns self so it can be chained."""
        object.__getattribute__(self, "_rbd_methods").update(methods)
        return self

    def _rbd_add_constants(self, constants: Mapping[str, Any]) -> "ResolveBridgeDouble":
        object.__getattribute__(self, "_rbd_constants").update(constants)
        return self


def calls_of(double: ResolveBridgeDouble) -> List[Tuple[str, tuple, dict]]:
    """Every recorded call as ``(method_name, args, kwargs)``, in order."""
    return list(object.__getattribute__(double, "_rbd_calls"))


def call_names(double: ResolveBridgeDouble) -> List[str]:
    """Just the method names called, in order."""
    return [c[0] for c in calls_of(double)]


def reset_calls(double: ResolveBridgeDouble) -> None:
    object.__getattribute__(double, "_rbd_calls").clear()


def make_resolve(
    methods: Optional[Mapping[str, Any]] = None,
    constants: Optional[Mapping[str, Any]] = None,
    export_constants: bool = True,
) -> ResolveBridgeDouble:
    """A top-level `resolve` double, pre-loaded with the real EXPORT_* constants.

    `export_constants=False` models an older Resolve that does not ship them.
    """
    consts: Dict[str, Any] = dict(RESOLVE_EXPORT_CONSTANTS) if export_constants else {}
    consts.update(constants or {})
    return ResolveBridgeDouble(methods=methods, constants=consts, name="resolve")


def missing_methods(double: ResolveBridgeDouble, names: Iterable[str]) -> List[str]:
    """Names the double does not expose — use to assert a test's own setup."""
    exposed = set(dir(double))
    return [n for n in names if n not in exposed]


def method_returning(value: Any) -> Callable[..., Any]:
    """Sugar for a method that ignores its arguments and returns `value`."""
    return lambda *a, **k: value
