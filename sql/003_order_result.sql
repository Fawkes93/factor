-- Won / lost tag for closed trades (from cTrader close deal PnL).
-- Values: won | lost | breakeven | unknown | null (still open / never traded)

alter table factor_orders
  add column if not exists result text;

comment on column factor_orders.result is
  'Trade outcome after close: won, lost, breakeven, unknown';

create index if not exists factor_orders_user_result_idx
  on factor_orders (user_id, result)
  where result is not null;
