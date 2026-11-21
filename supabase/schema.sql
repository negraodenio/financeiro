-- Escudo Real • QuantGuard — Supabase Postgres Schema (Multi-Tenant + RLS + Atomic Quota)
-- Execute este script no SQL Editor do seu projeto Supabase:
-- https://supabase.com/dashboard/project/poeopgcjnfxeazfxudgl/sql/new

-- 1. Perfis e Cotas por Usuário (vinculado a auth.users)
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  full_name text not null default '',
  plan_tier text not null default 'TRIAL' check (plan_tier in ('TRIAL', 'ESCUDO', 'PRO', 'VIP')),
  trial_uses_remaining integer not null default 3,
  monthly_contribution_brl numeric(12,2) not null default 500.00,
  monthly_expenses_brl numeric(12,2) not null default 3000.00,
  has_emergency_fund boolean not null default false,
  telegram_chat_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists profiles_telegram_chat_id_idx
  on public.profiles (telegram_chat_id)
  where telegram_chat_id is not null;

-- 2. Histórico de Análises (Comitê + Escudo Familiar + Backtest A/B)
create table if not exists public.analysis_history (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  symbol text not null,
  timeframe text not null,
  exchange text not null,
  verdict text not null,
  regime text not null,
  last_price numeric(18,6) not null,
  drawdown_saved_pct numeric(8,2) not null default 0,
  window_status text not null default '',
  created_at timestamptz not null default now()
);

create index if not exists analysis_history_user_created_idx
  on public.analysis_history (user_id, created_at desc);

-- 3. Auditorias do Detector de Ciladas
create table if not exists public.trap_checks (
  id bigint generated always as identity primary key,
  user_id uuid references auth.users(id) on delete set null,
  symbol text not null,
  claimed_side text not null,
  verdict_seal text not null,
  current_price numeric(18,6) not null,
  created_at timestamptz not null default now()
);

create index if not exists trap_checks_user_created_idx
  on public.trap_checks (user_id, created_at desc)
  where user_id is not null;

-- 4. Alertas Proativos de Câmbio (Produto Principal Pago: EUR/BRL e USDT/BRL)
create table if not exists public.price_alerts (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  symbol text not null check (symbol in ('EUR/BRL', 'USDT/BRL')),
  direction text not null check (direction in ('ABOVE', 'BELOW')),
  target_rate numeric(12,4) not null check (target_rate > 0),
  channel text not null default 'telegram' check (channel in ('telegram', 'email')),
  active boolean not null default true,
  last_triggered_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists price_alerts_user_created_idx
  on public.price_alerts (user_id, created_at desc);

create index if not exists price_alerts_active_symbol_idx
  on public.price_alerts (symbol, active)
  where active = true;

-- 5. Registro Público Auditável de Cotações e Leituras (Transparência Real)
create table if not exists public.alert_audit_log (
  id bigint generated always as identity primary key,
  symbol text not null,
  rate numeric(14,4) not null,
  rsi_14 numeric(6,2) not null,
  signal_label text not null,
  source text not null,
  is_live boolean not null default true,
  created_at timestamptz not null default now()
);

create index if not exists alert_audit_log_created_idx
  on public.alert_audit_log (created_at desc);

-- 6. Função RPC Atômica para Consumo de Cota Trial (evita condição de corrida)
create or replace function public.consume_trial_use(p_user_id uuid)
returns integer
language sql
security definer
set search_path = public
as $$
  update public.profiles
     set trial_uses_remaining = trial_uses_remaining - 1,
         updated_at = now()
   where id = p_user_id
     and plan_tier = 'TRIAL'
     and trial_uses_remaining > 0
  returning trial_uses_remaining;
$$;

revoke all on function public.consume_trial_use(uuid) from public, anon, authenticated;
grant execute on function public.consume_trial_use(uuid) to service_role;

-- 7. Habilitar Row Level Security (RLS) em todas as tabelas expostas
alter table public.profiles enable row level security;
alter table public.analysis_history enable row level security;
alter table public.trap_checks enable row level security;
alter table public.price_alerts enable row level security;
alter table public.alert_audit_log enable row level security;

-- 8. Permissões explícitas (sem permitir auto-upgrade de plan_tier pelo cliente)
grant usage on schema public to anon, authenticated;
grant select on public.profiles to authenticated;
grant update (full_name, monthly_contribution_brl, monthly_expenses_brl, has_emergency_fund, telegram_chat_id)
  on public.profiles to authenticated;
grant select, insert on public.analysis_history to authenticated;
grant select, insert on public.trap_checks to authenticated;
grant select, insert, update, delete on public.price_alerts to authenticated;
grant select on public.alert_audit_log to anon, authenticated;

-- 9. Políticas RLS performáticas usando (select auth.uid()) e cláusula TO authenticated
drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own on public.profiles
  for select to authenticated
  using ((select auth.uid()) = id);

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles
  for update to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);

drop policy if exists analysis_history_select_own on public.analysis_history;
create policy analysis_history_select_own on public.analysis_history
  for select to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists analysis_history_insert_own on public.analysis_history;
create policy analysis_history_insert_own on public.analysis_history
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists trap_checks_select_own on public.trap_checks;
create policy trap_checks_select_own on public.trap_checks
  for select to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists trap_checks_insert_own on public.trap_checks;
create policy trap_checks_insert_own on public.trap_checks
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists price_alerts_select_own on public.price_alerts;
create policy price_alerts_select_own on public.price_alerts
  for select to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists price_alerts_insert_own on public.price_alerts;
create policy price_alerts_insert_own on public.price_alerts
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists price_alerts_update_own on public.price_alerts;
create policy price_alerts_update_own on public.price_alerts
  for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

drop policy if exists price_alerts_delete_own on public.price_alerts;
create policy price_alerts_delete_own on public.price_alerts
  for delete to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists alert_audit_log_select_public on public.alert_audit_log;
create policy alert_audit_log_select_public on public.alert_audit_log
  for select to anon, authenticated
  using (true);
