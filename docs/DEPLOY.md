# Deploy Factor on Render.com

Factor and Reaktor are **two separate web services**. Reaktor detects signals; Factor places paper (later live) orders.

```
[Reaktor on Render]  --signed webhook-->  [Factor on Render]
        ^                                        |
        |--------- pull /api/v1/alerts ----------|
                   (boot + every 60s backup)
```

---

## 0. One-time prep

### A. GitHub repo for Factor

Factor must be its **own** GitHub repository (not inside your home folder git).

```bash
cd ~/factor
git status   # should show this folder only
# Create empty repo on GitHub (e.g. Fawkes93/factor), then:
git remote add origin https://github.com/YOU/factor.git
git push -u origin master
```

### B. Secrets to invent (save in a password manager)

| Secret | Example shape | Used by |
|--------|---------------|---------|
| Webhook shared secret | `whsec_…` long random | Reaktor + Factor |
| Reaktor API key for Factor | `rk_…` long random | Reaktor `REAKTOR_API_KEYS` + Factor `REAKTOR_API_KEY` |
| Factor admin token | long random | Factor only (`FACTOR_ADMIN_TOKEN`) |

Generate random strings:

```bash
openssl rand -hex 24
```

---

## 1. Deploy Factor on Render

1. [Render Dashboard](https://dashboard.render.com) → **New → Blueprint** (this repo’s `render.yaml`)  
   **or** **New → Web Service** → connect the Factor GitHub repo.
2. Settings if manual:
   - **Runtime:** Python  
   - **Build:** `pip install -r requirements.txt`  
   - **Start:** `python -m factor`  
   - **Health check:** `/api/health`  
   - **Plan:** **Starter (paid)** — free tier sleeps and misses webhooks.
3. Environment:

| Key | Value |
|-----|--------|
| `BROKER` | `ctrader` |
| `CTRADER_HOST` | `demo` |
| `CTRADER_DRY_RUN` | `true` until credentials verified |
| `CTRADER_CLIENT_ID` / `_SECRET` / `_ACCESS_TOKEN` / `_ACCOUNT_ID` | from openapi.ctrader.com |
| `TRADING_ENABLED` | `false` at first |
| `WEBHOOK_SECRET` | your `whsec_…` |
| `REAKTOR_BASE_URL` | `https://YOUR-REAKTOR.onrender.com` (no trailing slash) |
| `REAKTOR_API_KEY` | your `rk_…` |
| `FACTOR_ADMIN_TOKEN` | admin secret |
| `TRADE_KINDS` | `candle` |
| `TRADE_SYMBOLS` | `XAU/USD` |
| `POLL_INTERVAL_SEC` | `60` |

4. Deploy → open `https://factor-XXXX.onrender.com/api/health`  
   Expect `"ok": true`, `"webhook_configured": true`, `"reaktor_pull_configured": true`.

---

## 2. Point Reaktor at Factor

In the **Reaktor** Render service → Environment, add/update:

```bash
# Allow Factor to pull alerts
REAKTOR_API_KEYS=factor:rk_SAME_AS_FACTOR_REAKTOR_API_KEY

# Push alerts to Factor (use your real Factor URL)
REAKTOR_WEBHOOKS=[{"id":"factor","url":"https://factor-XXXX.onrender.com/hooks/reaktor","secret":"whsec_SAME_AS_FACTOR","kinds":["candle"],"symbols":["XAU/USD"],"enabled":true}]
```

Notes:

- JSON must be **one line** in Render’s env UI (or valid JSON if multi-line is allowed).
- `secret` must **exactly** match Factor `WEBHOOK_SECRET`.
- Redeploy / restart **Reaktor** after changing env so webhooks load.

---

## 3. Smoke test

### Health

```bash
curl -sS https://factor-XXXX.onrender.com/api/health | python3 -m json.tool
```

### Signed test webhook

With `TRADING_ENABLED=true`. Prefer `CTRADER_DRY_RUN=true` first (no broker order),
then `false` when gold market is open and demo credentials are set:

```bash
# set these first
export FACTOR_URL=https://factor-XXXX.onrender.com
export WEBHOOK_SECRET=whsec_…
```

```bash
python3 - <<'PY'
import json, hashlib, hmac, os, urllib.request
secret = os.environ["WEBHOOK_SECRET"]
base = os.environ["FACTOR_URL"].rstrip("/")
body = {
  "specversion": "1.0",
  "id": "reaktor-alert-900001",
  "source": "reaktor",
  "type": "com.reaktor.alert.v1",
  "data": {
    "alert_id": 900001,
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
    f"{base}/hooks/reaktor",
    data=raw,
    headers={
        "Content-Type": "application/json",
        "X-Reaktor-Signature": sig,
        "X-Reaktor-Event-Id": "900001",
    },
    method="POST",
)
print(urllib.request.urlopen(req).read().decode())
PY
```

List trades (needs admin token):

```bash
curl -sS -H "Authorization: Bearer $FACTOR_ADMIN_TOKEN" \
  "$FACTOR_URL/api/trades" | python3 -m json.tool
```

### Live signal path

Wait for a real Reaktor candle alert on XAU, then check Factor logs for `PAPER BUY/SELL` or `skip …`.

---

## 4. Enable demo trading

When health + dry-run webhook look good:

1. Set cTrader secrets (see **docs/CTRADER.md**)  
2. `CTRADER_HOST=demo`, `CTRADER_DRY_RUN=false`  
3. `TRADING_ENABLED=true`  
4. Test while market is open; confirm position in cTrader demo  
5. Only later: `CTRADER_HOST=live` with live account

Emergency stop:

```bash
curl -X POST -H "Authorization: Bearer $FACTOR_ADMIN_TOKEN" \
  "$FACTOR_URL/api/kill"
```

---

## 5. Ops notes

| Topic | Detail |
|-------|--------|
| **Always-on** | Use paid plan; free tier sleeps → missed webhooks |
| **One instance** | Don’t scale Factor to multiple instances (duplicate orders risk) |
| **Disk** | SQLite under `data/` is **ephemeral** on Render; redeploy wipes local de-dupe. Boot catch-up + 60s poll rebuild from Reaktor history. |
| **Dashboard** | No full UI yet — use `/api/health` and `/api/trades` |
| **Auth** | Webhook = HMAC. Admin routes = `FACTOR_ADMIN_TOKEN`. Health is public (ok). |
| **Region** | Same as Reaktor (`oregon` in blueprints) for lower latency |

---

## Checklist

- [ ] Factor repo on GitHub (only `~/factor` contents)
- [ ] Factor web service live, `/api/health` ok
- [ ] Secrets set on Factor
- [ ] Reaktor `REAKTOR_API_KEYS` + `REAKTOR_WEBHOOKS` updated
- [ ] Reaktor restarted
- [ ] Fake signed webhook returns `traded` or expected `skip`
- [ ] Real alert appears in Factor logs (paper)
- [ ] `TRADING_ENABLED=true` only after that
