-- Limits for the AI trader alert, and the Telegram chat linked to a Factor user.
-- Existing Reaktor alerts leave the new pref columns null.
-- Run in the Supabase SQL editor. Safe to re-run.

alter table factor_user_alert_prefs
  add column if not exists min_volume_lots numeric,
  add column if not exists max_volume_lots numeric,
  add column if not exists min_rr numeric,
  add column if not exists max_rr numeric,
  add column if not exists daily_loss_cap_usd numeric;

comment on column factor_user_alert_prefs.min_volume_lots is
  'AI trader only. Smallest lot the orchestrator may send.';
comment on column factor_user_alert_prefs.max_volume_lots is
  'AI trader only. Largest lot the orchestrator may send.';
comment on column factor_user_alert_prefs.min_rr is
  'AI trader only. Minimum reward:risk.';
comment on column factor_user_alert_prefs.max_rr is
  'AI trader only. Maximum reward:risk. Null means no ceiling.';
comment on column factor_user_alert_prefs.daily_loss_cap_usd is
  'AI trader only. Stop new entries after this much closed loss plus open risk.';

alter table factor_users
  add column if not exists telegram_chat_id bigint,
  add column if not exists telegram_link_code text;

create unique index if not exists factor_users_telegram_chat_uidx
  on factor_users (telegram_chat_id)
  where telegram_chat_id is not null;

create unique index if not exists factor_users_telegram_link_uidx
  on factor_users (telegram_link_code)
  where telegram_link_code is not null;
