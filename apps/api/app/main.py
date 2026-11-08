import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine
from app.routers import admin, auth, gateway, keys, models, projects, teams, users

settings = get_settings()
app = FastAPI(title=f"{settings.app_name} API", version="0.1.0")

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
        return JSONResponse(status_code=exc.status_code, content={"error": d["openai"]})
    if isinstance(d, dict) and "code" in d:
        return _envelope(request, exc.status_code, d["code"], d["message"], d.get("details"))
    return _envelope(request, exc.status_code, f"HTTP_{exc.status_code}", str(d))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errs = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return _envelope(request, 422, "VALIDATION_ERROR", "Request validation failed.",
                     {"errors": errs})


for r in (auth.router, users.router, teams.router, projects.router, admin.router,
          keys.router, models.router):
    app.include_router(r, prefix="/api/v1")
app.include_router(gateway.router)


@app.get("/health/live", tags=["health"])
def live():
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def ready():
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}
