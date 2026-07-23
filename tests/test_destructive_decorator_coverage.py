"""Enforce that every tool that owns destructive actions is decorated (C6).

If you add a new entry to `DESTRUCTIVE_ACTIONS_BY_TOOL`, the corresponding
top-level tool function in `src/server.py` must be decorated with
`@_destructive_op("<tool_name>")` — otherwise the version-on-mutate hook never
fires for that tool and edits go un-archived.

This test does a static text scan of `server.py` (independent of FastMCP's
runtime behavior) to guarantee the wiring.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from src.core.destructive_hook import DESTRUCTIVE_ACTIONS_BY_TOOL

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER_PATH = ROOT / "src" / "server.py"
# Domain tool functions moved out of server.py in the restructure epic (#52,
# Phase 3 / #46); the decorator wiring now lives across all of these files.
DOMAIN_ACTION_FILES = sorted((ROOT / "src" / "domains").glob("*/actions.py"))


class DestructiveDecoratorCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = "\n".join(
            p.read_text(encoding="utf-8") for p in [SERVER_PATH] + DOMAIN_ACTION_FILES
        )

    def test_every_registered_tool_is_decorated(self) -> None:
        missing: list[str] = []
        for tool_name in sorted(DESTRUCTIVE_ACTIONS_BY_TOOL):
            # Look for either order — @mcp.tool() before @_destructive_op or vice
            # versa — followed by the def line.
            pattern = re.compile(
                rf"@_destructive_op\(\"{re.escape(tool_name)}\"\)\s*\n"
                rf"(?:async\s+)?def\s+{re.escape(tool_name)}\s*\(",
                re.MULTILINE,
            )
            if not pattern.search(self.source):
                missing.append(tool_name)
        self.assertFalse(
            missing,
            msg=(
                "These tools have destructive actions registered but are NOT "
                "decorated with @_destructive_op in src/server.py: "
                f"{missing}. Decorate them so the version-on-mutate hook fires."
            ),
        )

    def test_decorator_pairs_with_mcp_tool(self) -> None:
        """Every `@_destructive_op(X)` should sit between `@mcp.tool()` and `def X`."""
        # Find every destructive_op decoration and its function name.
        pattern = re.compile(
            r"@mcp\.tool\(\)\s*\n@_destructive_op\(\"(?P<tool>[a-z_]+)\"\)\s*\n(?:async\s+)?def\s+(?P<fn>[a-z_]+)",
            re.MULTILINE,
        )
        for match in pattern.finditer(self.source):
            self.assertEqual(
                match.group("tool"), match.group("fn"),
                msg=(
                    f"@_destructive_op(\"{match.group('tool')}\") sits above "
                    f"def {match.group('fn')}(...) — the tool name and function "
                    f"name must match."
                ),
            )

    def test_destructive_op_only_on_registered_tools(self) -> None:
        """Reject `@_destructive_op(X)` for X not in the registry."""
        pattern = re.compile(
            r"@_destructive_op\(\"(?P<tool>[a-z_]+)\"\)",
            re.MULTILINE,
        )
        unregistered: list[str] = []
        for match in pattern.finditer(self.source):
            tool = match.group("tool")
            if tool not in DESTRUCTIVE_ACTIONS_BY_TOOL:
                unregistered.append(tool)
        self.assertFalse(
            unregistered,
            msg=(
                "These tools are decorated but have no entry in "
                f"DESTRUCTIVE_ACTIONS_BY_TOOL: {unregistered}. Either add the "
                "actions to the registry or remove the decorator."
            ),
        )


if __name__ == "__main__":
    unittest.main()
