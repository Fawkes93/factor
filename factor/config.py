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
# paper | ctrader (ctrader not implemented in phase 1)
BROKER = (os.getenv("BROKER") or "paper").strip().lower()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

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
MAX_OPEN_PER_SYMBOL = int(os.getenv("MAX_OPEN_PER_SYMBOL") or "1")
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY") or "10")
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC") or "300")

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("FACTOR_DATA_DIR") or "data")
DB_PATH = Path(os.getenv("FACTOR_DB_PATH") or str(DATA_DIR / "factor.db"))

# ---------------------------------------------------------------------------
# SL / TP presets — MT-style points (same idea as Reaktor points.py)
# XAU: 1 point = 0.01 price · EUR: 0.00001 · JPY: 0.001
# ---------------------------------------------------------------------------
PRESETS: Dict[str, Dict[str, Any]] = {
    "XAU/USD": {
        "volume_lots": 0.01,
        "sl_points": 300,
        "tp_points": 600,
        "point_size": 0.01,
        "broker_symbol": "XAUUSD",
    },
    "EUR/USD": {
        "volume_lots": 0.01,
        "sl_points": 150,
        "tp_points": 300,
        "point_size": 0.00001,
        "broker_symbol": "EURUSD",
    },
    "USD/JPY": {
        "volume_lots": 0.01,
        "sl_points": 150,
        "tp_points": 300,
        "point_size": 0.001,
        "broker_symbol": "USDJPY",
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
