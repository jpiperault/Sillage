"""Application FastAPI : API, santé, fichiers web, cycle de vie."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, log, paper, security
from .api import router
from .config import ConfigError, Settings, load_settings
from .ingest.prices import PriceClient
from .jobs import Ingestor

_log = log.get("main")
_access = log.get("access")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _web_dist() -> Path | None:
    candidates = [os.environ.get("WEB_DIST"), "/app/web/dist", str(Path(__file__).resolve().parents[2] / "web" / "dist")]
    for c in candidates:
        if c and (Path(c) / "index.html").is_file():
            return Path(c)
    return None


async def _bootstrap_admin(settings: Settings, pool) -> None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id, is_admin FROM users WHERE email=%s", (settings.admin_email,))
        row = await cur.fetchone()
        if row:
            if not row["is_admin"]:
                await conn.execute("UPDATE users SET is_admin=true WHERE id=%s", (row["id"],))
                _log.info("compte administrateur promu : %s", settings.admin_email)
        elif settings.admin_password:
            cur = await conn.execute(
                "INSERT INTO users(email, password_hash, is_admin) VALUES (%s,%s,true) RETURNING id",
                (settings.admin_email, security.hash_password(settings.admin_password)),
            )
            uid = (await cur.fetchone())["id"]
            await paper.ensure_account(conn, uid, settings.paper_start_cash)
            _log.info("compte administrateur créé : %s", settings.admin_email)
        else:
            _log.warning("aucun compte administrateur : définissez ADMIN_PASSWORD pour le créer")
        await conn.commit()


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.run_migrations(settings.database_url)
        pool = db.make_pool(settings.database_url)
        await pool.open(wait=False)
        app.state.pool = pool
        app.state.prices = PriceClient()
        await _bootstrap_admin(settings, pool)
        app.state.ingestor = None
        if settings.ingest_enabled:
            app.state.ingestor = Ingestor(settings, pool, app.state.prices)
            app.state.ingestor.start()
        _log.info("Sillage prêt sur le port %d (collecte %s, Congrès %s)", settings.port,
                  "active" if settings.ingest_enabled else "désactivée",
                  "actif" if settings.congress_enabled else "inactif")
        try:
            yield
        finally:
            t0 = time.monotonic()
            _log.info("arrêt demandé")
            if app.state.ingestor:
                await app.state.ingestor.shutdown(timeout=1.0)
            await app.state.prices.close()
            try:
                await asyncio.wait_for(pool.close(timeout=1.0), 1.5)
            except asyncio.TimeoutError:
                pass
            _log.info("arrêt propre en %.2f s", time.monotonic() - t0)

    app = FastAPI(title="Sillage", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.limiter = security.RateLimiter(limit=10, window=600)

    @app.middleware("http")
    async def access_and_headers(request: Request, call_next):
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _log.exception("erreur non gérée sur %s %s", request.method, request.url.path)
            response = JSONResponse({"detail": "Erreur interne."}, status_code=500)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        path = request.url.path
        if not path.startswith("/api/"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        if request.headers.get("x-forwarded-proto", "").lower() == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if not (path == "/health" and response.status_code == 200) and not path.startswith("/assets/"):
            # Journal d'accès maison : jamais d'adresse IP ni de query string.
            _access.info("%s %s %d %.0fms", request.method, path, response.status_code,
                         (time.perf_counter() - t0) * 1000)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Requête invalide : vérifiez les champs envoyés."}, status_code=422)

    @app.get("/health", include_in_schema=False)
    async def health(request: Request):
        try:
            async def ping():
                async with request.app.state.pool.connection(timeout=2) as conn:
                    await conn.execute("SELECT 1")
                    await conn.rollback()
            await asyncio.wait_for(ping(), 2.0)
            return PlainTextResponse("ok")
        except Exception:  # noqa: BLE001
            return PlainTextResponse("base de données indisponible", status_code=503)

    app.include_router(router)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
    async def api_404(rest: str):
        raise HTTPException(404, "Route inconnue.")

    dist = _web_dist()
    if dist is not None:
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        index = dist / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            f = (dist / path).resolve()
            if path and f.is_file() and dist.resolve() in f.parents and f.name != "index.html":
                return FileResponse(f)
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/", include_in_schema=False)
        async def no_web():
            return Response("Sillage : interface web non construite (web/dist absent).", media_type="text/plain")

    return app


def main() -> None:
    log.setup_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        _log.error("démarrage impossible : %s", exc)
        sys.exit(2)

    import uvicorn

    app = create_app(settings)
    config = uvicorn.Config(
        app, host=os.environ.get("HOST", "0.0.0.0"), port=settings.port, proxy_headers=False,
        access_log=False, log_config=None, timeout_graceful_shutdown=2, server_header=False,
    )
    server = uvicorn.Server(config)
    server.run()
