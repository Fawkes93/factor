-- Optional SL/TP override for Asia breakout.
-- Run in the Supabase SQL editor after 001–005.
-- When false (default), Factor uses the Reaktor Asia-box SL/TP.
-- When true, Factor uses this pref's sl_points / tp_points.
-- Trail + flatten still follow the alert. Safe to run before or after the app deploy.

alter table factor_user_alert_prefs
  add column if not exists sl_tp_override boolean not null default false;

comment on column factor_user_alert_prefs.sl_tp_override is
  'Asia breakout: when true, use sl_points/tp_points instead of Reaktor SL/TP prices';
