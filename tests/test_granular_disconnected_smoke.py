"""Disconnected-Resolve smoke test over the ENTIRE granular tool surface.

Contract: with no Resolve connection, every one of the ~341 granular tools must
return a structured result (usually an error) — never raise. This is the
offline half of connector robustness; live probes cover the connected half.

Each tool is called with synthesized minimal arguments and with get_resolve
patched to return None in every granular module. subprocess spawns are blocked
so app-control tools cannot launch/kill a real Resolve during the test.
"""
import importlib
import inspect
import pkgutil
import typing
import unittest
from unittest import mock

import src.granular as granular_pkg
from src.granular import mcp


def _dummy_for(param: inspect.Parameter):
    ann = param.annotation
    origin = typing.get_origin(ann)
    if origin is typing.Union:  # Optional[X] and friends
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        if type(None) in typing.get_args(ann):
            return None
        ann = args[0] if args else str
        origin = typing.get_origin(ann)
    if origin in (list, typing.List):
        return []
    if origin in (dict, typing.Dict):
        return {}
    if ann in (int,):
        return 1
    if ann in (float,):
        return 1.0
    if ann in (bool,):
        return False
    if ann in (list,):
        return []
    if ann in (dict,):
        return {}
    return "x"


def _synth_args(fn):
    kwargs = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        kwargs[name] = _dummy_for(param)
    return kwargs


def _granular_modules():
    mods = []
    for info in pkgutil.iter_modules(granular_pkg.__path__):
        mod = importlib.import_module(f"src.granular.{info.name}")
        mods.append(mod)
    return mods


class GranularDisconnectedSmokeTest(unittest.TestCase):
    def test_every_tool_survives_disconnected_resolve(self):
        tools = mcp._tool_manager._tools
        self.assertGreater(len(tools), 300, "tool registry unexpectedly small")

        patches = [mock.patch("subprocess.Popen", side_effect=AssertionError("spawn blocked")),
                   mock.patch("subprocess.run", side_effect=AssertionError("spawn blocked")),
                   mock.patch("subprocess.call", side_effect=AssertionError("spawn blocked"))]
        for mod in _granular_modules():
            if hasattr(mod, "get_resolve"):
                patches.append(mock.patch.object(mod, "get_resolve", return_value=None))

        failures = []
        with mock.patch.dict("os.environ", {"RESOLVE_MCP_CONFIRM_TOKENS": "0"}, clear=False):
            for p in patches:
                p.start()
            try:
                for name, tool in sorted(tools.items()):
                    fn = tool.fn
                    try:
                        fn(**_synth_args(fn))
                    except AssertionError as exc:
                        failures.append(f"{name}: attempted subprocess spawn ({exc})")
                    except Exception as exc:  # noqa: BLE001 — the point of the smoke
                        failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
            finally:
                for p in patches:
                    p.stop()

        self.assertEqual(
            failures, [],
            "Tools crashed (or spawned processes) when Resolve is disconnected:\n"
            + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
