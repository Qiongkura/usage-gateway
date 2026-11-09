import asyncio
import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from app.config import Upstream
from app.proxy import forward_with_failover

import httpx

from app.proxy import _stream_response
from app.sse import SSEParser
from app.tracing import Trace
from app.db import Store as RealStore
from app.pricing import PriceTable, ModelPrice


class Store:
    def __init__(self):
        self.records = []

    def record_request(self, **kwargs):
        self.records.append(kwargs)


class Source(httpx.AsyncByteStream):
    def __init__(self, chunks, gate=None):
        self.chunks = chunks
        self.gate = gate
        self.closed = False

    async def __aiter__(self):
        if self.gate:
            await self.gate.wait()
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def make_response(source, path='/chat/completions', content_type='text/event-stream', interval=0):
    store = Store()
    response = httpx.Response(200, headers={'content-type': content_type}, stream=source)
    result = _stream_response(resp=response, request_id='test', trace=Trace('test'), store=store,
                              cfg=SimpleNamespace(prices=None, sse_heartbeat_sec=interval),
                              upstream=SimpleNamespace(name='mock'), model='m', requested_model='m',
                              client_key_name='k', ts=1, started=time.perf_counter(), path=path, body=b'{}')
    return result, store


class ProxyP5Test(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_observation(self):
        cases = [
            ('/chat/completions', b'data: {"choices": []}\n\ndata: [DONE]\n\n', 'success'),
            ('/chat/completions', b'data: {"choices": []}\n\n', 'stream_interrupted'),
            ('/chat/completions', b'data: [DONE]\n', 'stream_interrupted'),
            ('/messages', b'event: message_stop\ndata: {"type":\ndata: "message_stop"}\n\n', 'success'),
            ('/messages', b'event: error\ndata: {"type":"error"}\n\nevent: message_stop\ndata: {}\n\n', 'upstream_error'),
            ('/other', b'data: {}\n\n', 'unknown'),
            ('/responses', b'data: [DONE]\n\n', 'stream_interrupted'),
            ('/responses', b'event: response.failed\ndata: {}\n\n', 'upstream_error'),
        ]
        for path, wire, expected in cases:
            with self.subTest(path=path, expected=expected):
                source = Source([wire[:7], wire[7:]])
                response, store = make_response(source, path)
                output = b''.join([chunk async for chunk in response.body_iterator])
                assert output == wire
                assert store.records[0]['result'] == expected
                assert source.closed


    async def test_heartbeat_before_upstream_bom_and_usage(self):
        gate = asyncio.Event()
        wire = (b'\xef\xbb\xbfdata: {"choices":[{"delta":{"content":"hello"}}],'
                b'"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\ndata: [DONE]\n\n')
        source = Source([wire[:1], wire[1:2], wire[2:]], gate)
        response, store = make_response(source, interval=0.005)
        iterator = response.body_iterator
        first = await asyncio.wait_for(anext(iterator), 1)
        assert first == b'\xef\xbb\xbf: keep-alive\n\n'
        gate.set()
        output = first + b''.join([chunk async for chunk in iterator])
        parser = SSEParser()
        blocks = parser.feed(output) + parser.finish()
        assert [b.data for b in blocks if b.dispatch][-1] == '[DONE]'
        assert store.records[0]['result'] == 'success'
        assert store.records[0]['usage_record'].output_tokens == 3
        assert source.closed


    async def test_multiline_usage_recorded_once_with_real_store(self):
        wire = (b'data: {"choices":[{"delta":{"content":"hello"}}],\n'
                b'data: "usage":{"prompt_tokens":1000,"completion_tokens":50}}\n\n'
                b'data: [DONE]\n\n')
        source = Source([wire[:31], wire[31:]])
        with tempfile.TemporaryDirectory() as directory:
            store = RealStore(Path(directory) / 'usage.db')
            try:
                response = _stream_response(
                    resp=httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=source),
                    request_id='multiline', trace=Trace('multiline'), store=store,
                    cfg=SimpleNamespace(prices=PriceTable({'mock': {'m': ModelPrice(input=2.0, output=8.0)}}),
                                        sse_heartbeat_sec=0),
                    upstream=SimpleNamespace(name='mock'), model='m', requested_model='m',
                    client_key_name='k', ts=1, started=time.perf_counter(), path='/chat/completions', body=b'{}')
                assert b''.join([chunk async for chunk in response.body_iterator]) == wire
                rows = store.read_conn().execute('SELECT * FROM usage_records').fetchall()
                assert len(rows) == 1
                row = dict(rows[0])
                assert (row['input_tokens'], row['output_tokens'], row['result']) == (1000, 50, 'success')
                assert row['cost_usd'] == 0.0024
            finally:
                store.close()


    async def test_responses_completed_real_store(self):
        wire = (b'event: response.completed\ndata: {"type":"response.completed",'
                b'"response":{"model":"m","usage":{"input_tokens":1000,"output_tokens":50,'
                b'"input_tokens_details":{"cached_tokens":200},'
                b'"output_tokens_details":{"reasoning_tokens":5}}}}\n\n')
        with tempfile.TemporaryDirectory() as directory:
            store = RealStore(Path(directory) / 'usage.db')
            try:
                response = _stream_response(
                    resp=httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Source([wire])),
                    request_id='responses', trace=Trace('responses'), store=store,
                    cfg=SimpleNamespace(prices=PriceTable({'mock': {'m': ModelPrice(input=2.0, output=8.0, cache_read=0.2)}}), sse_heartbeat_sec=0),
                    upstream=SimpleNamespace(name='mock'), model='m', requested_model='m',
                    client_key_name='k', ts=1, started=time.perf_counter(), path='/responses', body=b'{}')
                self.assertEqual(b''.join([chunk async for chunk in response.body_iterator]), wire)
                row = dict(store.read_conn().execute('SELECT * FROM usage_records').fetchone())
                self.assertEqual((row['result'], row['input_tokens'], row['output_tokens'], row['cache_read']),
                                 ('success', 800, 50, 200))
                self.assertAlmostEqual(row['cost_usd'], 0.00204)
            finally:
                store.close()

    async def test_invalid_usage_does_not_interrupt_stream(self):
        for path, wire in [
            ('/chat/completions', b'data: {"choices":[],"usage":{"prompt_tokens":"bad"}}\n\ndata: [DONE]\n\n'),
            ('/messages', b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":{}}}}\n\nevent: message_stop\ndata: {}\n\n'),
            ('/responses', b'event: response.completed\ndata: {"response":{"usage":{"input_tokens":[]}}}\n\n'),
        ]:
            with self.subTest(path=path):
                source = Source([wire])
                response, store = make_response(source, path)
                self.assertEqual(b''.join([chunk async for chunk in response.body_iterator]), wire)
                self.assertEqual(store.records[0]['result'], 'success')
                self.assertTrue(source.closed)

    async def test_prior_block_survives_oversized_same_chunk(self):
        for delimiter in (b'\n\n', b'\r\r', b'\r\n\r\n'):
            with self.subTest(delimiter=delimiter):
                wire = b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}' + delimiter + b'data: ' + b'x' * (1024 * 1024 + 10)
                response, store = make_response(Source([wire]))
                self.assertEqual(b''.join([chunk async for chunk in response.body_iterator]), wire)
                self.assertEqual(store.records[0]['result'], 'unknown')
                self.assertEqual(store.records[0]['usage_record'].input_tokens, 7)

    async def test_long_line_and_cr_forward_without_waiting_for_eof(self):
        gate = asyncio.Event()
        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'x' * (1024 * 1024 + 10)
                await gate.wait()
                yield b'\rdata: [DONE]\r\r'
        response, store = make_response(Waiting())
        iterator = response.body_iterator
        first = await asyncio.wait_for(anext(iterator), 1)
        self.assertEqual(first, b'x' * (1024 * 1024 + 10))
        gate.set()
        rest = b''.join([chunk async for chunk in iterator])
        self.assertEqual(rest, b'\rdata: [DONE]\r\r')
        self.assertEqual(store.records[0]['result'], 'unknown')

    async def test_pure_cr_dispatches_before_next_chunk(self):
        gate = asyncio.Event()
        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: hello\r\r'
                await gate.wait()
                yield b'data: [DONE]\r\r'
        response, store = make_response(Waiting())
        iterator = response.body_iterator
        self.assertEqual(await asyncio.wait_for(anext(iterator), 1), b'data: hello\r\r')
        gate.set()
        self.assertEqual(b''.join([chunk async for chunk in iterator]), b'data: [DONE]\r\r')
        self.assertEqual(store.records[0]['result'], 'success')

    async def test_unparseable_json_does_not_interrupt_proxy(self):
        values = [b'1' * 5000, b'[' * 2000 + b'0' + b']' * 2000]
        for value in values:
            wire = b'data: {"value":' + value + b'}\n\ndata: [DONE]\n\n'
            with self.subTest(length=len(value)):
                source = Source([wire])
                response, store = make_response(source)
                self.assertEqual(b''.join([chunk async for chunk in response.body_iterator]), wire)
                self.assertEqual(store.records[0]['result'], 'success')
                self.assertTrue(source.closed)

    async def test_unusual_json_frames_and_done_are_forwarded(self):
        frames = [
            b'data: {"choices":[{"delta":{"role":[],"content":[{"type":[]}]}}]}',
            b'data: {"choices":[{"delta":{"content":[{"type":[]}]}}]}',
            b'data: {"object":"\\ud800"}',
            b'data: {"choices":[{"delta":{"content":[{"type":"text","text":42}],"role":"user"}}]}',
            b'data: {"choices":[{"delta":{"content":42,"tool_calls":[{"function":{"name":""}}]}}]}',
            b'data: {"choices":[42,{"delta":{"role":"user"}}]}',
            b'data: {"choices":[{"delta":{"content":' + b'[' * 600 + b'"x"' + b']' * 600 + b'}}]}',
        ]
        for frame in frames:
            wire = frame + b'\n\ndata: [DONE]\n\n'
            for chunks in ([wire], [wire[:7], wire[7:]]):
                with self.subTest(frame=frame, chunks=len(chunks)):
                    source = Source(chunks)
                    response, store = make_response(source)
                    self.assertEqual(b''.join([chunk async for chunk in response.body_iterator]), wire)
                    self.assertEqual(store.records[0]['result'], 'success')
                    self.assertTrue(source.closed)

    async def test_oversized_complete_line_is_never_normalized(self):
        prefix = b'data: {"choices":null,"usage":{},"padding":"'
        suffix = b'"}'
        for length in (1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1):
            line = prefix + b'x' * (length - len(prefix) - len(suffix)) + suffix
            wire = line + b'\n\ndata: [DONE]\n\n'
            outputs = []
            for chunks in ([wire], [line[:1024 * 1024], line[1024 * 1024:] + b'\n\ndata: [DONE]\n\n']):
                response, store = make_response(Source(chunks))
                outputs.append(b''.join([chunk async for chunk in response.body_iterator]))
            with self.subTest(length=length):
                self.assertEqual(outputs[0], outputs[1])
                if length >= 1024 * 1024:
                    self.assertEqual(outputs[0], wire)
                else:
                    self.assertNotEqual(outputs[0], wire)
                    self.assertIn(b'"choices": []', outputs[0])

    async def test_normalizer_bug_is_not_swallowed(self):
        response, store = make_response(Source([b'data: {"choices":[]}\n\n']))
        with patch('app.proxy._normalize_openai_payload', side_effect=RuntimeError('bug')):
            with self.assertRaisesRegex(RuntimeError, 'bug'):
                _ = [chunk async for chunk in response.body_iterator]
        self.assertEqual(store.records[0]['result'], 'stream_interrupted')

    async def test_http_error_never_appends_sse_frame(self):
        class Broken(Source):
            async def __aiter__(self):
                for chunk in self.chunks:
                    yield chunk
                raise httpx.ReadError('private detail')

        cases = [
            ('application/json', [b'{"partial":'], b'{"partial":'),
            ('text/event-stream', [b'data: {"x":1}\n'], b'data: {"x":1}\n'),
            ('text/event-stream', [b'data: {"x":1}\n\ndata: {"partial":'], b'data: {"x":1}\n\n'),
            ('', [b'data: {"x":1}\n'], b'data: {"x":1}\n'),
            ('text/event-stream', [b'data: [DONE]\n\n'], b'data: [DONE]\n\n'),
        ]
        for content_type, chunks, expected in cases:
            with self.subTest(content_type=content_type, chunks=chunks):
                source = Broken(chunks)
                response, store = make_response(source, content_type=content_type)
                self.assertEqual(b''.join([chunk async for chunk in response.body_iterator]), expected)
                self.assertEqual(len(store.records), 1)
                record = store.records[0]
                self.assertEqual((record['result'], record['error_stage'], record['error_kind']),
                                 ('stream_interrupted', 'stream', 'ReadError'))
                self.assertNotIn('private detail', str(record))
                self.assertTrue(source.closed)

    async def test_key_retry_http_error_never_reuses_closed_response(self):
        for streaming in (False, True):
            for failover in (False, True):
                with self.subTest(streaming=streaming, failover=failover):
                    calls = []
                    old = httpx.Response(429, content=b'old response')
                    def handler(request):
                        calls.append((request.url.host, request.headers.get('authorization')))
                        if len(calls) == 1:
                            return old
                        if len(calls) == 2:
                            raise httpx.ReadError('private detail', request=request)
                        return httpx.Response(200, json={'choices': []})

                    upstream = Upstream(name='first', type='openai', base_url='http://first.local',
                                        api_key='primary-key', api_keys=['backup-key'])
                    alternate = Upstream(name='second', type='openai', base_url='http://second.local', api_key='other-key')
                    store = Store()
                    cfg = SimpleNamespace(prices=None, proxy_for=lambda up: None, failover=failover)
                    request = Request({'type': 'http', 'method': 'POST', 'path': '/',
                                       'headers': [], 'query_string': b''})
                    body = b'{"stream":true}' if streaming else b'{"stream":false}'
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        with patch('app.proxy.async_http_client', new=AsyncMock(return_value=client)), \
                             patch.dict('app.proxy._KEY_COOLDOWN', {}, clear=True):
                            response = await forward_with_failover(
                                request=request, cfg=cfg, store=store, tracker=None,
                                path='/chat/completions', body=body, client_key_name='k',
                                model='m', requested_model='m', candidates=[upstream, alternate] if failover else [upstream],
                                allow_retry=failover)
                            if streaming and failover:
                                await response.body_iterator.__anext__()
                                await response.body_iterator.aclose()
                    self.assertTrue(old.is_closed)
                    self.assertEqual(calls[:2], [('first.local', 'Bearer primary-key'), ('first.local', 'Bearer backup-key')])
                    self.assertEqual(response.status_code, 200 if failover else 502)
                    if not failover:
                        self.assertEqual(store.records[0]['result'], 'upstream_error')
                        self.assertEqual(store.records[0]['error_kind'], 'ReadError')
                        self.assertNotIn('private detail', str(store.records))
                    else:
                        self.assertEqual(calls[2][0], 'second.local')

    async def test_key_retry_timeouts_match_initial_timeout(self):
        for streaming in (False, True):
            for error_type in (httpx.ConnectTimeout, httpx.ReadTimeout,
                               httpx.WriteTimeout, httpx.PoolTimeout, httpx.TimeoutException):
                for after_key_switch in (False, True):
                    with self.subTest(streaming=streaming, error_type=error_type.__name__,
                                      after_key_switch=after_key_switch):
                        calls = []
                        old = httpx.Response(429, content=b'old response')
                        def handler(request):
                            calls.append(request.headers.get('authorization'))
                            if after_key_switch and len(calls) == 1:
                                return old
                            raise error_type('private detail', request=request)

                        upstream = Upstream(name='first', type='openai', base_url='http://first.local',
                                            api_key='primary-key', api_keys=['backup-key'])
                        store = Store()
                        cfg = SimpleNamespace(prices=None, proxy_for=lambda up: None, failover=False)
                        request = Request({'type': 'http', 'method': 'POST', 'path': '/',
                                           'headers': [], 'query_string': b''})
                        body = b'{"stream":true}' if streaming else b'{"stream":false}'
                        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                            with patch('app.proxy.async_http_client', new=AsyncMock(return_value=client)), \
                                 patch.dict('app.proxy._KEY_COOLDOWN', {}, clear=True):
                                response = await forward_with_failover(
                                    request=request, cfg=cfg, store=store, tracker=None,
                                    path='/chat/completions', body=body, client_key_name='k',
                                    model='m', requested_model='m', candidates=[upstream], allow_retry=False)
                        self.assertEqual(response.status_code, 504)
                        self.assertEqual(len(store.records), 1)
                        record = store.records[0]
                        self.assertEqual((record['status'], record['result'], record['error_stage'],
                                          record['error_kind'], record['error_status']),
                                         (504, 'timeout', 'upstream', error_type.__name__, 504))
                        self.assertNotIn('private detail', str(record))
                        self.assertEqual(calls, ['Bearer primary-key', 'Bearer backup-key'] if after_key_switch
                                         else ['Bearer primary-key'])
                        if after_key_switch:
                            self.assertTrue(old.is_closed)

    async def test_non_sse_no_heartbeat(self):
        gate = asyncio.Event()
        source = Source([b'{"ok":true}'], gate)
        response, store = make_response(source, content_type='application/json', interval=0.005)
        task = asyncio.create_task(anext(response.body_iterator))
        await asyncio.sleep(0.025)
        assert not task.done()
        gate.set()
        assert await task == b'{"ok":true}'
        with self.assertRaises(StopAsyncIteration):
            await anext(response.body_iterator)
        assert store.records[0]['result'] == 'unknown'
