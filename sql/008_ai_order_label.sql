-- cTrader label for orders placed by the AI execution API.
-- Reaktor orders leave this null. Their cTrader label stays "factor".
-- Run in the Supabase SQL editor after 001–007.
-- Safe to run before or after the app deploy. Until it runs, Factor stores
-- the label inside factor_orders.detail as well.

alter table factor_orders
  add column if not exists label text;

comment on column factor_orders.label is
  'cTrader order label. AI orders use AI-GOLD-v1. Reaktor orders leave this null.';

create index if not exists factor_orders_label_idx
  on factor_orders (label)
  where label is not null;
