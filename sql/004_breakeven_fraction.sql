-- Per-pref breakeven trigger as fraction of TP distance (not fixed ½).
-- Run after sql/002_breakeven.sql. 0.5 = half TP (legacy default).

alter table factor_user_alert_prefs
  add column if not exists breakeven_fraction numeric not null default 0.5
  check (breakeven_fraction > 0 and breakeven_fraction <= 1);

comment on column factor_user_alert_prefs.breakeven_fraction is
  'Move SL to entry when favorable move reaches this fraction of TP distance (e.g. 0.5 = half TP)';
