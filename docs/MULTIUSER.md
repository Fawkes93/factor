# Factor multi-user

Factor is **multi-user only**: one Reaktor webhook fans out to logged-in users with enabled alert prefs and linked cTrader accounts.

## Dashboard

Open `https://your-factor.onrender.com/` after deploy.

| UI | API |
|----|-----|
| Connect cTrader | `/auth/ctrader/login` |
| Home / status | `GET /api/me` |
| Alert settings | `PUT /api/me/prefs` → `PUT /api/me/prefs` |
| Orders | `GET /api/me/orders` |
| Kill / Resume | `POST /api/me/kill`, `/api/me/resume` |
| Log out | `POST /auth/logout` |

Static assets: `/static/css/app.css`, `/static/js/app.js`.

## Fan-out

1. Reaktor webhook verified (HMAC)  
2. Load candidates: enabled pref for `(kind, symbol)`, trading on, not killed, has connection  
3. Per user: **claim** `(user_id, alert_id)` → freshness → market-open blackout → daily cap → cTrader open count vs `max_concurrent` → place → `factor_orders` row  
4. Background poll may move SL to **breakeven** at half TP  

No single-tenant SQLite engine and no Reaktor catch-up trading — **webhooks only**.

## Kill switch

| Path | Effect |
|------|--------|
| `POST /api/me/kill` | Per-user: close open cTrader positions, block new trades |
| `POST /api/me/resume` | Clear per-user kill (does not re-open positions) |
| `POST /api/kill` | Global process: `TRADING_ENABLED=false` until restart (admin) |

## OAuth routes

| Path | Purpose |
|------|---------|
| `GET /auth/ctrader/login` | Start OAuth |
| `GET /auth/ctrader/callback` | Exchange code, store encrypted tokens, set session |
| `POST /auth/logout` | Clear session |
| `GET /api/me` | Current user + connection (no secrets) + prefs |

Redirect URI must match Open API app settings exactly (`CTRADER_REDIRECT_URI`).

## Supabase setup

1. SQL Editor → run `sql/001_factor_multiuser.sql`  
2. Then `sql/002_breakeven.sql` (optional BE pref column)  
3. Project Settings → API → **service_role** key (server only)

## Required env (Render)

```bash
WEBHOOK_SECRET=...
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
FACTOR_TOKEN_ENC_KEY=...      # Fernet or long passphrase
FACTOR_SESSION_SECRET=...     # cookie signing
CTRADER_CLIENT_ID=...
CTRADER_CLIENT_SECRET=...
CTRADER_REDIRECT_URI=https://YOUR-FACTOR.onrender.com/auth/ctrader/callback
CTRADER_HOST=demo             # or live
CTRADER_DRY_RUN=true          # until ready
TRADING_ENABLED=true          # global master switch
```

Optional: `MARKET_OPEN_*`, `BREAKEVEN_*` (see `factor/config.py` and docs/DEPLOY.md).

Generate a Fernet key:

```python
from factor.crypto import generate_fernet_key
print(generate_fernet_key())
```

## Table prefix

All tables use `factor_*` so they can live in a shared project with Reaktor.
