-- Breakeven-at-half-TP preference (run in Supabase SQL editor after 001).
-- Optional: Factor falls back if column is missing (BE still follows server default).

alter table factor_user_alert_prefs
  add column if not exists breakeven_half_tp boolean not null default true;

comment on column factor_user_alert_prefs.breakeven_half_tp is
  'When true, Factor moves SL to breakeven after price reaches half of TP distance';
