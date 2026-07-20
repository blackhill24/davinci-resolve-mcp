"""Disconnected-Resolve smoke over the compound tool surface (src/server.py).

Every compound tool must return a structured envelope — never raise — when
called with no Resolve connection, both for a real action ("setup"-style probes
excepted, we use an unknown action) and for its unknown-action error path.
"""
import asyncio
import inspect
import unittest
from unittest import mock

import src.server as s


def _compound_tools():
    tools = s.mcp._tool_manager._tools
    assert len(tools) >= 30, "compound registry unexpectedly small"
    return tools


class CompoundDisconnectedSmokeTest(unittest.TestCase):
    def test_every_tool_survives_disconnected_resolve(self):
        failures = []
        with mock.patch.object(s, "get_resolve", return_value=None):
            for name, tool in sorted(_compound_tools().items()):
                fn = tool.fn
                params = inspect.signature(fn).parameters
                try:
                    if "action" in params:
                        out = fn(action="__no_such_action__", params={})
                    else:
                        out = fn()
                    if inspect.iscoroutine(out):
                        out = asyncio.run(out)
                except Exception as exc:  # noqa: BLE001 — the point of the smoke
                    failures.append(f"{name}: raised {type(exc).__name__}: {exc}")
                    continue
                if not isinstance(out, (dict, list, str)):
                    failures.append(f"{name}: non-structured return {type(out).__name__}")
        self.assertEqual(failures, [],
                         "Compound tools crashed when Resolve is disconnected:\n"
                         + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
