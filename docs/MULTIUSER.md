# Factor multi-user (in progress)

Branch: `multiuser`

## Status

- [x] SQL schema (`sql/001_factor_multiuser.sql`)
- [x] Token encryption helpers
- [x] Supabase repos (users, connections, prefs, orders, claim de-dupe)
- [x] cTrader OAuth + sessions (`/auth/ctrader/login`, callback, `/api/me`)
- [x] Fan-out engine (`process_alert_multi`, claim-before-place, caps)
- [ ] Kill switch close-all
- [ ] Dashboard UI
- [ ] Deploy cutover

## Fan-out (PR3)

When `MULTI_USER=true`:

1. Reaktor webhook verified as before  
2. Load candidates: enabled pref for `(kind, symbol)`, trading on, not killed, has connection  
3. Per user: **claim** `(user_id, alert_id)` → freshness → daily cap → cTrader open count vs `max_concurrent` → place → `factor_orders` row  

Boot catch-up **does not trade** in multi-user mode (webhooks only).

Keep `MULTI_USER=false` on Render until you OAuth a user and enable a pref.

## OAuth routes

| Path | Purpose |
|------|---------|
| `GET /auth/ctrader/login?env=demo` | Start OAuth (`env=live` for live host) |
| `GET /auth/ctrader/callback` | Exchange code, store encrypted tokens, set session |
| `POST /auth/logout` | Clear session |
| `GET /api/me` | Current user + connection (no secrets) + prefs |

Redirect URI must match Open API app settings exactly (`CTRADER_REDIRECT_URI`).

## Supabase setup

1. Open project `https://ewaloiemhjuuwlshzsmy.supabase.co`
2. SQL Editor → paste and run `sql/001_factor_multiuser.sql`
3. Project Settings → API → copy **service_role** key (server only)

## Local env

```bash
SUPABASE_URL=https://ewaloiemhjuuwlshzsmy.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
FACTOR_TOKEN_ENC_KEY=...   # any long secret or Fernet key
FACTOR_SESSION_SECRET=...  # random string for cookies
MULTI_USER=false           # flip true when engine is ready
```

Generate a Fernet key:

```python
from factor.crypto import generate_fernet_key
print(generate_fernet_key())
```

## Table prefix

All tables use `factor_*` so they can live in a shared project with Reaktor.
