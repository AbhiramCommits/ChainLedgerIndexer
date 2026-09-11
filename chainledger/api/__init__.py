import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from chainledger.api.routes import router
from chainledger.metrics import API_REQUEST_LATENCY

app = FastAPI(title="chainledger", version="0.2.0")
app.include_router(router)


@app.middleware("http")
async def latency_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path == "/metrics":
        return await call_next(request)
    started = time.perf_counter()
    try:
        return await call_next(request)
    finally:
        API_REQUEST_LATENCY.labels(request.method, request.url.path).observe(
            time.perf_counter() - started
        )


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
