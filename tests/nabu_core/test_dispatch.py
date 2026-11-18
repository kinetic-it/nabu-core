import asyncio
import unittest

from nabu_core.dispatch import SerialDispatcher, TargetDenied


class Fake:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.started = []
        self.stopped = []
        self.event = asyncio.Event()

    async def play(self, target):
        self.started.append(target)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await self.event.wait()
        finally:
            self.active -= 1

    async def stop(self, target):
        self.stopped.append(target)
        return False


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_serial_and_stop_cancels_pending(self):
        p = Fake()
        d = SerialDispatcher(p, allowed_targets={"safe"}, max_seconds=1)
        a = asyncio.create_task(d.ring("safe"))
        await asyncio.sleep(0.01)
        b = asyncio.create_task(d.ring("safe"))
        await asyncio.sleep(0.01)
        self.assertEqual(p.maximum, 1)
        self.assertFalse(await d.stop())  # false = device silence unconfirmed
        self.assertEqual((await a).status, "stopped")
        self.assertEqual((await b).status, "cancelled")
        self.assertEqual(len(p.started), 1)
        self.assertEqual(p.stopped, ["safe"])

    async def test_timeout_releases_channel(self):
        p = Fake()
        d = SerialDispatcher(p, allowed_targets={"safe"}, max_seconds=0.01)
        self.assertEqual((await d.ring("safe")).status, "timeout")
        p.event.set()
        self.assertEqual((await d.ring("safe")).status, "completed")
        self.assertEqual(p.maximum, 1)

    async def test_default_deny(self):
        p = Fake()
        d = SerialDispatcher(p, allowed_targets=set(), max_seconds=1)
        with self.assertRaises(TargetDenied):
            await d.ring("kitchen")
        self.assertEqual(p.started, [])

    async def test_player_failure_releases_channel(self):
        p = Fake()

        async def fail(target):
            raise ConnectionError("offline")

        p.play = fail
        d = SerialDispatcher(p, allowed_targets={"safe"}, max_seconds=1)
        self.assertEqual((await d.ring("safe")).status, "failed")
        self.assertEqual((await d.ring("safe")).status, "failed")

    async def test_new_alarm_waits_for_prior_stop_ack(self):
        p = Fake()
        stop_started, release_stop = asyncio.Event(), asyncio.Event()

        async def slow_stop(target):
            stop_started.set()
            await release_stop.wait()
            return False

        p.stop = slow_stop
        d = SerialDispatcher(p, allowed_targets={"safe"}, max_seconds=2)
        old = asyncio.create_task(d.ring("safe"))
        await asyncio.sleep(0.01)
        stopping = asyncio.create_task(d.stop())
        await stop_started.wait()
        new = asyncio.create_task(d.ring("safe"))
        await asyncio.sleep(0.01)
        self.assertEqual(len(p.started), 1)
        release_stop.set()
        await stopping
        await old
        p.event.set()
        self.assertEqual((await new).status, "completed")

    async def test_uncooperative_player_does_not_stall_stop_or_restart(self):
        p = Fake()

        async def uncooperative(target):
            p.started.append(target)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await p.event.wait()

        p.play = uncooperative
        d = SerialDispatcher(p, allowed_targets={"safe"}, max_seconds=0.1)
        old = asyncio.create_task(d.ring("safe"))
        await asyncio.sleep(0.01)
        self.assertFalse(await asyncio.wait_for(d.stop(), 0.5))
        self.assertEqual((await old).status, "stopped")
        self.assertEqual((await d.ring("safe")).status, "unsettled")
        p.event.set()
        await asyncio.sleep(0.01)
        self.assertEqual(len(p.started), 1)

    async def test_cancellation_requests_a_bounded_player_stop(self):
        p = Fake()

        async def never(target):
            p.started.append(target)
            await asyncio.Event().wait()

        p.play = never
        stopped = asyncio.Event()

        async def stop(target):
            p.stopped.append(target)
            stopped.set()
            return False

        p.stop = stop
        d = SerialDispatcher(p, allowed_targets={"safe"}, max_seconds=5)
        running = asyncio.create_task(d.ring("safe"))
        await asyncio.sleep(0.01)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await asyncio.wait_for(stopped.wait(), 1)
        self.assertEqual(p.stopped, ["safe"])

    async def test_double_cancellation_still_requests_exactly_one_stop(self):
        p = Fake()

        async def never(target):
            p.started.append(target)
            await asyncio.Event().wait()

        p.play = never

        async def stop(target):
            p.stopped.append(target)
            return False

        p.stop = stop
        d = SerialDispatcher(p, allowed_targets={"safe"}, max_seconds=5)
        running = asyncio.create_task(d.ring("safe"))
        await asyncio.sleep(0.01)
        running.cancel()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        await asyncio.sleep(0.05)
        self.assertEqual(p.stopped, ["safe"])
