# Factor

Personal **execution** service for [Reaktor](https://github.com/Fawkes93/reaktor) alerts.

Reaktor detects signals. Factor receives them (webhook + pull backup), applies risk rules, and places **market orders with stop loss / take profit** on your **IC Markets cTrader** account (start on **demo**).

---

## Deploy on Render

Full guides:

- **[docs/DEPLOY.md](docs/DEPLOY.md)** — Render + Reaktor wiring  
- **[docs/CTRADER.md](docs/CTRADER.md)** — IC Markets demo Open API credentials  

Short version:

1. Repo: https://github.com/Fawkes93/factor  
2. Render Web Service · paid always-on · health `/api/health`  
3. Secrets: webhook + Reaktor pull + **cTrader** credentials  
4. Start with `CTRADER_HOST=demo`, `CTRADER_DRY_RUN=true`, `TRADING_ENABLED=false`  
5. Point Reaktor `REAKTOR_WEBHOOKS` at Factor  

```
Reaktor ──webhook──► Factor ──Open API──► cTrader demo (IC Markets)
```

---

## How it fits

```
Reaktor alert → HMAC verify → de-dupe → filters → SL/TP presets
             → cTrader MARKET order + relative SL/TP
```

---

## cTrader env (required for orders)

| Variable | Notes |
|----------|--------|
| `CTRADER_CLIENT_ID` | From [openapi.ctrader.com](https://openapi.ctrader.com/) |
| `CTRADER_CLIENT_SECRET` | App secret |
| `CTRADER_ACCESS_TOKEN` | OAuth token with **trade** scope |
| `CTRADER_ACCOUNT_ID` | Demo (or later live) account id |
| `CTRADER_HOST` | `demo` (default) or `live` |
| `CTRADER_DRY_RUN` | `true` = log only, no order |
| `TRADING_ENABLED` | Master switch |

---

## Config: what goes where

| Place | What belongs there |
|-------|--------------------|
| **Env / `.env` / Render secrets** | Secrets + safety: webhook, admin, Reaktor URL/key, cTrader creds, `TRADING_ENABLED`, `CTRADER_DRY_RUN`, `CTRADER_HOST` |
| **`factor/config.py`** | Product defaults: `TRADE_KINDS` (`candle` = range), symbols, open limits, cooldown, poll/catch-up, XAU **PRESETS** (lots / SL / TP) |

Optional env overrides still work (`TRADE_KINDS`, `MAX_OPEN_TOTAL`, `PRESETS_JSON`, …) if you need a one-off without a code change — you do **not** need them in `.env` for normal runs.

Default XAU range order (in code): **0.01 lot**, **SL 1000 pts**, **TP 1000 pts** (MT points, 1 pt = $0.01).

**Freshness:** only alerts with `triggered_at` younger than **`MAX_ALERT_AGE_SEC`** (default **5s**) can open an order. Catch-up and old history are logged as `alert_too_old` and skipped. Live path is the Reaktor **webhook**; the 60s poll only advances state for stale ids.

**Memory (Render 512MB):** Factor is far lighter than Reaktor (no live price WS / chart buffers). Still: single uvicorn worker, cTrader symbol map cached after first load, catch-up page size capped, trade `detail_json` truncated, RSS in `/api/health` (`memory.rss_mb`) and logs every ~15m (warn + `gc.collect` above ~380MB).

---

## Local run

Python **3.12+**:

```bash
cd ~/factor
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill cTrader + webhook secrets
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
- De-dupe on `alert_id`, skip `NEUTRAL`, daily/symbol limits  

Live money: only after demo is reliable — set `CTRADER_HOST=live` + live account id.

---

## Project layout

```text
factor/
├── factor/
│   ├── broker/ctrader*.py   # Open API client + orders
│   ├── inbound/             # HMAC + CloudEvent
│   ├── rules/               # filters + SL/TP
│   └── api/                 # FastAPI
├── docs/DEPLOY.md
├── docs/CTRADER.md
└── render.yaml
```
