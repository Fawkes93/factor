# Factor

Personal **execution** service for [Reaktor](https://github.com/Fawkes93/reaktor) alerts.

Reaktor detects signals. Factor receives them (webhook + pull backup), applies risk rules, and places orders with **preset stop loss / take profit**.

**Now:** paper trading only (`BROKER=paper`).  
**Later:** IC Markets via **cTrader Open API**.

---

## Deploy on Render (recommended)

Full step-by-step: **[docs/DEPLOY.md](docs/DEPLOY.md)**

Short version:

1. Push this repo to GitHub (Factor alone — not your home directory).
2. Render → New Web Service / Blueprint (`render.yaml`).
3. **Paid always-on** plan (free tier sleeps → missed webhooks).
4. Set secrets: `WEBHOOK_SECRET`, `REAKTOR_BASE_URL`, `REAKTOR_API_KEY`, `FACTOR_ADMIN_TOKEN`.
5. Keep `TRADING_ENABLED=false` until a test webhook works.
6. On **Reaktor**, set `REAKTOR_WEBHOOKS` → `https://factor-….onrender.com/hooks/reaktor` and `REAKTOR_API_KEYS`.

```
Reaktor (Render) ──webhook──► Factor (Render) /hooks/reaktor
       ^                              │
       └──── pull every 60s ──────────┘  (backup if a webhook is missed)
```

Health: `https://<factor>.onrender.com/api/health`

---

## How it fits

```
Reaktor ──signed webhook──► Factor POST /hooks/reaktor
                              │ verify HMAC
                              │ de-dupe alert_id
                              │ filter kind / symbol / safety
                              │ SL/TP from fixed points
                              └─ paper order (or cTrader later)
```

Do **not** scrape Telegram. Use Reaktor’s machine API  
(`docs/outbound-alerts.md` in the Reaktor repo).

---

## Run locally (optional)

Needs **Python 3.12+** (3.13 is fine).

```bash
cd ~/factor
/opt/miniconda3/bin/python3.13 -m venv .venv   # if system python3 is old
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# WEBHOOK_SECRET=…  TRADING_ENABLED=true  BROKER=paper

python -m factor
# → http://127.0.0.1:8090/api/health
```

### Tests

```bash
pytest -q
```

### Smoke-test webhook (local)

```bash
source .venv/bin/activate
python - <<'PY'
import json, hashlib, hmac, urllib.request

secret = "whsec_change_me_long_random"  # match .env WEBHOOK_SECRET
body = {
  "specversion": "1.0",
  "id": "reaktor-alert-999001",
  "source": "reaktor",
  "type": "com.reaktor.alert.v1",
  "data": {
    "alert_id": 999001,
    "kind": "candle",
    "symbol": "XAU/USD",
    "side": "UP",
    "price": 4000.0,
    "timeframe_min": 15,
  },
}
raw = json.dumps(body, separators=(",", ":")).encode()
sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
req = urllib.request.Request(
    "http://127.0.0.1:8090/hooks/reaktor",
    data=raw,
    headers={
        "Content-Type": "application/json",
        "X-Reaktor-Signature": sig,
        "X-Reaktor-Event-Id": "999001",
    },
    method="POST",
)
print(urllib.request.urlopen(req).read().decode())
PY
```

---

## Config

| Env | Default | Meaning |
|-----|---------|---------|
| `TRADING_ENABLED` | `false` | Master switch |
| `BROKER` | `paper` | `paper` now; `ctrader` later |
| `WEBHOOK_SECRET` | — | Must match Reaktor webhook `secret` |
| `FACTOR_ADMIN_TOKEN` | — | Protects `/api/trades`, `/api/kill`, `/api/catchup` |
| `REAKTOR_BASE_URL` | — | e.g. `https://reaktor-….onrender.com` |
| `REAKTOR_API_KEY` | — | Key listed in Reaktor `REAKTOR_API_KEYS` |
| `POLL_INTERVAL_SEC` | `60` | Backup pull; `0` = off |
| `TRADE_KINDS` | `candle` | Comma list |
| `TRADE_SYMBOLS` | `XAU/USD` | Comma list |
| `MAX_OPEN_PER_SYMBOL` | `1` | Safety |
| `MAX_TRADES_PER_DAY` | `10` | Safety |
| `COOLDOWN_SEC` | `300` | Per-symbol pause |

### Stop loss / take profit (points)

Edit `factor/config.py` → `PRESETS` (or `PRESETS_JSON` env):

```python
"XAU/USD": {
    "volume_lots": 0.01,
    "sl_points": 300,   # 300 × 0.01 = $3.00
    "tp_points": 600,
    "point_size": 0.01,
}
```

Per-kind overrides later via `PRESET_OVERRIDES` / `PRESET_OVERRIDES_JSON`.

---

## HTTP API

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/api/health` | public | Alive + config snapshot |
| `POST` | `/hooks/reaktor` | HMAC | Reaktor webhook |
| `GET` | `/api/trades` | admin token* | Recent attempts |
| `POST` | `/api/kill` | admin token* | Disable trading |
| `POST` | `/api/catchup` | admin token* | Force pull from Reaktor |

\*If `FACTOR_ADMIN_TOKEN` is set (recommended on Render).

---

## Safety

- Trading **off** by default  
- Paper broker by default  
- Duplicate `alert_id` never re-opens  
- `NEUTRAL` (news) never opens  
- Daily / per-symbol limits + cooldown  
- Invalid signature → 401  
- Boot catch-up + 60s poll if webhooks are missed  

**Real money is later (cTrader).** Prove paper on Render first.

---

## Project layout

```text
factor/
├── factor/           # Python package
│   ├── api/          # FastAPI routes
│   ├── inbound/      # HMAC + CloudEvent
│   ├── rules/        # filters + SL/TP
│   ├── broker/       # paper (+ ctrader stub)
│   ├── store/        # SQLite
│   └── catchup.py    # pull + poll loop
├── docs/DEPLOY.md    # Render + Reaktor wiring
├── render.yaml
└── tests/
```

---

## Roadmap

1. ✅ Paper path + webhook + filters + presets  
2. ✅ Render-ready (blueprint, poll backup, deploy docs)  
3. cTrader Open API on IC Markets demo  
4. Tiny live size after paper is boringly reliable  

### Broker note (IC Markets)

| API | For Factor? |
|-----|-------------|
| **cTrader Open API** | Yes — works on Render |
| cTrader Algo (cBots) | No |
| FIX | Later / heavier |
