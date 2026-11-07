# Usage Gateway

[中文](README.md)

[![tests](https://github.com/Qiongkura/usage-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Qiongkura/usage-gateway/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

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

## Screenshots

**Per-call ledger: open any row to see the itemized bill and how much prompt caching saved**

![call bill](docs/screenshots/call-bill.png)

**Overview: summary cards, trends, 365-day activity heatmap**

![overview](docs/screenshots/overview-cards.png)

**Subscription quota cards across accounts (5h / weekly windows with reset times)**

![quota](docs/screenshots/quota.png)

**Warm-paper theme (light / dark / warm — one-click switch)**

![warm theme](docs/screenshots/theme-warm.png)

**Trace waterfall: per-stage latency for slow-request debugging**

![traces](docs/screenshots/traces.png)

**tok/s widget (frameless, always-on-top, draggable / pinnable / closable): live generation speed + last call**

![float widget](docs/screenshots/float-final.png)

## Features

**Proxy core**
- OpenAI & Anthropic compatible (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`, `/v1/models`)
- Strict prefix routing (`alias/model`) — never silently switches channels (protects cheap pools)
- Upstream health circuit breaker with exponential cooldown; optional same-alias failover
- Compat layer: array-content flattening, tool-call chunk normalization, developer-role rewrite,
  strict-schema param whitelisting, auto `include_usage`, non-stream → SSE wrapping
- Connection pooling with 5-minute keepalive
- Ordered multi-key chain per upstream (402 / 429 → next key, 15-minute cooldown); single-key configs still work
- Weekly model-availability probe per upstream, hot-applied without restart (failed models hidden)

**Per-call ledger**
- Every call records: uncached input / cache read / cache write / output (incl. thinking) /
  status / TTFT / total latency / tok-s
- Itemized bill per call: input + cache-read + cache-write + output × time multiplier,
  plus **how much money the prompt cache saved you**
- Call Logs view: filters (time / channel / key / origin / status / model search), summary bar,
  pagination, CSV export
- Costs are always converted at official API list prices; subscription / free channels are tagged
- Upstream-reported costs (e.g. Zen `list_cost`) take precedence; historical records can be backfilled
- Daily rolling database backup (`VACUUM INTO`, keep 7)

**Dashboard**
- Summary cards, composition / cost / request trends, 365-day heatmap, request health timeline
- Compact multi-account quota cards with reset times, quota alerts (desktop + webhook, state machine)
- **Burn-rate projection**: each window's rate in percentage-points/hour, projected remaining at reset;
  a red "exhausts in ~N h/d" warning only when it would actually run dry before resetting.
  No more treating `99% left in a 5h window` as dozens of days
- Request trace waterfall, live tok/s widget (`/float`)
- Light / dark / warm-paper themes + zh / EN UI, both persisted locally

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

## Docker

```bash
docker compose up -d --build
```

Mounts `./data` (ledger database) and `./config.yaml`; on first start the example config is copied automatically — edit your keys and `docker compose restart`.

### Windows: autostart + app window

Drop a silent launcher into your Startup folder (`Win+R` → `shell:startup` → new `usage-gateway.vbs`):

```vbs
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "C:\path\to\usage-gateway"
shell.Run "cmd /c if not exist logs mkdir logs", 0, True
shell.Run "cmd /c uv run python -m app.main >> logs\gateway.log 2>&1", 0, False
```

A second instance exits on the busy port, so running it again is harmless. For a chrome-less app window,
point a desktop shortcut at `msedge --app=http://127.0.0.1:9000/` (or Chrome).

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
| `upstreams[].api_keys` | key chain: `api_key` first, then `api_keys` in order (402/429 → next key, 15-minute cooldown) |
| `upstreams[].no_proxy` | connect to local upstreams directly, bypassing the global proxy |
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

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | dashboard (Overview / Call Logs tabs) |
| `GET /float` | live tok/s widget |
| `GET /api/logs` | per-call ledger (filters + pagination + itemized cost) |
| `GET /api/summary` / `GET /api/usage` / `GET /api/timeseries` | aggregates |
| `GET /api/costs` / `GET /api/pricing` | cost detail / price table |
| `GET /api/speed` | recent call speed (pure generation tok/s, TTFT, total) |
| `GET /api/heatmap` / `GET /api/health-timeline` | 365-day heatmap / request health timeline |
| `GET /api/traces` / `GET /api/traces/{id}` | request traces |
| `GET /api/quota` / `GET /api/alerts` | quota snapshots (with burn projection) / alert state |
| `GET /api/balance` / `GET /api/models/active` / `GET /api/routes` | balance / active models / route table |
| `GET /api/upstreams/health` / `POST /api/upstreams/{name}/reset` | breaker state / manual reset |
| `POST /api/admin/checkpoint` / `POST /api/admin/shutdown` | graceful checkpoint / shutdown (optional admin key) |

`/api/logs` filters: `range`, `source`, `model` (fuzzy), `client_key`, `status=success|error`,
`origin=gateway|collector|all`, `page`, `page_size`.

## Project layout

```text
app/
  main.py            # entry: routing, lifespan, collector wiring
  proxy.py           # forwarding core: compat layer, SSE normalization, metering, pool
  pricing.py         # price table + itemized cost breakdown (multiplier, cache savings)
  db.py              # SQLite (WAL): usage / quota / traces / watermarks / retention
  api/stats.py       # stats API (including the /api/logs ledger)
  scheduler.py       # collection, pruning / backups, burn-rate, weekly model probe
  probe_models.py    # upstream model availability probe (hot-applied)
  collectors/        # read-only collectors (quota + local client logs)
  alerts.py          # alert state machine and notification channels
  health.py          # circuit breaker and load balancing
  tracing.py         # request tracing
  float_app.py       # tok/s widget (pywebview frameless native window)
web/                 # dependency-free SPA dashboard (bundled Chart.js, three themes)
tests/               # unit tests (compat / ledger / burn rate / maintenance)
Dockerfile           # uv base image + healthcheck
docker-compose.yml   # data / config / logs mounts
```

## Tests

```bash
python -m unittest discover -s tests
```

42 cases covering: SSE compat normalization, request rewriting, the `/api/logs` ledger,
cost breakdown, multi-key fallback, streaming proxy, burn-rate and pre-reset warnings,
retention / backups, incremental collectors, admin auth.

## Known limitations

- With duplicate model names the first upstream in config wins; streaming usage depends on the
  upstream, and missing token counts are estimated and flagged
- Burn rate is a linear extrapolation from recent samples; treat it as an estimate, with the
  projected-at-reset value for windows that have a reset time
- No user system: designed for single-user local use; add reverse-proxy auth before exposing it
- Developed on Windows; Linux/macOS should work (desktop notifications are Windows-only)

## License

[MIT](LICENSE)
