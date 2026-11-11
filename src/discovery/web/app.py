"""Single-origin local web application, sharing the existing recommendation core."""

import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from discovery.catalog.listenbrainz import load_with_listeners
from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT, load_project_env
from discovery.contracts import Catalog
from discovery.llm.budget import BudgetLedger
from discovery.llm.client import OpenRouterClient, OpenRouterSettings
from discovery.storage import Store
from discovery.training.serving import load_optional_artifact

from . import artwork, auth, profiles


def runtime_dir() -> Path:
    path = Path(os.environ.get("DISCOVERY_DB", "runtime/discovery.sqlite3"))
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve().parent


def default_data_dir() -> Path:
    return runtime_dir() / "web"


def create_app(data_dir: Path | None = None, catalog: Catalog | None = None,
               client_factory=None, *, allowed_origins: list[str] | None = None) -> FastAPI:
    load_project_env()
    from . import chat

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for path in app.state.data_dir.glob("users/*/discovery.sqlite3"):
            chat.recover_pending(Store(path))
        yield

    app = FastAPI(title="Music Discovery", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
    app.state.data_dir.mkdir(parents=True, exist_ok=True)
    app.state.data_dir.chmod(0o700)
    app.state.accounts = auth.Accounts(app.state.data_dir / "accounts.sqlite3")
    if catalog is None:
        catalog_path = Path(os.environ.get("DISCOVERY_CATALOG", DEFAULT_CATALOG))
        if not catalog_path.is_absolute():
            catalog_path = PROJECT_ROOT / catalog_path
        catalog = load_with_listeners(catalog_path)
    app.state.catalog = catalog
    app.state.artwork_cache = artwork.ArtworkCache(app.state.data_dir / "artwork.sqlite3")
    # Artwork can wait on slow external hosts. Never occupy the shared API worker
    # pool with dozens of queued album lookups while history/auth need a thread.
    app.state.artwork_limiter = anyio.CapacityLimiter(2)
    app.state.message_limiter = anyio.CapacityLimiter(4)
    app.state.artifact, _ = load_optional_artifact(os.environ.get("DISCOVERY_MODEL_PATH"))
    ledger_path = runtime_dir() / "llm-budget.sqlite3"

    def default_client_factory():
        settings = OpenRouterSettings.from_env()
        settings = replace(settings, request_cost_limit=min(settings.request_cost_limit, .01),
                           daily_cost_limit=min(settings.daily_cost_limit, .10),
                           account_daily_cost_limit=min(settings.account_daily_cost_limit
                                                        if settings.account_daily_cost_limit is not None
                                                        else settings.daily_cost_limit, .10))
        return OpenRouterClient(settings, BudgetLedger(ledger_path))

    app.state.client_factory = client_factory or default_client_factory
    app.state.ledger_path = ledger_path
    explicit_origins = set(allowed_origins or [])
    explicit_hosts = {urlsplit(origin).hostname for origin in explicit_origins}

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1"} | explicit_hosts:
            return JSONResponse(status_code=400, content={"detail": "Недопустимый адрес сервера."})
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin", "")
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if origin not in ({expected} | explicit_origins):
                return JSONResponse(status_code=403, content={"detail": "Запрос должен исходить с этого сайта."})
        if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH"}:
            # Bound the body before FastAPI decodes JSON, including chunked requests.
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 32768:
                    return JSONResponse(status_code=413, content={"detail": "Запрос слишком большой."})
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Never echo validation inputs: registration bodies contain passwords.
        detail = ("Проверьте поля формы: логин 3–32 символа, пароль 12–128."
                  if request.url.path.startswith("/api/auth/") else
                  "Проверьте параметры запроса: значение отсутствует или выходит за допустимые пределы.")
        return JSONResponse(status_code=422, content={"detail": detail})

    app.include_router(auth.router)
    app.include_router(profiles.router)
    app.include_router(chat.router)
    app.include_router(artwork.router)
    dist = PROJECT_ROOT / "frontend/dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Не найдено."})
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
        return JSONResponse(status_code=503, content={"detail": "Сначала соберите интерфейс: cd frontend && npm ci && npm run build"})

    # Serve the agreed artifact used by generated browser types and contract tests.
    app.openapi_schema = json.loads((PROJECT_ROOT / "docs/web-openapi.json").read_text())
    app.openapi = lambda: app.openapi_schema
    return app
