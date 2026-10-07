# Factor multi-user

Factor is **multi-user only**: one Reaktor webhook fans out to logged-in users with enabled alert prefs and linked cTrader accounts.

## Dashboard

Open `https://your-factor.onrender.com/` after deploy.

| UI | API |
|----|-----|
| Connect cTrader | `/auth/ctrader/login` |
| Home / status | `GET /api/me` |
| Switch account | `POST /api/me/account` `{ "account_id": … }` |
| Alert settings | `PUT /api/me/prefs` (selected account) |
| Orders | `GET /api/me/orders` (selected account) |
| Kill / Resume | `POST /api/me/kill`, `/api/me/resume` (selected account) |
| Log out | `POST /auth/logout` |

Static assets: `/static/css/app.css`, `/static/js/app.js`.

## Fan-out

1. Reaktor webhook verified (HMAC)  
2. Load candidates: enabled pref for `(kind, symbol)` **on that account**, trading on, not killed, has connection  
3. Per account: **claim** `(user_id, account_id, alert_id)` → freshness (5s, session alerts 20s) → market-open blackout → daily cap (`max_per_day` per kind on that account) → Factor open orders for that pref (`max_concurrent` per kind+symbol) → place → `factor_orders` row  
4. Background poll reconciles open `factor_orders` vs cTrader per account (marks closed when the position is gone) and may move SL to **breakeven** at half TP  

One cTrader login can grant several trading accounts. Each account has its own alert prefs, orders, and kill switch. Dashboard switcher only changes which account you are looking at — fan-out still trades every account that has the pref enabled.  


No single-tenant SQLite engine and no Reaktor catch-up trading — **webhooks only**.

## Alert freshness

Factor skips an alert when `triggered_at` is older than that kind's window (`alert_too_old`). The tight window is for fast signals. Session entries are slower box or bar-close trades, so they get longer.

| Kind | Default | Override |
|------|---------|----------|
| `asia_breakout`, `asia_breakout_h`, `dawn_cross` | 20s | `MAX_ALERT_AGE_ASIA_SEC` |
| `spike`, `spike_neo`, `candle`, `impulse_retrace` | 5s | `MAX_ALERT_AGE_SEC` |

`asia_breakout_h` is Asia Overnight H: the same engine and webhook shape as Asia breakout, with its own preset. `dawn_cross` is the Chișinău 02:00–06:00 5m cross. Both send `metrics.sl_price`, `metrics.tp_price`, and `metrics.flatten_hour`. Asia box alerts can still override SL/TP with the pref's points. Breakeven stays off. Factor flattens at that hour in `metrics.flatten_tz` on the entry's local date, including when the check runs after midnight. `flatten_hour` 0 means no clock flatten. Asia trails when the alert sends trail distances. Dawn Cross does not.

`/api/health` reports both as `max_alert_age_sec` and `max_alert_age_asia_sec`. Missing or unparseable `triggered_at` still skips (`alert_age_unknown`).

## Kill switch

| Path | Effect |
|------|--------|
| `POST /api/me/kill` | Selected account: close open cTrader positions, block new trades |
| `POST /api/me/resume` | Clear kill on the selected account (does not re-open positions) |
| `POST /api/kill` | Global process: `TRADING_ENABLED=false` until restart (admin) |

## OAuth routes

| Path | Purpose |
|------|---------|
| `GET /auth/ctrader/login` | Start OAuth |
| `GET /auth/ctrader/callback` | Exchange code, upsert **all** granted accounts, set session |
| `POST /auth/logout` | Clear session |
| `GET /api/me` | Current user + accounts list + selected connection (no secrets) + prefs |
| `POST /api/me/account` | Switch dashboard `active_account_id` |

Redirect URI must match Open API app settings exactly (`CTRADER_REDIRECT_URI`).

## Supabase setup

1. SQL Editor → run `sql/001_factor_multiuser.sql`  
2. Then `sql/002_breakeven.sql` (optional BE on/off pref column)  
3. Then `sql/004_breakeven_fraction.sql` (per-pref BE % of TP)  
4. Then `sql/005_accounts.sql` (multiple cTrader accounts per user)  
5. Project Settings → API → **service_role** key (server only)

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
