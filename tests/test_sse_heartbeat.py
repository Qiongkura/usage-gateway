"""Offline asynchronous heartbeat tests with controlled upstream gates."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sse_heartbeat import with_sse_heartbeat  # noqa: E402


class ControlledUpstream:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.started = asyncio.Event()
        self.calls = 0
        self.cancelled = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.calls += 1
        self.started.set()
        try:
            value = await self.queue.get()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if isinstance(value, BaseException):
            raise value
        return value

    async def aclose(self):
        self.closed = True


class HeartbeatTest(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_heartbeats_share_one_pending_read(self):
        source = ControlledUpstream()
        stream = with_sse_heartbeat(source, 0.005)
        source.queue.put_nowait(b': upstream\n\n')
        self.assertEqual(await anext(stream), b': upstream\n\n')
        self.assertEqual(await anext(stream), b': keep-alive\n\n')
        self.assertEqual(await anext(stream), b': keep-alive\n\n')
        self.assertEqual((source.calls, source.cancelled), (2, 0))
        source.queue.put_nowait(b'data: hello\n\n')
        self.assertEqual(await anext(stream), b'data: hello\n\n')
        source.queue.put_nowait(StopAsyncIteration())
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)
        self.assertTrue(source.closed)
        self.assertEqual(source.cancelled, 0)

    async def test_disabled_by_default_and_zero(self):
        for interval in (None, 0):
            source = ControlledUpstream()
            stream = with_sse_heartbeat(source, interval)
            task = asyncio.create_task(anext(stream))
            await source.started.wait()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            source.queue.put_nowait(b'data: x\n\n')
            self.assertEqual(await task, b'data: x\n\n')
            await stream.aclose()
            self.assertTrue(source.closed)

    async def test_no_injection_inside_partial_event_or_crlf(self):
        source = ControlledUpstream()
        stream = with_sse_heartbeat(source, 0.005)
        source.queue.put_nowait(b'data: par')
        self.assertEqual(await anext(stream), b'data: par')
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        source.queue.put_nowait(b't\r\n\r')
        self.assertEqual(await task, b't\r\n\r')
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        source.queue.put_nowait(b'\n')
        self.assertEqual(await task, b'\n')
        self.assertEqual(await anext(stream), b': keep-alive\n\n')
        await stream.aclose()
        self.assertTrue(source.closed)
        self.assertEqual(source.cancelled, 1)

    async def test_initial_wait_preserves_leading_bom(self):
        source = ControlledUpstream()
        stream = with_sse_heartbeat(source, 0.005)
        self.assertEqual(await anext(stream), b'\xef\xbb\xbf: keep-alive\n\n')
        source.queue.put_nowait(b'\xef')
        source.queue.put_nowait(b'\xbb')
        source.queue.put_nowait(b'\xbfdata: x\n\n')
        self.assertEqual(await anext(stream), b'data: x\n\n')
        self.assertEqual(await anext(stream), b': keep-alive\n\n')
        await stream.aclose()
        self.assertTrue(source.closed)

    async def test_exception_propagates_and_closes(self):
        source = ControlledUpstream()
        source.queue.put_nowait(ValueError('upstream failed'))
        stream = with_sse_heartbeat(source, 0.01)
        with self.assertRaisesRegex(ValueError, 'upstream failed'):
            await anext(stream)
        self.assertTrue(source.closed)

    async def test_consumer_cancellation_cancels_read_and_closes(self):
        source = ControlledUpstream()
        stream = with_sse_heartbeat(source)
        task = asyncio.create_task(anext(stream))
        await source.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(source.cancelled, 1)
        self.assertTrue(source.closed)

    async def test_close_suspended_after_heartbeat(self):
        source = ControlledUpstream()
        stream = with_sse_heartbeat(source, 0.005)
        source.queue.put_nowait(b': upstream\n\n')
        await anext(stream)
        await anext(stream)
        await stream.aclose()
        self.assertEqual(source.cancelled, 1)
        self.assertTrue(source.closed)

    async def test_invalid_parameters(self):
        for kwargs in ({'interval': -1}, {'interval': float('nan')},
                       {'interval': float('inf')}, {'heartbeat': b'data: x\n\n'},
                       {'heartbeat': b': ok\r\n\r\n'}, {'heartbeat': b'\n\n'}):
            stream = with_sse_heartbeat(ControlledUpstream(), **kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await anext(stream)


if __name__ == '__main__':
    unittest.main()
