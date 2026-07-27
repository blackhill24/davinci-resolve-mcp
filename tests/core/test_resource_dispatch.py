"""#143 finding 6: MCP resource handlers must not run inline on the event loop.

`_install_threaded_tool_dispatch` walks `_tool_manager._tools` only. Resources
are registered on a different manager, so `FunctionResource.read()` called
`self.fn()` inline on the single asyncio thread, with no `_bridge_lock`:

- a resource that touches Resolve froze the whole server — stdio read loop
  included — for the duration, stalling unrelated in-flight tool calls;
- it could enter the non-reentrant Resolve bridge while a tool body was already
  inside it, the hang mode documented in src/core/resolve_busy.py;
- and because hosts read resources *passively*, `get_resolve()` would
  auto-launch Resolve and block ~60s on a probe the user never initiated.
"""

from __future__ import annotations

import asyncio
import threading
import unittest

from mcp.server.fastmcp import FastMCP

from src import server
from src.core.resolve_autolaunch import autolaunch_suppressed, passive_resolve_probe


class ThreadedResourceDispatchTest(unittest.TestCase):
    def _fastmcp_with_resources(self):
        app = FastMCP("test")
        seen = {}

        @app.resource("status://plain")
        def plain():
            seen["thread"] = threading.current_thread()
            seen["bridge_held"] = server._bridge_lock.locked()
            seen["passive"] = autolaunch_suppressed()
            return {"ok": True}

        @app.resource("status://templated/{key}")
        def templated(key: str):
            seen["template_key"] = key
            seen["template_passive"] = autolaunch_suppressed()
            return {"key": key}

        return app, seen

    def test_resources_and_templates_are_both_wrapped(self):
        app, _seen = self._fastmcp_with_resources()
        wrapped = server._install_threaded_resource_dispatch(app)
        self.assertEqual(2, wrapped, "both the resource and the template must be wrapped")

    def test_wrapping_is_idempotent(self):
        app, _seen = self._fastmcp_with_resources()
        server._install_threaded_resource_dispatch(app)
        # A second pass must not double-wrap (each layer would take _bridge_lock
        # again, and it is a plain non-reentrant Lock — that would deadlock).
        self.assertEqual(0, server._install_threaded_resource_dispatch(app))

    def test_a_resource_runs_off_the_event_loop_under_the_bridge_lock(self):
        app, seen = self._fastmcp_with_resources()
        server._install_threaded_resource_dispatch(app)
        resource = app._resource_manager._resources["status://plain"]

        async def drive():
            main = threading.current_thread()
            payload = await resource.read()
            return main, payload

        main_thread, payload = asyncio.run(drive())
        self.assertIn('"ok": true', payload)
        self.assertIsNot(seen["thread"], main_thread,
                         "handler must run on a worker thread, not the event loop")
        self.assertTrue(seen["bridge_held"], "handler must hold _bridge_lock")
        self.assertFalse(server._bridge_lock.locked(), "lock must be released after")

    def test_a_resource_read_suppresses_resolve_autolaunch(self):
        app, seen = self._fastmcp_with_resources()
        server._install_threaded_resource_dispatch(app)
        resource = app._resource_manager._resources["status://plain"]
        asyncio.run(resource.read())
        self.assertTrue(seen["passive"],
                        "a passive host poll must not be allowed to launch Resolve")
        self.assertFalse(autolaunch_suppressed(),
                         "the flag must not leak past the handler")

    def test_a_template_resource_is_offloaded_and_keeps_its_params(self):
        app, seen = self._fastmcp_with_resources()
        server._install_threaded_resource_dispatch(app)
        template = app._resource_manager._templates["status://templated/{key}"]

        async def drive():
            created = await template.create_resource(
                "status://templated/abc", {"key": "abc"})
            return await created.read()

        payload = asyncio.run(drive())
        self.assertIn('"key": "abc"', payload)
        self.assertEqual("abc", seen["template_key"])
        self.assertTrue(seen["template_passive"])

    def test_an_async_resource_is_left_alone(self):
        app = FastMCP("test")

        @app.resource("status://already-async")
        async def already_async():
            return {"ok": True}

        self.assertEqual(0, server._install_threaded_resource_dispatch(app))

    def test_a_missing_resource_manager_is_not_fatal(self):
        class _NoManager:
            pass

        self.assertEqual(0, server._install_threaded_resource_dispatch(_NoManager()))


class PassiveProbeFlagTest(unittest.TestCase):
    def test_the_flag_nests_and_restores(self):
        self.assertFalse(autolaunch_suppressed())
        with passive_resolve_probe():
            self.assertTrue(autolaunch_suppressed())
            with passive_resolve_probe():
                self.assertTrue(autolaunch_suppressed())
            self.assertTrue(autolaunch_suppressed(), "inner exit must not clear the outer")
        self.assertFalse(autolaunch_suppressed())

    def test_the_flag_is_per_thread(self):
        observed = {}

        def worker():
            observed["other_thread"] = autolaunch_suppressed()

        with passive_resolve_probe():
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
        self.assertFalse(observed["other_thread"],
                         "suppression must not leak into unrelated threads")

    def test_get_resolve_does_not_launch_inside_a_passive_probe(self):
        from src.core import live_connection

        launched = []
        original_try = live_connection._try_connect
        original_launch = live_connection._launch_resolve
        original_handle = live_connection.resolve
        # Another test in the same process may have left a cached handle, which
        # would short-circuit get_resolve() before it ever reaches the launch.
        live_connection.resolve = None
        live_connection._try_connect = lambda: None
        live_connection._launch_resolve = lambda: launched.append(True)
        try:
            with passive_resolve_probe():
                self.assertIsNone(live_connection.get_resolve())
            self.assertEqual([], launched, "a passive probe must never launch Resolve")
        finally:
            live_connection._try_connect = original_try
            live_connection._launch_resolve = original_launch
            live_connection.resolve = original_handle


if __name__ == "__main__":
    unittest.main()
