-- Multiple cTrader accounts per Factor user.
-- Run in Supabase SQL Editor after 001–004.
--
-- One OAuth login (person) → many factor_ctrader_connections (accounts).
-- Alert prefs, orders, claims, and kill switch are per account.

-- ---------------------------------------------------------------------------
-- Users: which account the dashboard is showing
-- ---------------------------------------------------------------------------
alter table factor_users
  add column if not exists active_account_id bigint;

comment on column factor_users.active_account_id is
  'Dashboard-selected ctidTraderAccountId; execution still fans out to every account with enabled prefs';

-- ---------------------------------------------------------------------------
-- Connections: 1:N accounts (was unique user_id)
-- ---------------------------------------------------------------------------
alter table factor_ctrader_connections
  drop constraint if exists factor_ctrader_connections_user_id_key;

drop index if exists factor_ctrader_connections_user_id_key;

alter table factor_ctrader_connections
  add column if not exists trader_login text;
alter table factor_ctrader_connections
  add column if not exists broker_name text;
alter table factor_ctrader_connections
  add column if not exists is_live boolean not null default false;
alter table factor_ctrader_connections
  add column if not exists kill_switch boolean not null default false;
alter table factor_ctrader_connections
  add column if not exists trading_enabled boolean not null default true;

comment on column factor_ctrader_connections.trader_login is
  'Broker platform login shown in cTrader (e.g. 10093658)';
comment on column factor_ctrader_connections.kill_switch is
  'Per-account kill: block new trades and used by dashboard Kill';

create unique index if not exists factor_ctrader_connections_user_account_uidx
  on factor_ctrader_connections (user_id, account_id);

-- Copy legacy user-level kill onto the (single) connection
update factor_ctrader_connections c
set
  kill_switch = coalesce(u.kill_switch, false),
  trading_enabled = coalesce(u.trading_enabled, true),
  is_live = case
    when c.host ilike 'live%' then true
    else coalesce(c.is_live, false)
  end
from factor_users u
where u.id = c.user_id;

update factor_users u
set active_account_id = c.account_id
from factor_ctrader_connections c
where c.user_id = u.id
  and u.active_account_id is null;

-- ---------------------------------------------------------------------------
-- Prefs: unique (user, account, kind, symbol)
-- ---------------------------------------------------------------------------
alter table factor_user_alert_prefs
  add column if not exists account_id bigint;

update factor_user_alert_prefs p
set account_id = c.account_id
from factor_ctrader_connections c
where c.user_id = p.user_id
  and p.account_id is null;

update factor_user_alert_prefs
set account_id = 0
where account_id is null;

alter table factor_user_alert_prefs
  alter column account_id set not null;

alter table factor_user_alert_prefs
  drop constraint if exists factor_user_alert_prefs_user_id_kind_symbol_key;

drop index if exists factor_user_alert_prefs_user_id_kind_symbol_key;

create unique index if not exists factor_user_alert_prefs_user_account_kind_symbol_uidx
  on factor_user_alert_prefs (user_id, account_id, kind, symbol);

create index if not exists factor_user_alert_prefs_account_enabled_idx
  on factor_user_alert_prefs (account_id, kind, symbol)
  where enabled = true;

-- ---------------------------------------------------------------------------
-- Orders: tag the trading account
-- ---------------------------------------------------------------------------
alter table factor_orders
  add column if not exists account_id bigint;

update factor_orders o
set account_id = c.account_id
from factor_ctrader_connections c
where c.user_id = o.user_id
  and o.account_id is null;

create index if not exists factor_orders_user_account_created_idx
  on factor_orders (user_id, account_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Claims: same alert may trade on more than one of the user's accounts
-- ---------------------------------------------------------------------------
alter table factor_processed_alerts
  add column if not exists account_id bigint;

update factor_processed_alerts p
set account_id = coalesce(
  (
    select c.account_id
    from factor_ctrader_connections c
    where c.user_id = p.user_id
    limit 1
  ),
  0
)
where p.account_id is null;

alter table factor_processed_alerts
  alter column account_id set not null;

alter table factor_processed_alerts
  drop constraint if exists factor_processed_alerts_pkey;

alter table factor_processed_alerts
  add primary key (user_id, account_id, alert_id);
