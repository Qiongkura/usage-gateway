"""Local dashboard preview with synthetic data; never loads gateway configuration."""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.stats import router
from app.db import Store
from app.models import UsageRecord
from app.pricing import ModelPrice, PriceTable

tmp = tempfile.TemporaryDirectory(prefix="usage-dashboard-preview-")
store = Store(Path(tmp.name) / "preview.db")
now = int(time.time())
store.insert_usage([
    UsageRecord(ts=now - age, source=source, model=model, client_key="preview",
                input_tokens=inp, output_tokens=out, cache_read=cache, cache_write=write,
                cost_usd=cost, status=200, raw={"request_id": f"preview-{age}"})
    for age, source, model, inp, out, cache, write, cost in [
        (3600, "demo", "model-a", 1000, 500, 800, 400, 0.005),
        (7200, "unknown", "model-b", 200, 100, 300, 0, None),
        (5400, "alternate", "model-a", 300, 150, 50, 0, None),
        (2 * 86400, "demo", "model-a", 2000, 1000, 500, 0, 0.01),
        (5 * 86400, "demo", "model-a", 4000, 2000, 600, 0, 0.02),
        (1800, "preview-long-source-name-for-layout-testing",
         "model-with-an-extraordinarily-long-identifier-for-responsive-layout-verification-1234567890",
         12, 3, 0, 0, None),
    ]
])
if os.environ.get("DASHBOARD_PREVIEW_TINY_COST") == "1":
    store.insert_usage([
        UsageRecord(ts=now - 90, source="tiny-preview", model="tiny-model", client_key="preview",
                    input_tokens=1, status=200, cost_usd=0.0000004,
                    raw={"request_id": "preview-tiny-cost"})
    ])
if os.environ.get("DASHBOARD_PREVIEW_LARGE_COUNTS") == "1":
    store.insert_usage([
        UsageRecord(ts=now - 60, source="bulk-preview", model="model-large", client_key="preview",
                    input_tokens=1000000, output_tokens=200000, status=200,
                    raw={"request_id": f"preview-bulk-{i}"})
        for i in range(13915)
    ])
if os.environ.get("DASHBOARD_PREVIEW_XSS") == "1":
    payload = '<img src=x onerror="window.previewXssExecuted=1">'
    store.insert_usage([
        UsageRecord(ts=now - 60, source=payload, model=payload, client_key=payload,
                    input_tokens=11, output_tokens=7, status=200,
                    raw={"request_id": "preview-xss"})
    ])
if os.environ.get("DASHBOARD_PREVIEW_CLIENT_KEYS") == "1":
    store.insert_usage([
        UsageRecord(ts=now - age, source="demo", model="model-a", client_key=key,
                    input_tokens=10, output_tokens=5, status=200,
                    raw={"request_id": f"preview-client-key-{age}"})
        for age, key in ((61, None), (62, "(direct)"), (63, '<key "&">'))
    ])
if os.environ.get("DASHBOARD_PREVIEW_UNKNOWN_MODELS") == "1":
    store.insert_usage([
        UsageRecord(ts=now - age, source=source, model=model, client_key="preview",
                    input_tokens=tokens, status=200,
                    raw={"request_id": f"preview-unknown-model-{age}"})
        for age, source, model, tokens in (
            (65, "demo", None, 10), (66, "demo", "(unknown)", 20),
            (67, "alternate", None, 30),
        )
    ])
store.insert_usage([
    UsageRecord(ts=now - age, source="demo", model="model-a", client_key="preview",
                input_tokens=0, output_tokens=0, status=200,
                raw={"request_id": f"preview-outcome-{age}"})
    for age in (120, 240, 360)
])
conn = store.read_conn()
for age, result in ((120, "stream_interrupted"), (240, "client_cancelled"), (360, "unknown")):
    conn.execute("UPDATE usage_records SET result=? WHERE ts=?", (result, now - age))
conn.commit()

app = FastAPI()
app.state.store = store
app.state.cfg = SimpleNamespace(
    upstreams=[], prices=PriceTable({"demo": {"model-a": ModelPrice(input=2, output=8, cache_read=0.2)}}),
    stats_key="", admin_key="",
)

if os.environ.get("DASHBOARD_PREVIEW_SLOW_LOGS") == "1":
    slow_log_count = 0

    @app.middleware("http")
    async def delay_second_logs_request(request, call_next):
        global slow_log_count
        if request.url.path == "/api/logs":
            slow_log_count += 1
            if slow_log_count == 2:
                await asyncio.sleep(32)
        return await call_next(request)


if os.environ.get("DASHBOARD_PREVIEW_SLOW_SESSIONS") == "1":
    slow_session_count = 0

    @app.middleware("http")
    async def delay_second_sessions_request(request, call_next):
        global slow_session_count
        if request.url.path == "/api/sessions":
            slow_session_count += 1
            if slow_session_count == 2:
                await asyncio.sleep(32)
        return await call_next(request)


if os.environ.get("DASHBOARD_PREVIEW_SLOW_OVERVIEW") == "1":
    slow_overview_count = 0

    @app.middleware("http")
    async def delay_second_overview_request(request, call_next):
        global slow_overview_count
        if request.url.path == "/api/overview-usage":
            slow_overview_count += 1
            if slow_overview_count == 2:
                await asyncio.sleep(32)
        return await call_next(request)


if os.environ.get("DASHBOARD_PREVIEW_SLOW_QUOTA") == "1":
    @app.middleware("http")
    async def delay_quota_request(request, call_next):
        if request.url.path == "/api/quota":
            await asyncio.sleep(15)
        return await call_next(request)


if os.environ.get("DASHBOARD_PREVIEW_REFRESH_FAILURE") == "1":
    refresh_overview_count = 0

    @app.middleware("http")
    async def simulate_refresh_failure(request, call_next):
        global refresh_overview_count
        if request.url.path == "/api/overview-usage":
            refresh_overview_count += 1
            if refresh_overview_count > 1:
                return JSONResponse({"detail": "simulated failure"}, status_code=503)
        if request.url.path == "/api/alerts":
            return JSONResponse({"enabled": True, "items": [{
                "level": "critical", "source": "demo", "account": "preview",
                "window": "daily", "remaining_pct": 5,
            }]})
        return await call_next(request)


@app.get("/api/quota")
def preview_quota():
    return []


app.include_router(router)
web = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=web), name="static")


@app.get("/")
def index():
    return FileResponse(web / "index.html")


@app.on_event("shutdown")
def shutdown():
    store.close()
    tmp.cleanup()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9004, log_level="warning")
