"""
FastAPI app: webhook, health, trades, kill switch.
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
from factor.auth.routes import router as auth_router
from factor.broker.factory import get_broker
from factor.catchup import last_poll_status, run_catchup, start_poll_loop, stop_poll_loop
from factor.services.breakeven import (
    last_breakeven_status,
    start_breakeven_loop,
    stop_breakeven_loop,
)
from factor.db.client import supabase_configured
from factor.engine import process_alert
from factor.engine_multi import process_alert_multi
from factor.inbound.auth import verify_signature
from factor.inbound.models import parse_cloudevent
from factor.memory_util import memory_snapshot, memory_watch_loop, rss_mb
from factor.store.state import StateStore

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


def create_app(
    *,
    store: Optional[StateStore] = None,
    skip_catchup: bool = False,
) -> FastAPI:
    state_store = store or StateStore(config.DB_PATH)
    broker = get_broker()

    mem_stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = state_store
        app.state.broker = broker
        print(
            f"🚀 Factor v{__version__} broker={config.BROKER} "
            f"host={config.CTRADER_HOST} "
            f"trading_enabled={config.TRADING_ENABLED} "
            f"dry_run={config.CTRADER_DRY_RUN} "
            f"kinds={config.TRADE_KINDS} symbols={config.TRADE_SYMBOLS}"
        )
        mb0 = rss_mb()
        if mb0 is not None:
            print(f"🧠 Startup RSS ≈ {mb0} MB (warn at {config.MEMORY_WARN_MB} MB)")
        if not config.WEBHOOK_SECRET:
            print("⚠️  WEBHOOK_SECRET empty — webhooks will 503 until set")
        if not config.FACTOR_ADMIN_TOKEN:
            print(
                "⚠️  FACTOR_ADMIN_TOKEN unset — /api/trades and /api/kill are open"
            )
        if not (
            config.CTRADER_CLIENT_ID
            and config.CTRADER_CLIENT_SECRET
            and config.CTRADER_ACCESS_TOKEN
            and config.CTRADER_ACCOUNT_ID
        ):
            print(
                "⚠️  cTrader credentials incomplete — orders will fail until set "
                "(see docs/CTRADER.md)"
            )
        # Periodic RSS (Render 512MB) — same idea as Reaktor
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
        if config.MULTI_USER:
            print(
                "ℹ️  MULTI_USER=true — boot catch-up trading disabled "
                "(webhooks fan-out only; freshness still enforced)"
            )
            try:
                start_breakeven_loop()
            except Exception as e:
                print(f"⚠️  Breakeven loop start failed: {e}")
        elif not skip_catchup:
            try:
                run_catchup(state_store, broker)
            except Exception as e:
                print(f"⚠️  Catch-up error (continuing): {e}")
            try:
                start_poll_loop(state_store, broker)
            except Exception as e:
                print(f"⚠️  Poll loop start failed: {e}")
        yield
        mem_stop.set()
        stop_poll_loop()
        stop_breakeven_loop()

    # Serve /api/health and /api/health/ the same — no 307 redirect spam from Render probes
    app = FastAPI(
        title="Factor",
        version=__version__,
        lifespan=lifespan,
        redirect_slashes=False,
    )
    # Available immediately (tests + early requests before lifespan)
    app.state.store = state_store
    app.state.broker = broker
    app.include_router(auth_router)

    if WEB_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(WEB_DIR)),
            name="static",
        )

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
            "multi_user": bool(config.MULTI_USER),
        }

    @app.get("/api/info")
    def api_info() -> Dict[str, Any]:
        return {
            "service": "factor",
            "version": __version__,
            "health": "/api/health",
            "webhook": "POST /hooks/reaktor",
            "login": "/auth/ctrader/login",
            "multi_user": bool(config.MULTI_USER),
            "supabase_configured": supabase_configured(),
        }

    def _health_payload() -> Dict[str, Any]:
        st = app.state.store
        return {
            "ok": True,
            "service": "factor",
            "version": __version__,
            "broker": config.BROKER,
            "ctrader_host": config.CTRADER_HOST,
            "ctrader_configured": bool(
                config.CTRADER_CLIENT_ID
                and config.CTRADER_CLIENT_SECRET
                and config.CTRADER_ACCESS_TOKEN
                and config.CTRADER_ACCOUNT_ID
            ),
            "ctrader_dry_run": config.CTRADER_DRY_RUN,
            "trading_enabled": config.TRADING_ENABLED,
            "trade_kinds": config.TRADE_KINDS,
            "trade_symbols": config.TRADE_SYMBOLS,
            "breakeven_enabled": bool(getattr(config, "BREAKEVEN_ENABLED", True)),
            "breakeven_fraction": float(getattr(config, "BREAKEVEN_FRACTION", 0.5) or 0.5),
            "breakeven": last_breakeven_status(),
            "market_open_blackout": bool(getattr(config, "MARKET_OPEN_BLACKOUT", True)),
            "market_open_time": getattr(config, "MARKET_OPEN_TIME", "01:05"),
            "market_open_tz": getattr(config, "MARKET_OPEN_TZ", "Europe/Bucharest"),
            "market_open_blackout_minutes": int(
                getattr(config, "MARKET_OPEN_BLACKOUT_MINUTES", 15) or 15
            ),
            "max_open_total": int(getattr(config, "MAX_OPEN_TOTAL", 1) or 1),
            "max_alert_age_sec": float(getattr(config, "MAX_ALERT_AGE_SEC", 5) or 5),
            "xau_preset": (config.PRESETS.get("XAU/USD") or {}),
            "webhook_configured": bool(config.WEBHOOK_SECRET),
            "reaktor_pull_configured": bool(
                config.REAKTOR_BASE_URL and config.REAKTOR_API_KEY
            ),
            "poll_interval_sec": config.POLL_INTERVAL_SEC,
            "admin_token_configured": bool(config.FACTOR_ADMIN_TOKEN),
            "poll": last_poll_status(),
            "stats": st.stats(),
            "memory": memory_snapshot(),
            "multi_user": bool(config.MULTI_USER),
            "supabase_configured": supabase_configured(),
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
        _require_admin(authorization, x_api_key)
        trades = app.state.store.list_trades(limit=limit)
        return {"count": len(trades), "trades": trades}

    @app.post("/api/kill")
    async def kill(
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
        x_kill_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        """
        Disable trading in-process until process restart / env change.
        """
        # Accept admin token or legacy X-Kill-Token
        if config.FACTOR_ADMIN_TOKEN:
            try:
                _require_admin(authorization, x_api_key)
            except HTTPException:
                if not x_kill_token or not secrets.compare_digest(
                    x_kill_token, config.FACTOR_ADMIN_TOKEN
                ):
                    raise HTTPException(status_code=401, detail="admin auth required")
        config.TRADING_ENABLED = False
        print("🛑 KILL switch: TRADING_ENABLED=false")
        return {"ok": True, "trading_enabled": False}

    @app.post("/api/catchup")
    def manual_catchup(
        authorization: Optional[str] = Header(default=None),
        x_api_key: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        """Force a pull from Reaktor (admin)."""
        _require_admin(authorization, x_api_key)
        return run_catchup(app.state.store, app.state.broker, force=True)

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

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid JSON: {e}")

        try:
            alert = parse_cloudevent(payload)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        if config.MULTI_USER:
            if not supabase_configured():
                raise HTTPException(
                    status_code=503,
                    detail="MULTI_USER requires Supabase configuration",
                )
            result = process_alert_multi(alert)
        else:
            result = process_alert(
                alert,
                store=app.state.store,
                broker=app.state.broker,
            )
        # Always 200 once signature + parse ok so Reaktor does not hammer retries
        # for policy skips.
        return JSONResponse(status_code=200, content=result)

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        return "User-agent: *\nDisallow: /\n"

    return app
