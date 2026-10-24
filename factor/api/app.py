"""
FastAPI app: multi-user Factor — webhook fan-out, dashboard, health, admin kill.
"""

from __future__ import annotations

import json
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from factor import __version__, config
from factor.api.ai_routes import router as ai_router
from factor.auth.routes import router as auth_router
from factor.db.client import supabase_configured
from factor.engine_multi import process_alert_multi
from factor.inbound.auth import verify_signature
from factor.inbound.models import parse_cloudevent
from factor.memory_util import memory_snapshot, memory_watch_loop, rss_mb
from factor.services.breakeven import (
    last_breakeven_status,
    start_breakeven_loop,
    stop_breakeven_loop,
)

# factor/web (static dashboard)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _require_admin(authorization: Optional[str], x_api_key: Optional[str]) -> None:
    """If FACTOR_ADMIN_TOKEN is set, require Bearer or X-Api-Key."""
    token = config.FACTOR_ADMIN_TOKEN
    if not token:
        return
    got = None
    if authorization and authorization.lower().startswith("bearer "):
        got = authorization[7:].strip()
    elif x_api_key:
        got = x_api_key.strip()
    if not got or not secrets.compare_digest(got, token):
        raise HTTPException(status_code=401, detail="admin auth required")


def create_app() -> FastAPI:
    mem_stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        print(
            f"🚀 Factor v{__version__} multi-user "
            f"host={config.CTRADER_HOST} "
            f"trading_enabled={config.TRADING_ENABLED} "
            f"dry_run={config.CTRADER_DRY_RUN} "
            f"supabase={supabase_configured()}"
        )
        mb0 = rss_mb()
        if mb0 is not None:
            print(f"🧠 Startup RSS ≈ {mb0} MB (warn at {config.MEMORY_WARN_MB} MB)")
        if not config.WEBHOOK_SECRET:
            print("⚠️  WEBHOOK_SECRET empty — webhooks will 503 until set")
        if not config.FACTOR_ADMIN_TOKEN:
            print("⚠️  FACTOR_ADMIN_TOKEN unset — /api/kill is open")
        if not supabase_configured():
            print(
                "⚠️  Supabase not configured — webhooks and login will 503 "
                "(set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)"
            )
        if not (config.CTRADER_CLIENT_ID and config.CTRADER_CLIENT_SECRET):
            print(
                "⚠️  cTrader OAuth app incomplete — set CTRADER_CLIENT_ID / "
                "CTRADER_CLIENT_SECRET (see docs/CTRADER.md)"
            )
        if not (
            config.FACTOR_TOKEN_ENC_KEY
            and config.FACTOR_SESSION_SECRET
            and config.CTRADER_REDIRECT_URI
        ):
            print(
                "⚠️  Multi-user secrets incomplete — set FACTOR_TOKEN_ENC_KEY, "
                "FACTOR_SESSION_SECRET, CTRADER_REDIRECT_URI"
            )

        mem_stop.clear()
        threading.Thread(
            target=memory_watch_loop,
            args=(mem_stop,),
            kwargs={
                "interval_sec": float(config.MEMORY_LOG_INTERVAL_SEC),
                "warn_mb": float(config.MEMORY_WARN_MB),
            },
            name="memory-watch",
            daemon=True,
        ).start()

        print("ℹ️  Webhook fan-out only (no single-tenant catch-up / SQLite engine)")
        try:
            start_breakeven_loop()
        except Exception as e:
            print(f"⚠️  Breakeven loop start failed: {e}")

        yield
        mem_stop.set()
        stop_breakeven_loop()

    app = FastAPI(
        title="Factor",
        version=__version__,
        lifespan=lifespan,
        redirect_slashes=False,
    )
    app.include_router(auth_router)
    app.include_router(ai_router)

    if WEB_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(WEB_DIR)),
            name="static",
        )

    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    def favicon():
        """Browsers request /favicon.ico by default."""
        icon = WEB_DIR / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        raise HTTPException(status_code=404, detail="no favicon")

    @app.get("/")
    def root():
        """Dashboard UI when present; else JSON service info."""
        index = WEB_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        return {
            "service": "factor",
            "version": __version__,
            "health": "/api/health",
            "webhook": "POST /hooks/reaktor",
            "login": "/auth/ctrader/login",
            "mode": "multi_user",
        }

    @app.get("/api/info")
    def api_info() -> Dict[str, Any]:
        return {
            "service": "factor",
            "version": __version__,
            "health": "/api/health",
            "webhook": "POST /hooks/reaktor",
            "login": "/auth/ctrader/login",
            "mode": "multi_user",
            "supabase_configured": supabase_configured(),
            "ai_orders": "POST /api/ai/orders",
            "ai_positions": "GET /api/ai/positions",
            "ai_manage": "POST /api/ai/positions/manage",
            "ai_kill": "GET /api/ai/kill",
            "ai_subscribers": "GET /api/ai/subscribers",
        }

    def _health_payload() -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "factor",
            "version": __version__,
            "mode": "multi_user",
            "broker": config.BROKER,
            "ctrader_host": config.CTRADER_HOST,
            "ctrader_oauth_configured": bool(
                config.CTRADER_CLIENT_ID and config.CTRADER_CLIENT_SECRET
            ),
            "ctrader_dry_run": config.CTRADER_DRY_RUN,
            "trading_enabled": config.TRADING_ENABLED,
            "trade_kinds": config.TRADE_KINDS,
            "trade_symbols": config.TRADE_SYMBOLS,
            "breakeven_enabled": bool(getattr(config, "BREAKEVEN_ENABLED", True)),
            "breakeven_fraction": float(getattr(config, "BREAKEVEN_FRACTION", 0.5) or 0.5),
            "crt_weekly_flatten": getattr(
                config, "CRT_WEEKLY_FLATTEN", "FRI 23:45 Europe/Chisinau"
            ),
            "crt_weekly_flatten_enabled": bool(
                getattr(config, "CRT_WEEKLY_FLATTEN_ENABLED", True)
            ),
            "breakeven": last_breakeven_status(),
            "market_open_blackout": bool(getattr(config, "MARKET_OPEN_BLACKOUT", True)),
            "market_open_time": getattr(config, "MARKET_OPEN_TIME", "01:05"),
            "market_open_tz": getattr(config, "MARKET_OPEN_TZ", "Europe/Bucharest"),
            "market_open_blackout_minutes": int(
                getattr(config, "MARKET_OPEN_BLACKOUT_MINUTES", 15) or 15
            ),
            "max_alert_age_sec": float(getattr(config, "MAX_ALERT_AGE_SEC", 5) or 5),
            "max_alert_age_asia_sec": float(
                getattr(config, "MAX_ALERT_AGE_ASIA_SEC", 20) or 20
            ),
            "xau_preset": (config.PRESETS.get("XAU/USD") or {}),
            "webhook_configured": bool(config.WEBHOOK_SECRET),
            "admin_token_configured": bool(config.FACTOR_ADMIN_TOKEN),
            "memory": memory_snapshot(),
            "supabase_configured": supabase_configured(),
            "session_configured": bool(config.FACTOR_SESSION_SECRET),
            "token_enc_configured": bool(config.FACTOR_TOKEN_ENC_KEY),
            "redirect_uri_configured": bool(config.CTRADER_REDIRECT_URI),
        }

    @app.get("/api/health")
    @app.get("/api/health/")
    def health() -> Dict[str, Any]:
        return _health_payload()

    @app.get("/api/trades")
    def list_trades(
        limit: int = 50,
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        """
        Legacy admin endpoint (was SQLite single-tenant trades).
        Multi-user orders live in Supabase — use dashboard /api/me/orders.
        """
        _require_admin(authorization, x_api_key)
        return {
            "count": 0,
            "trades": [],
            "note": "multi_user — use GET /api/me/orders while logged in",
        }

    @app.post("/api/kill")
    async def kill(
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
        x_kill_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        """
        Global process kill: set TRADING_ENABLED=false until restart.
        Per-user kill is POST /api/me/kill on the dashboard.
        """
        if config.FACTOR_ADMIN_TOKEN:
            try:
                _require_admin(authorization, x_api_key)
            except HTTPException:
                if not x_kill_token or not secrets.compare_digest(
                    x_kill_token, config.FACTOR_ADMIN_TOKEN
                ):
                    raise HTTPException(status_code=401, detail="admin auth required")
        config.TRADING_ENABLED = False
        print("🛑 KILL switch: TRADING_ENABLED=false (global)")
        return {"ok": True, "trading_enabled": False}

    @app.post("/hooks/reaktor")
    async def reaktor_hook(
        request: Request,
        x_reaktor_signature: Optional[str] = Header(default=None),
        x_reaktor_event_id: Optional[str] = Header(default=None),
    ):
        raw = await request.body()

        if not config.WEBHOOK_SECRET:
            print("⚠️  WEBHOOK_SECRET not set — rejecting webhooks")
            raise HTTPException(
                status_code=503,
                detail="WEBHOOK_SECRET not configured",
            )

        if not verify_signature(config.WEBHOOK_SECRET, raw, x_reaktor_signature):
            print(
                f"⚠️  bad signature event_id={x_reaktor_event_id} "
                f"body_len={len(raw)}"
            )
            raise HTTPException(status_code=401, detail="invalid signature")

        if not supabase_configured():
            raise HTTPException(
                status_code=503,
                detail="Supabase not configured (required for multi-user Factor)",
            )

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {e}")

        try:
            alert = parse_cloudevent(payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        result = process_alert_multi(alert)
        # Always 200 once signature + parse ok so Reaktor does not hammer retries
        # for policy skips.
        return JSONResponse(status_code=200, content=result)

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        return "User-agent: *\nDisallow: /\n"

    return app
