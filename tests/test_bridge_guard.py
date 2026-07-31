"""Async tool bodies must not enter the Resolve bridge unserialized.

`_install_threaded_tool_dispatch` wraps **sync** tool bodies only — it skips any
tool already `is_async` — so `auto_edit`, `media_analysis` and `orchestrate` ran
their Resolve work with no `_bridge_lock` and inline on the asyncio thread
(#167 finding 2). Two consequences:

* a sync tool body (worker thread, holding the lock) or a `background_jobs`
  worker (which holds the same lock by design) running concurrently with one of
  these puts two threads inside fusionscript, which per `core/resolve_busy`
  "simply hangs with no feedback";
* every Resolve call blocked the stdio read loop, the exact thing the offload
  wrapper exists to prevent.

`bridge_guard()` closes both. These tests observe the two properties that make
it correct rather than asserting it was called:

1. **Mutual exclusion** — a guarded async body and a lock-holding worker thread
   are never inside the bridge at the same moment.
2. **Deadlock freedom under nesting** — `orchestrate` delegates to `auto_edit`
   and both are guarded, so the inner guard must not block on the lock its own
   caller holds. Re-entrancy is per-task via a ContextVar, because the acquire
   happens in a worker thread and the release on the event loop (an RLock would
   forbid that cross-thread release, which is why the lock stays a plain Lock).
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest

from src.core.live_connection import _bridge_held, _bridge_lock, bridge_guard, bridge_release, bridge_serialized


class BridgeGuardMutualExclusionTest(unittest.TestCase):
    def test_a_guarded_body_and_a_lock_holder_never_overlap(self):
        # `inside` stands in for fusionscript: the bug is two entrants at once,
        # so the assertion is on the peak count, not on call ordering.
        inside = 0
        peak = 0
        counter_lock = threading.Lock()

        def enter():
            nonlocal inside, peak
            with counter_lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.02)
            with counter_lock:
                inside -= 1

        def sync_tool_body():
            # Exactly what _install_threaded_tool_dispatch's wrapper does, and
            # what a background_jobs worker does.
            with _bridge_lock:
                enter()

        async def guarded_async_tool():
            async with bridge_guard():
                enter()

        async def drive():
            threads = [threading.Thread(target=sync_tool_body) for _ in range(4)]
            for thread in threads:
                thread.start()
            await asyncio.gather(*(guarded_async_tool() for _ in range(4)))
            for thread in threads:
                thread.join()

        asyncio.run(drive())
        self.assertEqual(1, peak, "two callers were inside the bridge at once")

    def test_the_lock_is_released_even_when_the_body_raises(self):
        async def boom():
            async with bridge_guard():
                raise RuntimeError("body failed")

        with self.assertRaises(RuntimeError):
            asyncio.run(boom())
        self.assertTrue(_bridge_lock.acquire(blocking=False),
                        "the bridge lock was not released after an exception")
        _bridge_lock.release()

    def test_the_acquire_does_not_block_the_event_loop(self):
        # The acquire must happen off-thread: while another holder drains the
        # bridge, the loop has to keep servicing other work (the stdio read loop
        # in production). If it were acquired inline, the ticker below would not
        # advance while the guard waited.
        ticks = 0
        released = threading.Event()

        def hold_then_release():
            with _bridge_lock:
                released.wait(1.0)

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                ticks += 1
                await asyncio.sleep(0.005)

        async def waiter():
            async with bridge_guard():
                pass

        async def drive():
            holder = threading.Thread(target=hold_then_release)
            holder.start()
            time.sleep(0.02)  # let the holder take the lock first
            task = asyncio.ensure_future(waiter())
            await ticker()
            released.set()
            await task
            holder.join()

        asyncio.run(drive())
        self.assertGreater(ticks, 10,
                           "the event loop stalled while the guard waited for the bridge")


class BridgeGuardReentrancyTest(unittest.TestCase):
    def test_a_nested_guard_does_not_deadlock(self):
        # orchestrate -> auto_edit: both guarded, one task.
        @bridge_serialized
        async def inner(action, params=None):
            return {"inner": action}

        @bridge_serialized
        async def outer(action, params=None):
            return {"outer": await inner("nested")}

        async def drive():
            return await asyncio.wait_for(outer("top"), timeout=5.0)

        self.assertEqual({"outer": {"inner": "nested"}}, asyncio.run(drive()))

    def test_the_flag_is_cleared_after_the_outermost_guard(self):
        async def drive():
            async with bridge_guard():
                self.assertTrue(_bridge_held.get())
            return _bridge_held.get()

        self.assertFalse(asyncio.run(drive()),
                         "the held flag outlived the guard, so a later call would skip locking")

    def test_a_nested_guard_leaves_the_lock_held_until_the_outer_one_exits(self):
        async def drive():
            async with bridge_guard():
                async with bridge_guard():
                    pass
                # The inner guard must NOT have released the shared lock.
                return _bridge_lock.acquire(blocking=False)

        self.assertFalse(asyncio.run(drive()),
                         "the inner guard released the outer guard's lock")


class BridgeReleaseTest(unittest.TestCase):
    """`bridge_release()` — media_analysis's carve-out for ffmpeg/Whisper passes
    nested inside an otherwise-guarded body (#167 finding 2, second half:
    wrapping the whole tool like auto_edit/orchestrate would hold the bridge
    hostage for the duration of work that touches no Resolve API at all).
    """

    def test_another_holder_can_enter_while_released(self):
        # The whole point: a long non-Resolve pass inside a guarded body must
        # not block a concurrent sync tool body / background_jobs worker from
        # draining the bridge.
        entered = threading.Event()

        def other_holder():
            entered.wait(1.0)
            with _bridge_lock:
                pass

        async def drive():
            async with bridge_guard():
                async with bridge_release():
                    other = threading.Thread(target=other_holder)
                    other.start()
                    entered.set()
                    other.join(1.0)
                    self.assertFalse(other.is_alive(),
                                     "another holder could not acquire the bridge while released")

        asyncio.run(drive())

    def test_the_lock_is_held_again_after_the_release_block_exits(self):
        async def drive():
            async with bridge_guard():
                async with bridge_release():
                    pass
                # bridge_release() must have re-acquired before returning control.
                return _bridge_lock.acquire(blocking=False)

        self.assertFalse(asyncio.run(drive()),
                         "the lock was not held again after bridge_release() exited")

    def test_reacquires_even_if_the_released_body_raises(self):
        async def drive():
            with self.assertRaises(RuntimeError):
                async with bridge_guard():
                    async with bridge_release():
                        raise RuntimeError("long non-Resolve work failed")
            # Guard's own finally already released by now; the lock must be free.
            return _bridge_lock.acquire(blocking=False)

        self.assertTrue(asyncio.run(drive()),
                        "the lock leaked after an exception inside bridge_release()")
        _bridge_lock.release()

    def test_a_noop_outside_a_guard(self):
        # _publish_clip_metadata_from_analysis is called both from within a
        # guarded dispatch and, in principle, could run standalone — the
        # release must not explode or touch a lock it doesn't hold.
        async def drive():
            async with bridge_release():
                pass
            return True

        self.assertTrue(asyncio.run(drive()))
        self.assertTrue(_bridge_lock.acquire(blocking=False),
                        "a standalone bridge_release() touched the shared lock")
        _bridge_lock.release()

    def test_mutual_exclusion_still_holds_around_the_release_window(self):
        # Resolve reads before the release and Resolve writes after it must
        # still be serialized against a concurrent sync tool body.
        inside = 0
        peak = 0
        counter_lock = threading.Lock()

        def enter():
            nonlocal inside, peak
            with counter_lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.02)
            with counter_lock:
                inside -= 1

        def sync_tool_body():
            with _bridge_lock:
                enter()

        async def guarded_with_release():
            async with bridge_guard():
                enter()  # "Resolve read"
                async with bridge_release():
                    await asyncio.sleep(0.02)  # "ffmpeg/Whisper pass"
                enter()  # "Resolve write"

        async def drive():
            threads = [threading.Thread(target=sync_tool_body) for _ in range(4)]
            for thread in threads:
                thread.start()
            await asyncio.gather(*(guarded_with_release() for _ in range(4)))
            for thread in threads:
                thread.join()

        asyncio.run(drive())
        self.assertEqual(1, peak, "two callers were inside the bridge at once")


class BridgeSerializedDecoratorTest(unittest.TestCase):
    def test_the_decorated_tools_are_marked_and_keep_their_signature(self):
        # FastMCP builds each tool's schema from the wrapped signature, so the
        # decorator must be transparent to inspect.signature.
        import inspect

        import src.server  # noqa: F401 — import order: server first, domains resolve through it
        from src.domains.auto_edit.actions import auto_edit
        from src.domains.orchestration.actions import orchestrate

        for tool in (auto_edit, orchestrate):
            with self.subTest(tool=tool.__name__):
                self.assertTrue(getattr(tool, "__bridge_serialized__", False),
                                f"{tool.__name__} is not bridge-serialized")
                params = inspect.signature(tool).parameters
                self.assertIn("action", params)
                self.assertIn("params", params)


if __name__ == "__main__":
    unittest.main()
