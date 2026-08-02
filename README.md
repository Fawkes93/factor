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

## Other config

| Env | Default | Meaning |
|-----|---------|---------|
| `WEBHOOK_SECRET` | — | Match Reaktor webhook secret |
| `FACTOR_ADMIN_TOKEN` | — | Protects `/api/trades`, `/api/kill` |
| `REAKTOR_BASE_URL` / `REAKTOR_API_KEY` | — | Catch-up pull |
| `POLL_INTERVAL_SEC` | `60` | Backup pull |
| `TRADE_KINDS` | `candle` | Range expansion only (Reaktor live range) |
| `TRADE_SYMBOLS` | `XAU/USD` | |
| `MAX_OPEN_TOTAL` | `1` | Ignore all alerts while any position is open |
| `MAX_OPEN_PER_SYMBOL` | `1` | |
| `MAX_TRADES_PER_DAY` | `10` | |
| `COOLDOWN_SEC` | `0` | |

Default XAU range order: **0.01 lot**, **SL 1000 pts**, **TP 1000 pts** (MT points, 1 pt = $0.01). Edit `factor/config.py` → `PRESETS` or `PRESETS_JSON`.

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
