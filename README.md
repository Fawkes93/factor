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
4. Run SQL: `sql/001_factor_multiuser.sql` (+ `002`–`005` as needed; `005` = multi-account)  
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

## CRT

`kind=crt` is a market order after Reaktor has already seen the limit level. Factor places `metrics.sl_price` and `metrics.tp_price` (the runner). It does not recompute those levels from the fill.

The 15s poll (`BREAKEVEN_POLL_SEC`) then:

- closes `tp1_frac` (default half) once, when the exit-side mark reaches `tp1_price` (bid for a buy, ask for a sell), rounded down to the volume step
- leaves the stop at the original SL (`be_after_tp1` is false; generic breakeven is off; no trail)
- closes whatever is left at `max_hold_until`, or at open time + `max_hold_hours` when that timestamp is missing (calendar hours, weekends included — 96h XAU, 48h EUR when Reaktor sends those)
- closes open CRT positions on the weekly clock, whichever of the two deadlines comes first

Weekly flatten is Factor config, not the webhook string:

| Variable | Default |
|----------|---------|
| `CRT_WEEKLY_FLATTEN` | `FRI 23:45 Europe/Chisinau` |
| `CRT_WEEKLY_FLATTEN_ENABLED` | `true` |
| `CRT_MAX_ENTRY_SLIP_PTS` | off (`0`) |

The schedule is copied onto the order at entry. The enable switch is read on every poll. With a 15s poll the Friday close goes out between 23:45:00 and 23:45:15 local. A position opened at or after that clock waits until the next Friday. A later poll still closes a position whose Friday already passed.

Before the order is sent, Factor skips `crt_stale_fill` when the live mark is already through `sl_price` or `tp1_price`. Dry-run has no quote, so that check is skipped and the order is only logged. When the live fill returns a position id, Factor amends SL and TP back onto the absolute webhook prices (cTrader's market order uses a relative distance from the fill).

Half a position below the broker minimum, or not a whole step, is stored as `partial_skipped:min_volume` and the partial is not retried. A 0.01 lot on gold is one minimum step, so the partial needs at least 0.02 lot. If the rounded partial would leave a stub under the minimum, the whole position is closed.

CRT uses the same 5s alert-age window as candle (`MAX_ALERT_AGE_SEC`). The stale-fill check is the price gate. No SQL change: `kind` is plain text and the protect state lives in `detail` JSON.

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
- Market-open blackout (first 15 minutes after ~01:05 local, **Monday only** by default)  

Live money: only after demo is reliable — set `CTRADER_HOST=live` + live OAuth.
