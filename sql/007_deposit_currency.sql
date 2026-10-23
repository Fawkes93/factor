-- Account deposit currency (ISO code, e.g. USD).
-- Run in the Supabase SQL editor after 001–006.
-- Factor fills this from ProtoOATrader.depositAssetId.
-- Safe to run before or after the app deploy.

alter table factor_ctrader_connections
  add column if not exists deposit_currency text;

comment on column factor_ctrader_connections.deposit_currency is
  'Deposit currency code for this cTrader account (e.g. USD). P/L is in this currency.';
