import json
import unittest

from app.diagnostic_bundle import (
    MAX_SPANS,
    MAX_SPANS_JSON_BYTES,
    MAX_TRACES,
    build_diagnostic_bundle,
)


class DiagnosticBundleTests(unittest.TestCase):
    def test_realistic_trace_and_database_row(self):
        span = {
            "name": "record", "start_ms": 10.2, "end_ms": 20,
            "duration_ms": 9.8, "error": "private failure",
            "detail": {"tokens": {"input": 5, "output": 6, "cache_read": 2,
                                  "cache_creation": 1}, "upstream_cost": 0.01},
        }
        result = build_diagnostic_bundle([{
            "ts": 1700000000, "status": 200, "total_ms": 21,
            "spans_json": json.dumps([span]),
        }])
        self.assertEqual(result, {"schema_version": 1, "traces": [{
            "ts": 1700000000, "status": 200, "total_ms": 21,
            "spans": [{"name": "record", "start_ms": 10.2, "end_ms": 20,
                       "duration_ms": 9.8, "has_error": True,
                       "detail": {"tokens": {"input": 5, "output": 6,
                                             "cache_read": 2, "cache_creation": 1},
                                  "upstream_cost": 0.01}}],
        }]})

    def test_sensitive_data_never_copied_even_in_allowed_fields(self):
        secret = 'private-prompt-response-header-key-tool-argument'
        trace = {
            "trace_id": secret, "source": secret, "model": secret,
            "requested_model": secret, "client_key": secret, "prompt": secret,
            "response": secret, "headers": {"Authorization": secret}, "key": secret,
            "tools": [{"arguments": secret}], "status": secret, "total_ms": secret,
            "spans": [{"name": secret, "error": secret,
                       "detail": {"url": secret, "target": secret, "model": secret,
                                  "reason": secret, "last_error": secret,
                                  "headers": {"x": secret}, "attempt": secret,
                                  "tokens": {"input": secret, "output": {"nested": secret},
                                             "other": secret},
                                  "upstream_cost": [secret]},
                       "nested": {"response": secret}}],
        }
        output = json.dumps(build_diagnostic_bundle([trace]), allow_nan=False)
        self.assertNotIn(secret, output)
        self.assertEqual(json.loads(output)["traces"][0]["spans"], [{"has_error": True}])
        self.assertEqual(trace["trace_id"], secret)

    def test_invalid_numeric_types_and_nonfinite_values(self):
        result = build_diagnostic_bundle([{
            "ts": True, "status": 200.0, "total_ms": float("nan"),
            "spans": [{"name": "parse", "start_ms": float("inf"), "end_ms": -1,
                       "duration_ms": 604_800_001,
                       "detail": {"bytes": True, "status": 600, "attempt": -1,
                                  "upstream_cost": float("-inf"),
                                  "tokens": {"input": 1.5, "output": False}}}],
        }])
        self.assertEqual(result["traces"], [{"spans": [{"name": "parse"}]}])
        json.dumps(result, allow_nan=False)

    def test_malformed_nested_and_cyclic_values(self):
        cyclic = {}
        cyclic["detail"] = cyclic
        cyclic["tokens"] = cyclic
        cyclic["name"] = "record"
        result = build_diagnostic_bundle([None, {"spans": [None, [], cyclic]},
                                          {"spans_json": "[invalid"},
                                          {"spans_json": '"not a list"'},
                                          {"spans_json": "[" * 2000},
                                          {"spans_json": "\ud800"}])
        self.assertEqual(result["traces"], [
            {"spans": [{"name": "record"}]}, {"spans": []},
            {"spans": []}, {"spans": []}, {"spans": []},
        ])
        with self.assertRaises(ValueError):
            build_diagnostic_bundle({"spans": []})

    def test_limits_and_spans_precedence(self):
        trace = {"spans": [{"name": "receive"}] * (MAX_SPANS + 1)}
        result = build_diagnostic_bundle([trace] * (MAX_TRACES + 1))
        self.assertEqual(len(result["traces"]), MAX_TRACES)
        self.assertEqual(len(result["traces"][0]["spans"]), MAX_SPANS)
        result = build_diagnostic_bundle([
            {"spans": [], "spans_json": '[{"name":"record"}]'},
            {"spans_json": " " * (MAX_SPANS_JSON_BYTES + 1)},
        ])
        self.assertEqual(result["traces"], [{"spans": []}, {"spans": []}])


if __name__ == "__main__":
    unittest.main()
