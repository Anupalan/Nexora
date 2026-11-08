import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import ops
from app.config import get_settings
from app.db import get_engine, session_scope
from app.inference import dbcall, get_scheduler, sweep_once
from app.litellm import get_litellm
from app.routers import (
    admin,
    auth,
    gateway,
    keys,
    models,
    projects,
    queue,
    quotas,
    teams,
    usage,
    users,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup: rebuild Redis counters if Redis was wiped; run the crash-recovery sweeper."""
    sched = get_scheduler()
    stop = asyncio.Event()

    async def sweeper():
        first = True
        while not stop.is_set():
            try:
                if first:
                    from app import quota
                    await dbcall(lambda db: None)  # warm the engine
                    with session_scope() as db:
                        await quota.ensure_counters(sched.redis, db)
                    first = False
                await sweep_once(sched)
            except Exception:  # noqa: BLE001 - recovery must never take the API down
                logging.getLogger("nexora").exception("sweeper iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), settings.sweeper_interval_seconds)
            except TimeoutError:
                pass

    async def ops_loop():
        litellm = get_litellm()
        while not stop.is_set():
            try:
                await ops.ops_cycle(sched, litellm)
            except Exception:  # noqa: BLE001 - monitoring must never take the API down
                logging.getLogger("nexora").exception("ops cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), settings.health_check_interval_seconds)
            except TimeoutError:
                pass

    tasks = [asyncio.create_task(sweeper())]
    if settings.ops_loop_enabled:
        tasks.append(asyncio.create_task(ops_loop()))
    yield
    stop.set()
    for t in tasks:
        t.cancel()


app = FastAPI(title=f"{settings.app_name} API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_id(request: Request, call_next):
    cid = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = cid
    response = await call_next(request)
    response.headers["x-request-id"] = cid
    return response


def _envelope(request: Request, status: int, code: str, message: str, details=None):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message,
                           "request_id": getattr(request.state, "request_id", None),
                           "details": details or {}}},
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    d = exc.detail
    if isinstance(d, dict) and "openai" in d:  # OpenAI-shaped errors on /v1
        return JSONResponse(status_code=exc.status_code, content={"error": d["openai"]},
                            headers=exc.headers)
    if isinstance(d, dict) and "code" in d:
        return _envelope(request, exc.status_code, d["code"], d["message"], d.get("details"))
    return _envelope(request, exc.status_code, f"HTTP_{exc.status_code}", str(d))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errs = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return _envelope(request, 422, "VALIDATION_ERROR", "Request validation failed.",
                     {"errors": errs})


for r in (auth.router, users.router, teams.router, projects.router, admin.router,
          keys.router, models.router, quotas.router, queue.router, usage.router):
    app.include_router(r, prefix="/api/v1")
app.include_router(gateway.router)
app.include_router(admin.metrics_router)


@app.get("/health/live", tags=["health"])
def live():
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def ready():
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        await get_scheduler().redis.ping()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}
