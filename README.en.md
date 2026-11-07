# Usage Gateway

A local-first multi-upstream LLM gateway: **clients only see the keys you issue** — upstream
keys never leave the server. Every call is metered into a per-call ledger with itemized cost,
cache-hit savings, and a built-in web dashboard.

```text
Clients (OpenAI / Anthropic SDK)               Upstreams
      │  sk-gw-xxxx                 ┌──────────────────────────────┐
      ▼                             │  Antigravity Tools  (sub)    │
┌──────────────────┐   prefix route  │  Mistral API        (sub)    │
│  usage-gateway   │ ─────────────▶ │  OpenCode Zen       (sub)    │
│  :9000           │                │  New API relays     (free)   │
└──────────────────┘                │  ...custom                   │
      │        ▲                    └──────────────────────────────┘
      │        └── collectors: quota snapshots, local client logs
      ▼
  Web dashboard + SQLite (per-call ledger / tracing / heatmap / alerts)
```

## Features

**Proxy core**
- OpenAI & Anthropic compatible (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`, `/v1/models`)
- Strict prefix routing (`alias/model`) — never silently switches channels (protects cheap pools)
- Upstream health circuit breaker with exponential cooldown; optional same-alias failover
- Multi-key fallback chain per upstream (402/429 → next key, cooldown + auto-recovery)
- Compat layer: array-content flattening, tool-call chunk normalization, developer-role rewrite,
  strict-schema param whitelisting, auto `include_usage`, non-stream → SSE wrapping
- Connection pooling with 5-minute keepalive

**Per-call ledger**
- Every call records: uncached input / cache read / cache write / output (incl. thinking) /
  status / TTFT / total latency / tok-s
- Itemized bill per call: input + cache-read + cache-write + output × time multiplier,
  plus **how much money the prompt cache saved you**
- Call Logs view: filters (time / channel / key / origin / status / model search), summary bar,
  pagination, CSV export; zh / EN UI
- Costs are always converted at official API list prices; subscription / free channels are tagged
- Upstream-reported costs (e.g. Zen `list_cost`) take precedence; historical records can be backfilled

**Dashboard**
- Summary cards, composition / cost / request trends, 365-day heatmap, request health timeline
- Compact multi-account quota cards with reset times, quota alerts (desktop + webhook, state machine)
- Request trace waterfall, live tok/s liquid-glass widget (`/float`)

**Collectors** (read-only)
- Antigravity Tools (SQLite usage + per-account quota windows)
- Mistral billing (API / Vibe budgets), OpenCode Zen (5h / weekly / monthly)
- OpenCode CLI local database, Vibe session logs (incremental deltas)

## Quick start

Python ≥ 3.10. Using [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/Qiongkura/usage-gateway.git
cd usage-gateway
uv sync
cp config.example.yaml config.yaml   # fill in your upstream + client keys
uv run python -m app.main
```

Dashboard at http://127.0.0.1:9000, API docs at `/docs`.

Binds to `127.0.0.1` by default. If you expose it, set `server.admin_key`
(protects `/api/admin/*`) and review the read-only `/api` surface.

## Client usage

Any OpenAI SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:9000/v1", api_key="sk-gw-default-0001")
client.chat.completions.create(model="opgo/kimi-k3", messages=[...])   # prefix required
```

Anthropic SDK / Claude Code:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9000
export ANTHROPIC_API_KEY=sk-gw-default-0001
```

## Configuration

See [config.example.yaml](config.example.yaml). Highlights:

| Key | Purpose |
|---|---|
| `gateway.client_keys[].allow_models` | per-key model whitelist |
| `upstreams[].alias` | model prefix (`alias: opgo` → `opgo/kimi-k3`) |
| `upstreams[].billing_mode` | `payg` / `subscription` (list-price equivalence) / `free` |
| `upstreams[].api_keys` | ordered fallback key chain (402/429 → next key) |
| `proxy` | outbound proxy for foreign upstreams |
| `failover` / `retry_rounds` | failover, off by default |
| `retention_days` | usage record retention (default 365) |
| `server.admin_key` | protects `/api/admin/*` when set |
| `alerts.*` | quota alert thresholds, cooldown, desktop / webhook |

## Billing semantics

OpenAI `prompt_tokens` **includes** cached tokens — billing both in full double-counts.
The gateway normalizes:

```text
input_tokens  = prompt_tokens - cached_tokens    # uncached, input price
cache_read    = cached_tokens                    # cache price (usually 10-20x cheaper)
cache_write   = cache_creation_input_tokens      # cache-write price
output_tokens = completion_tokens (reasoning tokens folded in)
```

Prompt caching dominates long-context cost (measured 30-55x in practice); the gateway keeps
cache-routing session keys stable for upstreams that need them (OpenCode Zen).

## Tests

```bash
python -m unittest discover -s tests
```

Covers: SSE compat normalization, request rewriting, the `/api/logs` ledger, cost breakdown,
key fallback, retention, incremental collectors, admin auth, streaming e2e with a mock upstream.

## License

[MIT](LICENSE)
