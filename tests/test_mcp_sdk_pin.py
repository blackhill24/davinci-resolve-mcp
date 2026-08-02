"""The MCP SDK upper bound must be present at every install site.

MCP SDK 2.0.0 removed `mcp.server.fastmcp`, which `src/server.py` imports at
module scope. An uncapped `pip install "mcp[cli]"` therefore resolves to 2.x and
kills every entry point at import — surfacing to users as an unexplained
"Server disconnected" rather than anything naming the SDK.

This was live: the cap was missing from install.py AND both workflows, so a
fresh clone was broken and the publish workflow would have failed at the offline
suite. CI caught it; nothing else did, because the repo's own venv already had a
1.x resolved months earlier and stayed green.

The guard reads string literals rather than running pip, so it costs nothing and
works offline. It exists because the fix has three sites and the failure mode is
silent — dropping the cap from one of them would otherwise go unnoticed until
someone provisioned a fresh environment.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Any requirement string that names mcp[cli] must carry an upper bound. Matches
# the requirement as it appears in a shell command or a Python literal.
_MCP_REQ = re.compile(r"""mcp\[cli\][^"'\s]*""")


def _requirements_in(text: str):
    return _MCP_REQ.findall(text)


class McpSdkPinTest(unittest.TestCase):
    def test_install_py_defines_a_capped_requirement(self):
        src = (ROOT / "install.py").read_text(encoding="utf-8")
        m = re.search(r'MCP_SDK_REQUIREMENT\s*=\s*"([^"]+)"', src)
        self.assertIsNotNone(m, "install.py must define MCP_SDK_REQUIREMENT in one place")
        self.assertIn("<2", m.group(1), f"MCP_SDK_REQUIREMENT must cap below 2.0: {m.group(1)!r}")

    def test_install_py_installs_the_pinned_constant_not_a_bare_name(self):
        src = (ROOT / "install.py").read_text(encoding="utf-8")
        self.assertIn(
            '"install", "-q", MCP_SDK_REQUIREMENT',
            src,
            "install.py must pip-install MCP_SDK_REQUIREMENT, not a bare 'mcp[cli]'",
        )

    def test_every_workflow_install_is_capped(self):
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows, "expected at least one workflow")
        checked = 0
        for wf in workflows:
            for line in wf.read_text(encoding="utf-8").splitlines():
                if "pip install" not in line:
                    continue
                for req in _requirements_in(line):
                    checked += 1
                    self.assertIn(
                        "<2", req,
                        f"{wf.name}: uncapped MCP SDK requirement {req!r} — SDK 2.0 removed "
                        "mcp.server.fastmcp and breaks every entry point at import",
                    )
        self.assertGreater(checked, 0, "no workflow pip-installs the MCP SDK — did a site move?")

    def test_the_installed_sdk_actually_exposes_the_imported_module(self):
        # The pin exists to protect exactly one import. Assert the real
        # environment satisfies it, so a bad resolve fails here with a message
        # naming the SDK rather than as 30-odd unrelated collection errors.
        try:
            import mcp.server.fastmcp  # noqa: F401
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
            self.fail(
                f"mcp.server.fastmcp is missing ({exc}). The installed MCP SDK is likely 2.x; "
                "reinstall with the capped requirement (see install.py's MCP_SDK_REQUIREMENT)."
            )


if __name__ == "__main__":
    unittest.main()
