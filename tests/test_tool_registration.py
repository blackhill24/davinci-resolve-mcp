"""Regression guard for the MCP tool registry in ``src/server.py``.

A misplaced ``@mcp.tool()`` decorator is silent: in v2.33.0 (commit 32be0ec) a
new ``_parse_pos`` helper was inserted between the decorator and
``def fusion_comp``. The decorator landed on the private helper, so
``_parse_pos`` was exposed as a tool while every ``fusion_comp`` node-graph
operation vanished from the tool list. It shipped undetected because nothing
asserted the registered tool set (reported + fixed in PR #57, v2.36.1).

These tests run fully offline — importing ``src.server`` does not connect to
Resolve, and enumerating tools via ``mcp.list_tools()`` touches no live handle.
"""
import asyncio
import unittest

from tests.test_import import ALL_COMPOUND_TOOL_FILES, _count_mcp_tools


def _registered_tool_names():
    import src.server as server

    tools = asyncio.run(server.mcp.list_tools())
    return sorted(t.name for t in tools)


# Stable, public compound tools that must always be registered. Not exhaustive —
# a representative spread so an accidental mass-unregister fails loudly.
KNOWN_GOOD_TOOLS = {
    "fusion_comp",  # the exact tool the 32be0ec regression unregistered
    "timeline",
    "timeline_item",
    "media_pool",
    "media_pool_item",
    "render",
    "project_manager",
    "resolve_control",
    "timeline_versioning",
}

# Floor for the registered tool count. Lives below the current count (33) so
# adding tools never breaks this; a large accidental drop trips it.
MIN_TOOL_COUNT = 33


class ToolRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.names = _registered_tool_names()

    def test_no_private_helpers_registered_as_tools(self):
        """No registered tool name starts with ``_`` — directly catches the
        ``_parse_pos`` regression class (a private helper grabbing the
        decorator)."""
        private = [n for n in self.names if n.startswith("_")]
        self.assertEqual(
            private,
            [],
            f"private helper(s) exposed as MCP tools: {private} — a @mcp.tool() "
            "decorator likely slid onto an internal function",
        )

    def test_known_good_tools_are_registered(self):
        missing = sorted(KNOWN_GOOD_TOOLS - set(self.names))
        self.assertEqual(
            missing,
            [],
            f"expected MCP tool(s) missing from the registry: {missing}",
        )

    def test_tool_count_at_or_above_floor(self):
        self.assertGreaterEqual(
            len(self.names),
            MIN_TOOL_COUNT,
            f"registered tool count {len(self.names)} fell below floor "
            f"{MIN_TOOL_COUNT} — tools were unregistered. Registered: {self.names}",
        )

    def test_no_tool_hides_an_async_body_behind_a_sync_wrapper(self):
        """A sync decorator wrapped around an ``async def`` tool hands FastMCP the
        un-awaited coroutine.

        FastMCP dispatches on the OUTERMOST callable: a plain ``def wrapper`` that
        returns ``fn(*args, **kwargs)`` is not awaited, so the tool's declared
        dict output is a coroutine object and every call dies with
        ``Input should be a valid dictionary [type=dict_type,
        input_value=<coroutine object ...>]`` — the tool is dead for every action.

        This shipped for ``auto_edit`` and ``orchestrate``: both are ``async def``
        and both were decorated with the then sync-only
        ``core.params.missing_param_envelope``. Nothing caught it, because the
        tests call the tool function directly and ``await`` whatever comes back,
        which papers over exactly this mismatch. Assert against the decorator
        chain instead: if any layer under a tool is a coroutine function, the
        outermost callable must be one too.
        """
        import inspect

        import src.server as server

        broken = []
        for tool in server.mcp._tool_manager.list_tools():
            name, fn = tool.name, tool.fn
            inner, seen = fn, set()
            while hasattr(inner, "__wrapped__") and id(inner) not in seen:
                seen.add(id(inner))
                inner = inner.__wrapped__
                if inspect.iscoroutinefunction(inner) and not inspect.iscoroutinefunction(fn):
                    broken.append(f"{name} (async {inner.__qualname__} behind sync wrapper)")
                    break
        self.assertEqual(
            broken,
            [],
            "tool(s) expose a sync wrapper over an async body — FastMCP will "
            f"return the un-awaited coroutine and every call fails: {broken}",
        )

    def test_runtime_registry_matches_source_decorators(self):
        """The number of tools the server registers at runtime must equal the
        number of ``@mcp.tool()`` decorators in the source. A mismatch means a
        decorator is decorating the wrong thing (or not taking effect)."""
        static_count = sum(_count_mcp_tools(p) for p in ALL_COMPOUND_TOOL_FILES)
        self.assertEqual(
            len(self.names),
            static_count,
            f"runtime registered tools ({len(self.names)}) != @mcp.tool() "
            f"decorators in src/server.py ({static_count})",
        )


if __name__ == "__main__":
    unittest.main()
