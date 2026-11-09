"""Offline tests for the standalone SSE observer."""
import unittest

from app.sse import SSELimitError, SSEParser


class SSEParserTest(unittest.TestCase):
    def parse(self, raw, size=1):
        parser = SSEParser()
        blocks = []
        for pos in range(0, len(raw), size):
            blocks.extend(parser.feed(raw[pos:pos + size]))
        blocks.extend(parser.finish())
        self.assertEqual(b"".join(block.raw for block in blocks), raw)
        return blocks

    def test_chunk_boundaries_utf8_line_endings_and_bom(self):
        raw = (b'\xef\xbb\xbf: heartbeat\r\nevent: content_block_delta\r'
               + 'data: {"text":"你好"}'.encode() + b'\r\ndata: second\n\r\n'
               + b'data: [DONE]\r\r')
        for size in range(1, len(raw) + 1):
            with self.subTest(size=size):
                blocks = self.parse(raw, size)
                self.assertEqual(len(blocks), 2)
                self.assertTrue(blocks[0].dispatch)
                self.assertEqual(blocks[0].event, "content_block_delta")
                self.assertEqual(blocks[0].data, '{"text":"你好"}\nsecond')
                self.assertEqual(blocks[1].data, '[DONE]')
                self.assertEqual(blocks[1].event, 'message')

    def test_fields_and_stream_state(self):
        blocks = self.parse(b':comment\nunknown: hi\nid: abc\nretry: 123\n\n'
                            b'id: bad\x00id\nretry: +1\ndata\ndata:  two\n\n'
                            b'id\nevent: named\ndata:\n\n\n')
        self.assertFalse(blocks[0].dispatch)
        self.assertEqual((blocks[0].last_event_id, blocks[0].retry), ('abc', 123))
        self.assertEqual(blocks[1].data, '\n two')
        self.assertEqual((blocks[1].last_event_id, blocks[1].retry), ('abc', 123))
        self.assertTrue(blocks[2].dispatch)
        self.assertEqual((blocks[2].event, blocks[2].data, blocks[2].last_event_id),
                         ('named', '', ''))
        self.assertFalse(blocks[3].dispatch)

    def test_eof_does_not_dispatch_unterminated_event(self):
        for raw in (b'data: x', b'data: x\n', b'data: x\r', b'data: x\r\n'):
            with self.subTest(raw=raw):
                blocks = self.parse(raw)
                self.assertEqual(len(blocks), 1)
                self.assertFalse(blocks[0].dispatch)
        self.assertTrue(self.parse(b'data: x\r\r')[0].dispatch)
        self.assertEqual(self.parse(b''), [])

    def test_invalid_utf8_is_replaced_only_in_parsed_value(self):
        block = self.parse(b'data: \xff\xe4\xb8\n\n')[0]
        self.assertEqual(block.data, '\ufffd\ufffd')
        self.assertEqual(block.raw, b'data: \xff\xe4\xb8\n\n')

    def test_limit_includes_delimiters_and_resets_at_boundary(self):
        parser = SSEParser(9)
        blocks = parser.feed(b'data: x\n\n' * 2)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(parser.finish(), [])
        parser = SSEParser(8)
        with self.assertRaises(SSELimitError):
            parser.feed(b'data: x\n\n')
        with self.assertRaises(RuntimeError):
            parser.finish()
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                SSEParser(value)

    def test_crlf_split_at_limit(self):
        parser = SSEParser(3)
        self.assertEqual(parser.feed(b'\r'), [])
        self.assertEqual(parser.feed(b'\n')[0].raw, b'\r\n')
        self.assertEqual(parser.finish(), [])
        parser = SSEParser(1)
        parser.feed(b'\r')
        with self.assertRaises(SSELimitError):
            parser.feed(b'\n')

    def test_provider_frames_unchanged(self):
        raw = (b'event: message_start\ndata: {"type":"message_start"}\n\n'
               b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'
               b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
               b'data: [DONE]\n\n')
        blocks = self.parse(raw, 7)
        self.assertEqual([b.event for b in blocks],
                         ['message_start', 'content_block_delta', 'message', 'message'])
        self.assertTrue(all(b.dispatch for b in blocks))
        self.assertEqual(blocks[-1].data, '[DONE]')

    def test_lifecycle_and_retry_bound(self):
        parser = SSEParser()
        block = parser.feed(b'retry: 000000000001\n\n')[0]
        self.assertEqual(block.retry, 1)
        block = parser.feed(b'retry: 999999999999\n\n')[0]
        self.assertEqual(block.retry, 999999999)
        parser.finish()
        with self.assertRaises(RuntimeError):
            parser.feed(b'')
        with self.assertRaises(RuntimeError):
            parser.finish()


if __name__ == '__main__':
    unittest.main()
