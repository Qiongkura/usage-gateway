"""Offline SSE terminal-frame observations, independent of proxy implementation.

SSEParser only exposes parsed blocks; it does not classify stream outcomes or
observe client cancellation. These tests deliberately make no proxy assertions.
"""
import asyncio
import unittest

from app.sse import SSEParser


class StreamTerminalParserTest(unittest.TestCase):
    def parse_in_chunks(self, wire, cuts):
        parser = SSEParser()
        blocks = []
        start = 0
        for end in cuts:
            blocks.extend(parser.feed(wire[start:end]))
            start = end
        blocks.extend(parser.feed(wire[start:]))
        blocks.extend(parser.finish())
        self.assertEqual(b"".join(block.raw for block in blocks), wire)
        return blocks

    def test_openai_done_and_anthropic_message_stop_across_every_split(self):
        cases = (
            (b'data: {"choices":[{"delta":{"content":"hi"}}]}\r\n\r\n'
             b'data: [DONE]\r\n\r\n', "message", "[DONE]"),
            (b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'
             b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
             "message_stop", '{"type":"message_stop"}'),
        )
        for wire, event, data in cases:
            for split in range(len(wire) + 1):
                with self.subTest(event=event, split=split):
                    blocks = self.parse_in_chunks(wire, [split])
                    self.assertEqual(len(blocks), 2)
                    self.assertTrue(all(block.dispatch for block in blocks))
                    self.assertEqual((blocks[-1].event, blocks[-1].data), (event, data))

    def test_eof_without_terminal_and_incomplete_terminal(self):
        first = b'data: {"choices":[]}\n\n'
        for tail in (b'', b'data: [DONE]', b'data: [DONE]\n',
                     b'event: message_stop\ndata: {"type":"message_stop"}\n'):
            with self.subTest(tail=tail):
                blocks = self.parse_in_chunks(first + tail, [len(first)])
                self.assertTrue(blocks[0].dispatch)
                self.assertEqual(len(blocks), 2 if tail else 1)
                if tail:
                    self.assertFalse(blocks[-1].dispatch)
                    self.assertEqual(blocks[-1].raw, tail)

    def test_error_event_is_dispatched_without_becoming_done(self):
        wire = (b'event: error\ndata: {"type":"error",'
                b'"error":{"message":"failure"}}\n\n')
        blocks = self.parse_in_chunks(wire, list(range(1, len(wire))))
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].dispatch)
        self.assertEqual(blocks[0].event, "error")
        self.assertEqual(blocks[0].data, '{"type":"error","error":{"message":"failure"}}')

    def test_heartbeats_do_not_dispatch_or_mask_terminal(self):
        wire = (b': ping\r\n\r\ndata: {"choices":[]}\r\n\r\n'
                b': keepalive\r\n\r\ndata: [DONE]\r\n\r\n')
        blocks = self.parse_in_chunks(wire, list(range(1, len(wire))))
        self.assertEqual([block.dispatch for block in blocks], [False, True, False, True])
        self.assertEqual([block.data for block in blocks if block.dispatch],
                         ['{"choices":[]}', '[DONE]'])


class StreamCancellationParserTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_consumer_does_not_dispatch_partial_terminal(self):
        parser = SSEParser()
        entered_wait = asyncio.Event()
        closed = asyncio.Event()
        observed = []

        async def upstream():
            try:
                yield b'data: {"choices":[]}\n\ndata: [DO'
                entered_wait.set()
                await asyncio.Event().wait()
                yield b'NE]\n\n'
            finally:
                closed.set()

        async def consume():
            stream = upstream()
            try:
                async for chunk in stream:
                    observed.extend(parser.feed(chunk))
            finally:
                await stream.aclose()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered_wait.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].data, '{"choices":[]}')
        self.assertTrue(observed[0].dispatch)
        # Cancellation is not parser EOF; inspecting its leftover separately must
        # not turn the incomplete terminal frame into a dispatched event.
        tail = parser.finish()
        self.assertEqual(len(tail), 1)
        self.assertFalse(tail[0].dispatch)
        self.assertEqual(tail[0].raw, b'data: [DO')


if __name__ == '__main__':
    unittest.main()
