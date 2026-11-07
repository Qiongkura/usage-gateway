"""响应与请求兼容层回归测试。"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Upstream  # noqa: E402
from app.proxy import (  # noqa: E402
    _extract_stream_text,
    _is_openai_chat_path,
    _iter_normalized_sse,
    _normalize_openai_payload,
    _normalize_request_body,
    _normalize_sse_line,
    _obj_has_output_token,
    usage_from_openai,
)


def chunk(delta: dict, finish=None) -> bytes:
    obj = {
        "id": "b726807dfa92477794b22998e2012872",
        "object": "chat.completion.chunk",
        "created": 1794030037,
        "model": "deepseek-v4-1-flash-1017",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return b"data: " + json.dumps(obj).encode()


def parse(line: bytes) -> dict:
    assert line.startswith(b"data: ")
    return json.loads(line[6:])


class _FakeStreamResponse:
    def __init__(self, byte_chunks: list[bytes]):
        self._chunks = byte_chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


def _mistral_upstream() -> Upstream:
    return Upstream(
        name="mistral",
        type="openai",
        base_url="https://api.mistral.ai/v1",
        api_key="test",
        compat={
            "developer_role": "system",
            "rename_params": {"seed": "random_seed", "max_output_tokens": "max_tokens"},
            "reasoning_effort_map": {"minimal": "low", "medium": "high"},
            "reasoning_models": ["deepseek-*", "magistral-*", "*-reasoning*"],
            "allow_params": [
                "temperature", "top_p", "max_tokens", "stream", "stream_options",
                "tools", "tool_choice", "reasoning_effort", "random_seed",
            ],
            "strip_message_fields": ["name", "reasoning_content", "reasoning"],
        },
    )


class ContentNormalizationTest(unittest.TestCase):
    def test_reported_failure_thinking_array(self):
        """用户实际报错的 chunk：content 是 thinking 数组。"""
        line = chunk({
            "index": 0,
            "content": [{
                "type": "thinking",
                "thinking": [{"type": "text", "text": "The"}],
                "closed": True,
            }],
        })
        delta = parse(_normalize_sse_line(line, "/v1/chat/completions"))["choices"][0]["delta"]
        self.assertIsInstance(delta["content"], str)
        self.assertEqual(delta["content"], "")
        self.assertEqual(delta["reasoning_content"], "The")

    def test_text_array(self):
        line = chunk({"content": [{"type": "text", "text": "你好"},
                                  {"type": "text", "text": "，世界"}]})
        delta = parse(_normalize_sse_line(line, "/v1/chat/completions"))["choices"][0]["delta"]
        self.assertEqual(delta["content"], "你好，世界")
        self.assertNotIn("reasoning_content", delta)

    def test_mixed_text_and_thinking(self):
        line = chunk({"content": [
            {"type": "thinking", "thinking": [{"type": "text", "text": "想一想"}]},
            {"type": "text", "text": "答案"},
        ]})
        delta = parse(_normalize_sse_line(line, "/v1/chat/completions"))["choices"][0]["delta"]
        self.assertEqual(delta["content"], "答案")
        self.assertEqual(delta["reasoning_content"], "想一想")

    def test_standard_string_untouched(self):
        line = chunk({"content": "hello"})
        self.assertIs(_normalize_sse_line(line, "/v1/chat/completions"), line)

    def test_tool_calls_untouched_when_valid(self):
        line = chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                      "function": {"name": "read", "arguments": "{}"}}]})
        self.assertIs(_normalize_sse_line(line, "/v1/chat/completions"), line)

    def test_tool_calls_continuation_empty_name_normalized(self):
        """Mistral 续传 tool_calls 分片带 name: '' 时应置为 None，且补齐缺失 index。"""
        line = chunk({"tool_calls": [{"function": {"name": "", "arguments": "{\"a\":1}"}}]})
        delta = parse(_normalize_sse_line(line, "/v1/chat/completions"))["choices"][0]["delta"]
        tc = delta["tool_calls"][0]
        self.assertEqual(tc["index"], 0)
        self.assertIsNone(tc["function"]["name"])
        self.assertEqual(tc["function"]["arguments"], "{\"a\":1}")

    def test_null_choices_on_usage_frame_normalized(self):
        payload = {"id": "1", "object": "chat.completion.chunk", "choices": None,
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        self.assertTrue(_normalize_openai_payload(payload))
        self.assertEqual(payload["choices"], [])

    def test_done_and_comments_passthrough(self):
        for line in (b"data: [DONE]", b": keep-alive", b""):
            self.assertIs(_normalize_sse_line(line, "/v1/chat/completions"), line)

    def test_anthropic_path_untouched(self):
        line = chunk({"content": [{"type": "thinking", "thinking": []}]})
        self.assertIs(_normalize_sse_line(line, "/v1/messages"), line)

    def test_non_stream_message_array(self):
        payload = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
                        {"type": "text", "text": "正文"},
                    ],
                },
                "finish_reason": "stop",
            }]
        }
        self.assertTrue(_normalize_openai_payload(payload))
        msg = payload["choices"][0]["message"]
        self.assertEqual(msg["content"], "正文")
        self.assertEqual(msg["reasoning_content"], "hmm")

    def test_reasoning_array_flattened(self):
        payload = {"choices": [{"delta": {"reasoning_content": [
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"},
        ]}}]}
        self.assertTrue(_normalize_openai_payload(payload))
        self.assertEqual(payload["choices"][0]["delta"]["reasoning_content"], "ab")

    def test_paths(self):
        self.assertTrue(_is_openai_chat_path("/v1/chat/completions"))
        self.assertTrue(_is_openai_chat_path("/openai/v1/chat/completions"))
        self.assertFalse(_is_openai_chat_path("/v1/messages"))
        self.assertFalse(_is_openai_chat_path("/v1beta/models/x:streamGenerateContent"))

    def test_split_tcp_chunks_and_true_ttft(self):
        """跨 TCP 分片的 SSE 行不能丢 usage，且空预热帧不计为首字。"""
        f0 = chunk({"role": "assistant", "content": ""}) + b"\n\n"
        f1 = chunk({"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "思考"}]}]}) + b"\n\n"
        f2 = chunk({"tool_calls": [{"index": 0, "id": "t1", "type": "function",
                                    "function": {"name": "bash", "arguments": "{\"cmd\":\"ls\"}"}}]}) + b"\n\n"
        f3 = b'data: {"id":"1","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":100,"completion_tokens":42,"total_tokens":142,"prompt_tokens_details":{"cached_tokens":80}}}\n\ndata: [DONE]\n\n'
        raw = f0 + f1 + f2 + f3
        # 故意在 JSON 中间切断成多个碎片
        pieces = [raw[:35], raw[35:190], raw[190:410], raw[410:]]

        collected: dict = {}
        sink: list[str] = []
        first_token: list[float] = []

        async def run():
            out = []
            async for part in _iter_normalized_sse(
                _FakeStreamResponse(pieces),
                "/chat/completions",
                collected=collected,
                text_sink=sink,
                first_token_at=first_token,
            ):
                out.append(part)
            return b"".join(out)

        combined = asyncio.run(run())
        self.assertEqual(len(first_token), 1)
        self.assertEqual(collected["usage"]["input"], 20)
        self.assertEqual(collected["usage"]["cache_read"], 80)
        self.assertEqual(collected["usage"]["output"], 42)
        self.assertIn("思考", "".join(sink))
        self.assertIn("{\"cmd\":\"ls\"}", "".join(sink))
        # 验证转发出去的所有 data 行里 content 都是 str
        for line in combined.splitlines():
            if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                obj = json.loads(line[5:].strip())
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    if "content" in d and d["content"] is not None:
                        self.assertIsInstance(d["content"], str)

    def test_first_token_detection_ignores_empty_preamble(self):
        empty = json.loads(chunk({"role": "assistant", "content": ""})[6:])
        real = json.loads(chunk({"reasoning_content": "A"})[6:])
        self.assertFalse(_obj_has_output_token(empty, "/chat/completions"))
        self.assertTrue(_obj_has_output_token(real, "/chat/completions"))

    def test_extract_stream_text_includes_tool_calls(self):
        sink: list[str] = []
        obj = json.loads(chunk({"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "Write", "arguments": "{\"file_path\":\"a.py\"}"},
        }]})[6:])
        _extract_stream_text(obj, sink)
        self.assertEqual("".join(sink), "Write{\"file_path\":\"a.py\"}")

    def test_usage_from_openai_completion_plus_reasoning_total(self):
        u = usage_from_openai({"usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "reasoning_tokens": 150,
            "total_tokens": 300,
        }})
        self.assertEqual(u["output"], 200)
        self.assertEqual(u["reasoning"], 150)


class RequestNormalizationTest(unittest.TestCase):
    def test_mistral_request_compat_full(self):
        up = _mistral_upstream()
        raw = {
            "model": "mistral/deepseek-v4-1-flash-1017",
            "stream": True,
            "store": False,
            "seed": 42,
            "reasoning_effort": "medium",
            "messages": [
                {"role": "developer", "name": "sys", "content": [
                    {"type": "text", "text": "You are helpful", "cache_control": {"type": "ephemeral"}},
                ]},
                {"role": "user", "name": "alice", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "let me think",
                    "tool_calls": [{
                        "id": "call_123",
                        "index": 0,
                        "type": "function",
                        "function": {"name": "read", "arguments": {"path": "x"}, "extra": 1},
                        "extra_content": {"google": {"thought_signature": "sig"}},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_123", "name": "read", "content": "ok"},
            ],
        }
        out = json.loads(_normalize_request_body(
            json.dumps(raw).encode(),
            up,
            payload=raw,
            path="/chat/completions",
            use_stream=True,
            model_override="deepseek-v4-1-flash-1017",
            reuse_payload=True,
        ))
        self.assertEqual(out["model"], "deepseek-v4-1-flash-1017")
        self.assertNotIn("store", out)
        self.assertEqual(out["random_seed"], 42)
        # medium -> high
        self.assertEqual(out["reasoning_effort"], "high")
        # 自动注入 include_usage
        self.assertEqual(out["stream_options"], {"include_usage": True})
        # developer -> system, cache_control 剥离
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertNotIn("name", out["messages"][0])
        self.assertNotIn("cache_control", out["messages"][0]["content"][0])
        # assistant reasoning_content 与 tool_calls.extra_content 剥离
        assistant_msg = out["messages"][2]
        self.assertNotIn("reasoning_content", assistant_msg)
        tc = assistant_msg["tool_calls"][0]
        self.assertEqual(set(tc.keys()), {"id", "type", "function"})
        self.assertEqual(set(tc["function"].keys()), {"name", "arguments"})
        self.assertEqual(tc["function"]["arguments"], "{\"path\": \"x\"}")
        # tool 消息保留 name
        self.assertEqual(out["messages"][3]["name"], "read")

    def test_mistral_non_reasoning_model_strips_reasoning_effort(self):
        up = _mistral_upstream()
        raw = {
            "model": "mistral-large-latest",
            "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = json.loads(_normalize_request_body(
            json.dumps(raw).encode(),
            up,
            payload=raw,
            path="/chat/completions",
            use_stream=False,
            reuse_payload=True,
        ))
        self.assertNotIn("reasoning_effort", out)


if __name__ == "__main__":
    unittest.main()
