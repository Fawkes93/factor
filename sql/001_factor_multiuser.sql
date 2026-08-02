-- Factor multi-user schema (prefix factor_ for shared Supabase projects)
-- Run in Supabase SQL Editor.

-- Users (cTrader OAuth identity)
create table if not exists factor_users (
  id uuid primary key default gen_random_uuid(),
  ctrader_ctid text unique,
  display_name text,
  kill_switch boolean not null default false,
  trading_enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists factor_users_kill_idx
  on factor_users (kill_switch) where kill_switch = false;

-- Per-user cTrader connection (tokens encrypted by Factor app)
create table if not exists factor_ctrader_connections (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references factor_users(id) on delete cascade,
  account_id bigint not null,
  host text not null default 'demo.ctraderapi.com',
  access_token_enc text not null,
  refresh_token_enc text not null,
  access_expires_at timestamptz,
  scopes text,
  raw_meta jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create index if not exists factor_ctrader_connections_account_idx
  on factor_ctrader_connections (account_id);

-- Alert preferences (kind × symbol)
create table if not exists factor_user_alert_prefs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references factor_users(id) on delete cascade,
  kind text not null,
  symbol text not null,
  enabled boolean not null default false,
  max_concurrent int not null default 1 check (max_concurrent >= 0),
  max_per_day int not null default 5 check (max_per_day >= 0),
  volume_lots numeric not null default 0.01 check (volume_lots > 0),
  sl_points int not null default 1000 check (sl_points > 0),
  tp_points int not null default 1000 check (tp_points > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, kind, symbol)
);

create index if not exists factor_user_alert_prefs_enabled_idx
  on factor_user_alert_prefs (kind, symbol)
  where enabled = true;

-- Orders (dashboard + audit)
create table if not exists factor_orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references factor_users(id) on delete cascade,
  reaktor_alert_id bigint not null,
  kind text not null,
  symbol text not null,
  side text not null,
  status text not null,
  skip_reason text,
  entry_price numeric,
  sl_price numeric,
  tp_price numeric,
  volume_lots numeric,
  broker_order_id text,
  broker_position_id text,
  detail jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  closed_at timestamptz
);

create index if not exists factor_orders_user_created_idx
  on factor_orders (user_id, created_at desc);

create index if not exists factor_orders_alert_idx
  on factor_orders (reaktor_alert_id);

create index if not exists factor_orders_user_status_idx
  on factor_orders (user_id, status);

create index if not exists factor_orders_user_day_idx
  on factor_orders (user_id, kind, created_at desc);

-- Per-user alert de-dupe (claim-before-place)
create table if not exists factor_processed_alerts (
  user_id uuid not null references factor_users(id) on delete cascade,
  alert_id bigint not null,
  reason text,
  created_at timestamptz not null default now(),
  primary key (user_id, alert_id)
);

-- Optional system-level inbound log
create table if not exists factor_inbound_events (
  alert_id bigint primary key,
  received_at timestamptz not null default now(),
  payload_hash text
);

-- updated_at helper
create or replace function factor_set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists factor_users_updated_at on factor_users;
create trigger factor_users_updated_at
  before update on factor_users
  for each row execute function factor_set_updated_at();

drop trigger if exists factor_ctrader_connections_updated_at on factor_ctrader_connections;
create trigger factor_ctrader_connections_updated_at
  before update on factor_ctrader_connections
  for each row execute function factor_set_updated_at();

drop trigger if exists factor_user_alert_prefs_updated_at on factor_user_alert_prefs;
create trigger factor_user_alert_prefs_updated_at
  before update on factor_user_alert_prefs
  for each row execute function factor_set_updated_at();
