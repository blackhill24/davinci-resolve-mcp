"""Dashboard handler helpers (#104 findings 5 + 10).

5.  `body.get(k) or prefs[k]` silently discarded an explicit 0 sent by the
    panel, so a frame_floor of 0 could not be expressed at all.
10. `asyncio.run()` per request raised "cannot be called from a running event
    loop" whenever a handler path ran on a thread that already owned a loop.

No Resolve required.
"""

from __future__ import annotations

import asyncio
import threading
import unittest

from src.dashboard.handler import _first_specified, _run_tool_coro


class FirstSpecified(unittest.TestCase):
    def test_explicit_zero_beats_the_saved_preference(self) -> None:
        # The whole point: `0 or 24` is 24, which is the bug.
        self.assertEqual(_first_specified(0, 24), 0)

    def test_none_falls_through_to_the_preference(self) -> None:
        self.assertEqual(_first_specified(None, 24), 24)

    def test_falls_through_a_whole_chain_of_nones(self) -> None:
        self.assertEqual(_first_specified(None, None, 12), 12)

    def test_all_none_is_none(self) -> None:
        self.assertIsNone(_first_specified(None, None))

    def test_other_falsy_values_are_also_respected(self) -> None:
        self.assertEqual(_first_specified(False, True), False)
        self.assertEqual(_first_specified("", "fallback"), "")


class RunToolCoro(unittest.TestCase):
    def test_runs_a_coroutine_off_the_event_loop(self) -> None:
        async def work() -> str:
            await asyncio.sleep(0)
            return "done"

        self.assertEqual(_run_tool_coro(work()), "done")

    def test_propagates_exceptions(self) -> None:
        async def boom() -> None:
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            _run_tool_coro(boom())

    def test_works_when_the_calling_thread_already_owns_a_loop(self) -> None:
        """The failure mode the bare asyncio.run() had.

        Calling asyncio.run() from inside a running loop raises RuntimeError;
        the helper must detect that and hand the coroutine to a worker thread.
        """

        async def work() -> str:
            return "done"

        async def driver() -> str:
            # Synchronous call from inside a live loop, exactly as a handler
            # path invoked from async code would do.
            return _run_tool_coro(work())

        self.assertEqual(asyncio.run(driver()), "done")

    def test_bare_asyncio_run_would_have_failed_here(self) -> None:
        """Pin the regression: this is what the old code did."""

        async def work() -> str:
            return "done"

        async def driver() -> None:
            coro = work()
            try:
                with self.assertRaises(RuntimeError):
                    asyncio.run(coro)
            finally:
                coro.close()  # asyncio.run bailed before awaiting it

        asyncio.run(driver())

    def test_usable_from_a_plain_worker_thread(self) -> None:
        results = []

        async def work() -> str:
            return "threaded"

        def run() -> None:
            results.append(_run_tool_coro(work()))

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=10)
        self.assertEqual(results, ["threaded"])


if __name__ == "__main__":
    unittest.main()
