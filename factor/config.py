"""
Central configuration for Factor.
Edit presets here; secrets and toggles come from env.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
# When PORT is set (Render), bind 0.0.0.0 unless HOST overrides.
_PORT_ENV = os.getenv("PORT")
PORT = int(_PORT_ENV or os.getenv("FACTOR_PORT") or "8090")
if _PORT_ENV:
    HOST = os.getenv("HOST", "0.0.0.0")
else:
    HOST = os.getenv("HOST", "127.0.0.1")

# ---------------------------------------------------------------------------
# Safety / broker
# ---------------------------------------------------------------------------
# Master switch — false by default so restarts never surprise-trade.
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Only real broker path: cTrader Open API (any cTrader broker).
BROKER = (os.getenv("BROKER") or "ctrader").strip().lower()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# ---------------------------------------------------------------------------
# cTrader Open API (demo/live hosts — Spotware; works with any cTrader broker)
# Register app: https://openapi.ctrader.com/
# ---------------------------------------------------------------------------
CTRADER_CLIENT_ID = os.getenv("CTRADER_CLIENT_ID", "").strip()
CTRADER_CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET", "").strip()
CTRADER_ACCESS_TOKEN = os.getenv("CTRADER_ACCESS_TOKEN", "").strip()
CTRADER_ACCOUNT_ID = os.getenv("CTRADER_ACCOUNT_ID", "").strip()
# demo → demo.ctraderapi.com · live → live.ctraderapi.com
_host_raw = (os.getenv("CTRADER_HOST") or "demo").strip().lower()
if _host_raw in ("live", "prod", "production"):
    CTRADER_HOST = "live.ctraderapi.com"
elif "." in _host_raw:
    CTRADER_HOST = _host_raw  # full hostname override
else:
    CTRADER_HOST = "demo.ctraderapi.com"
# Log order payload without sending (local safety while wiring secrets)
CTRADER_DRY_RUN = os.getenv("CTRADER_DRY_RUN", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Protects /api/trades and /api/kill when set (recommended on Render).
# Also accepted as KILL_TOKEN for backwards compatibility.
FACTOR_ADMIN_TOKEN = (
    os.getenv("FACTOR_ADMIN_TOKEN") or os.getenv("KILL_TOKEN") or ""
).strip()
KILL_TOKEN = FACTOR_ADMIN_TOKEN  # alias

# ---------------------------------------------------------------------------
# Reaktor catch-up + backup poll (important on Render after restarts)
# ---------------------------------------------------------------------------
REAKTOR_BASE_URL = (os.getenv("REAKTOR_BASE_URL") or "").rstrip("/")
REAKTOR_API_KEY = os.getenv("REAKTOR_API_KEY", "").strip()
CATCHUP_ON_START = os.getenv("CATCHUP_ON_START", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Seconds between pull-API polls. 0 = off. Default 60 so missed webhooks recover.
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC") or "60")

# ---------------------------------------------------------------------------
# What to trade (configurable — change without code edits)
# ---------------------------------------------------------------------------
def _csv_list(raw: Optional[str], default: List[str]) -> List[str]:
    if raw is None or not str(raw).strip():
        return list(default)
    return [p.strip() for p in str(raw).split(",") if p.strip()]


# Defaults: range expansion only, gold only. Expand when ready.
TRADE_KINDS = _csv_list(os.getenv("TRADE_KINDS"), ["candle"])
TRADE_SYMBOLS = _csv_list(os.getenv("TRADE_SYMBOLS"), ["XAU/USD"])

# ---------------------------------------------------------------------------
# Risk limits
# ---------------------------------------------------------------------------
# One open trade at a time (any symbol). Further Reaktor alerts are ignored.
MAX_OPEN_TOTAL = int(os.getenv("MAX_OPEN_TOTAL") or "1")
MAX_OPEN_PER_SYMBOL = int(os.getenv("MAX_OPEN_PER_SYMBOL") or "1")
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY") or "10")
# Cooldown optional; open-position gate is the main concurrency control.
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC") or "0")
# Only place orders if alert is fresher than this (seconds since triggered_at).
# Catch-up / history and delayed poll pulls will log and skip — no order.
MAX_ALERT_AGE_SEC = float(os.getenv("MAX_ALERT_AGE_SEC") or "5")

# ---------------------------------------------------------------------------
# Memory (Render 512MB) — logging + soft limits, not hard cgroups
# ---------------------------------------------------------------------------
MEMORY_WARN_MB = float(os.getenv("MEMORY_WARN_MB") or "380")
MEMORY_LOG_INTERVAL_SEC = float(os.getenv("MEMORY_LOG_INTERVAL_SEC") or "900")
# Max alerts pulled per catch-up/poll request (avoids huge JSON spikes)
CATCHUP_LIMIT = int(os.getenv("CATCHUP_LIMIT") or "50")
# Cap stored trade detail_json size (bytes)
TRADE_DETAIL_MAX_BYTES = int(os.getenv("TRADE_DETAIL_MAX_BYTES") or "4096")

# ---------------------------------------------------------------------------
# Supabase (multi-user)
# ---------------------------------------------------------------------------
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
# Encrypt cTrader tokens at rest (Fernet key or any passphrase)
FACTOR_TOKEN_ENC_KEY = os.getenv("FACTOR_TOKEN_ENC_KEY", "").strip()
# Sign browser session cookies
FACTOR_SESSION_SECRET = os.getenv("FACTOR_SESSION_SECRET", "").strip()
# cTrader OAuth app redirect (e.g. https://factor-xxx.onrender.com/auth/ctrader/callback)
CTRADER_REDIRECT_URI = (os.getenv("CTRADER_REDIRECT_URI") or "").strip()
# Multi-user fan-out mode (big-bang rewrite). When true, engine uses Supabase users.
MULTI_USER = os.getenv("MULTI_USER", "false").lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Persistence (legacy SQLite — de-dupe fallback / local dev)
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("FACTOR_DATA_DIR") or "data")
DB_PATH = Path(os.getenv("FACTOR_DB_PATH") or str(DATA_DIR / "factor.db"))

# ---------------------------------------------------------------------------
# SL / TP presets — MT-style points (same idea as Reaktor points.py)
# XAU: 1 point = 0.01 price · EUR: 0.00001 · JPY: 0.001
# Range (kind=candle): 0.01 lot, SL/TP 1000 pts each ($10 on gold).
# ---------------------------------------------------------------------------
PRESETS: Dict[str, Dict[str, Any]] = {
    "XAU/USD": {
        "volume_lots": 0.01,
        "sl_points": 1000,
        "tp_points": 1000,
        "point_size": 0.01,
        "broker_symbol": "XAUUSD",
        # cTrader: 1.0 lot = this many base units (gold often 100 oz)
        "lot_units": 100,
    },
    "EUR/USD": {
        "volume_lots": 0.01,
        "sl_points": 150,
        "tp_points": 300,
        "point_size": 0.00001,
        "broker_symbol": "EURUSD",
        "lot_units": 100_000,
    },
    "USD/JPY": {
        "volume_lots": 0.01,
        "sl_points": 150,
        "tp_points": 300,
        "point_size": 0.001,
        "broker_symbol": "USDJPY",
        "lot_units": 100_000,
    },
}

# Optional overrides by alert kind (tune spikes later without changing defaults)
# Example:
# PRESET_OVERRIDES = {
#     "spike": {"XAU/USD": {"sl_points": 200, "tp_points": 400}},
# }
PRESET_OVERRIDES: Dict[str, Dict[str, Dict[str, Any]]] = {}


def _parse_json_env(name: str) -> Any:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"⚠️  {name} is not valid JSON — ignored")
        return None


# Allow env to replace presets without editing code
_env_presets = _parse_json_env("PRESETS_JSON")
if isinstance(_env_presets, dict):
    PRESETS.update(_env_presets)

_env_overrides = _parse_json_env("PRESET_OVERRIDES_JSON")
if isinstance(_env_overrides, dict):
    PRESET_OVERRIDES.update(_env_overrides)
