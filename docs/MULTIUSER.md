# Factor multi-user (in progress)

Branch: `multiuser`

## Status

- [x] SQL schema (`sql/001_factor_multiuser.sql`)
- [x] Token encryption helpers
- [x] Supabase repos (users, connections, prefs, orders, claim de-dupe)
- [ ] cTrader OAuth + sessions
- [ ] Fan-out engine
- [ ] Kill switch close-all
- [ ] Dashboard UI
- [ ] Deploy cutover

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
