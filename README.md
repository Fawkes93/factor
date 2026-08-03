# Factor

**Multi-user execution** service for [Reaktor](https://github.com/Fawkes93/reaktor) alerts.

Reaktor detects signals. Factor receives them (HMAC webhook), fans out to logged-in users with **enabled alert prefs**, and places **market orders with SL/TP** on each user’s **cTrader** account (any supported broker). Start on **demo**.

---

## Deploy on Render

Full guides:

- **[docs/MULTIUSER.md](docs/MULTIUSER.md)** — architecture, OAuth, Supabase  
- **[docs/DEPLOY.md](docs/DEPLOY.md)** — Render + Reaktor wiring  
- **[docs/CTRADER.md](docs/CTRADER.md)** — cTrader Open API app  

Short version:

1. Repo: https://github.com/Fawkes93/factor  
2. Render Web Service · paid always-on · health `/api/health`  
3. Secrets: webhook, Supabase, OAuth app, token/session secrets, redirect URI  
4. Run SQL: `sql/001_factor_multiuser.sql` (+ optional `002_breakeven.sql`)  
5. Start with `CTRADER_HOST=demo`, `CTRADER_DRY_RUN=true`, `TRADING_ENABLED=false`  
6. Point Reaktor `REAKTOR_WEBHOOKS` at Factor  
7. Open `/` → Connect with cTrader → enable alert prefs  

```
Reaktor ──webhook──► Factor ──fan-out──► each user’s cTrader account
```

---

## How it fits

```
Reaktor alert → HMAC verify → candidates (pref + trading + not killed)
             → claim (user, alert_id) → freshness / market-open blackout
             → cTrader MARKET + SL/TP → factor_orders
             → optional BE poll (SL → entry at ½ TP)
```

There is **no** single-tenant SQLite trading path and **no** Reaktor catch-up trader — webhooks only.

---

## Required env

| Variable | Notes |
|----------|--------|
| `WEBHOOK_SECRET` | Same secret as Reaktor webhook |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Multi-user DB |
| `FACTOR_TOKEN_ENC_KEY` | Encrypt cTrader tokens at rest |
| `FACTOR_SESSION_SECRET` | Dashboard session cookies |
| `CTRADER_CLIENT_ID` / `CTRADER_CLIENT_SECRET` | Open API app |
| `CTRADER_REDIRECT_URI` | `https://…/auth/ctrader/callback` |
| `CTRADER_HOST` | `demo` or `live` |
| `CTRADER_DRY_RUN` | `true` = log only |
| `TRADING_ENABLED` | Global master switch |

Per-user access tokens come from **OAuth login**, not env.

---

## Local run

Python **3.12+**:

```bash
cd factor
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# fill .env with Supabase + OAuth + webhook
python -m factor
```

```bash
pytest -q
```

---

## Safety

- `TRADING_ENABLED=false` by default  
- Demo host by default  
- `CTRADER_DRY_RUN` for wiring without orders  
- Per-user kill switch on the dashboard  
- Claim de-dupe on `(user_id, alert_id)`, skip `NEUTRAL`, caps from prefs  
- Market-open blackout (first 15 minutes after ~01:05 local)  

Live money: only after demo is reliable — set `CTRADER_HOST=live` + live OAuth.
