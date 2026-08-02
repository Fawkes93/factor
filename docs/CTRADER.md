# cTrader Open API (demo)

Factor places **real API orders** on a **cTrader demo** account (fake money) via Spotware Open API.  
Works with **any cTrader broker** (IC Markets is a common example — not required).  
Same API as live — later you only switch host + account.

Official docs: [help.ctrader.com/open-api](https://help.ctrader.com/open-api/)  
App console: [openapi.ctrader.com](https://openapi.ctrader.com/)

---

## 1. Prerequisites

1. IC Markets account with **cTrader** (not only MT5).
2. Open a **cTrader demo** account in cTrader / IC Markets.
3. Register an **Open API application** at [openapi.ctrader.com](https://openapi.ctrader.com/):
   - Create app → note **Client ID** and **Client Secret**
   - Complete OAuth and grant **trading** permission for your demo account
   - Copy **Access Token**
   - Copy **ctidTraderAccountId** (numeric account id)

Account id is visible in the Open API “get accounts by token” response, or in some cTrader account details UIs.

---

## 2. Factor environment (Render)

| Variable | Example | Notes |
|----------|---------|--------|
| `BROKER` | `ctrader` | Default now |
| `CTRADER_HOST` | `demo` | Use `live` only when ready for real money |
| `CTRADER_CLIENT_ID` | from console | |
| `CTRADER_CLIENT_SECRET` | from console | secret |
| `CTRADER_ACCESS_TOKEN` | OAuth token | expires — refresh later if needed |
| `CTRADER_ACCOUNT_ID` | `12345678` | demo account id |
| `CTRADER_DRY_RUN` | `false` | `true` = log only, no order |
| `TRADING_ENABLED` | `false` first | flip on after health looks good |

Also keep your existing webhook / Reaktor vars.

---

## 3. Check health

```bash
curl -sS https://YOUR-FACTOR.onrender.com/api/health | python3 -m json.tool
```

Want:

- `"broker": "ctrader"`
- `"ctrader_host": "demo.ctraderapi.com"`
- `"ctrader_configured": true`
- `"ctrader_dry_run": false` when ready
- `"trading_enabled": true` only when you want orders

---

## 4. Safe first order

1. Set `CTRADER_DRY_RUN=true` → signed webhook test → should log order math, not hit broker.
2. Set `CTRADER_DRY_RUN=false`, `TRADING_ENABLED=true`, `MAX_OPEN_PER_SYMBOL=1`.
3. Fire signed test with a **new** `alert_id` while **gold market is open** (weekends often reject).
4. Confirm position in **cTrader demo** with SL/TP attached.

Market closed → broker may reject; that’s normal.

---

## 5. Volume / SL / TP

Configured in `factor/config.py` → `PRESETS`:

| Field | Meaning |
|-------|---------|
| `volume_lots` | Your size (e.g. `0.01`) |
| `lot_units` | Base units per 1.0 lot (XAU often `100`) |
| `sl_points` / `tp_points` | MT-style points × `point_size` → price distance |
| `broker_symbol` | Name on cTrader (e.g. `XAUUSD`) — must match the symbol list |

cTrader protocol volume = `lots × lot_units × 100`.  
Relative SL/TP = price distance × `100_000`.

If the symbol name differs on IC Markets (e.g. `XAUUSD.a`), set:

```bash
PRESETS_JSON={"XAU/USD":{"broker_symbol":"XAUUSD.a","volume_lots":0.01,"sl_points":300,"tp_points":600,"point_size":0.01,"lot_units":100}}
```

---

## 6. Live (later)

1. Long clean run on **demo**.
2. Create live token / account with trade scope.
3. Set `CTRADER_HOST=live` and live `CTRADER_ACCOUNT_ID`.
4. Keep tiny size and limits.

Demo and live are **separate hosts** — you cannot mix on one connection.

---

## 7. Token expiry

Access tokens can expire. If orders start failing with auth errors:

1. Refresh token via Open API console / OAuth refresh flow.
2. Update `CTRADER_ACCESS_TOKEN` on Render and restart.

A durable refresh-token loop can be added later.
