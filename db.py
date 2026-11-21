"""Supabase Auth (with refresh token & email confirmation) & Postgres persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import threading
import time
from typing import Any

import httpx

from agents import _load_dotenv

_load_dotenv()

logger = logging.getLogger(__name__)

TIER_LABELS: dict[str, str] = {
    "TRIAL": "Gratuito (3 Análises)",
    "ESCUDO": "Scudex Pro (€ 14 / R$ 79)",
    "PRO": "Pro Desk Quant (€ 39 / R$ 147)",
    "VIP": "Desk VIP",
}

AUDIT_ENGINE_VERSION = "Scudex Quant v2 (Motor Determinístico)"
AUDIT_METHODOLOGY = "EMA(20/50) + RSI(14) + Bollinger(20,2) sobre velas reais"

_AUDIT_LOCK = threading.Lock()
_LAST_AUDIT_TS: dict[str, float] = {}


class SupabaseAuthError(Exception):
    """Raised when signup, login, refresh, or JWT validation fails."""


class SupabaseQuotaExceededError(Exception):
    """Raised when a Supabase TRIAL user has exhausted their free quota."""


class SupabaseFeatureUnavailableError(Exception):
    """Raised when a new Supabase table/RPC migration has not been applied yet."""


def clear_audit_dedup_cache() -> None:
    """Clear in-memory audit snapshot deduplication timestamps."""
    with _AUDIT_LOCK:
        _LAST_AUDIT_TS.clear()


def _cfg() -> tuple[str, str, str]:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    anon = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
    service = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return url, anon, service


def is_supabase_configured() -> bool:
    url, anon, service = _cfg()
    return bool(url and anon and service)


def _headers(
    use_service_role: bool = False,
    bearer_token: str | None = None,
    prefer: str | None = None,
) -> dict[str, str]:
    _, anon, service = _cfg()
    api_key = service if use_service_role else anon
    hdrs = {
        "apikey": api_key,
        "Authorization": f"Bearer {bearer_token or api_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        hdrs["Prefer"] = prefer
    return hdrs


def check_tables_ready() -> dict[str, Any]:
    """Verify connectivity to Supabase Auth and check if core public schema tables exist."""
    url, anon, service = _cfg()
    if not (url and anon and service):
        return {"configured": False, "auth_connected": False, "tables_ready": False}

    auth_connected = False
    tables_ready = False

    try:
        r_auth = httpx.get(
            f"{url}/auth/v1/health",
            headers={"apikey": anon},
            timeout=5.0,
        )
        auth_connected = r_auth.status_code == 200
    except Exception as exc:
        logger.warning("Supabase auth health check failed: %s", exc)

    try:
        r_tbl = httpx.get(
            f"{url}/rest/v1/profiles?select=id&limit=1",
            headers=_headers(use_service_role=True),
            timeout=5.0,
        )
        tables_ready = r_tbl.status_code == 200
    except Exception as exc:
        logger.warning("Supabase table check failed: %s", exc)

    return {
        "configured": True,
        "auth_connected": auth_connected,
        "tables_ready": tables_ready,
    }


def _default_profile(user_id: str, email: str, full_name: str = "") -> dict[str, Any]:
    return {
        "id": user_id,
        "email": email,
        "full_name": full_name,
        "plan_tier": "TRIAL",
        "trial_uses_remaining": 3,
        "monthly_contribution_brl": 500.0,
        "monthly_expenses_brl": 3000.0,
        "has_emergency_fund": False,
        "telegram_chat_id": None,
    }


def _ensure_profile(user_id: str, email: str, full_name: str = "") -> dict[str, Any]:
    url, _, _ = _cfg()
    fallback = _default_profile(user_id, email, full_name)
    try:
        r_get = httpx.get(
            f"{url}/rest/v1/profiles?id=eq.{user_id}&select=*",
            headers=_headers(use_service_role=True),
            timeout=5.0,
        )
        if r_get.status_code == 200:
            rows = r_get.json()
            if rows:
                return rows[0]

            r_ins = httpx.post(
                f"{url}/rest/v1/profiles",
                headers=_headers(use_service_role=True, prefer="return=representation"),
                json={
                    "id": user_id,
                    "email": email,
                    "full_name": full_name,
                    "plan_tier": "TRIAL",
                    "trial_uses_remaining": 3,
                },
                timeout=5.0,
            )
            if r_ins.status_code in (200, 201) and r_ins.json():
                return r_ins.json()[0]
    except Exception as exc:
        logger.warning("Profile ensure fallback used: %s", exc)

    return fallback


def format_profile_as_license_info(profile: dict[str, Any]) -> dict[str, Any]:
    """Convert a Supabase profile row into the unified account plan/quota structure."""
    tier = (profile.get("plan_tier") or "TRIAL").upper()
    uses = int(profile.get("trial_uses_remaining", 3))
    unlimited = tier != "TRIAL"
    return {
        "license_key": profile.get("email", "CONTA-SUPABASE"),
        "owner": profile.get("full_name") or profile.get("email", "Usuário"),
        "tier": tier,
        "plan_name": TIER_LABELS.get(tier, tier),
        "uses_remaining": -1 if unlimited else uses,
        "unlimited": unlimited,
        "active": True,
        "auth_source": "supabase",
        "telegram_chat_id": profile.get("telegram_chat_id"),
    }


def supabase_signup(email: str, password: str, full_name: str = "") -> dict[str, Any]:
    """Register a user via public Supabase /auth/v1/signup (respecting email confirmation)."""
    url, anon, _ = _cfg()
    if not (url and anon):
        raise SupabaseAuthError("Supabase não configurado neste ambiente.")

    clean_email = (email or "").strip().lower()
    if "@" not in clean_email or len(password or "") < 6:
        raise SupabaseAuthError("Informe um e-mail válido e senha com pelo menos 6 caracteres.")

    clean_name = full_name.strip()
    try:
        resp = httpx.post(
            f"{url}/auth/v1/signup",
            headers={"apikey": anon, "Content-Type": "application/json"},
            json={
                "email": clean_email,
                "password": password,
                "data": {"full_name": clean_name},
            },
            timeout=8.0,
        )
        if resp.status_code in (400, 422):
            body = resp.json()
            msg = (body.get("msg") or body.get("error_description") or body.get("message") or "").lower()
            if "already" in msg or "exists" in msg or "registered" in msg:
                raise SupabaseAuthError("Este e-mail já está cadastrado. Use a aba Entrar.")
            raise SupabaseAuthError(
                body.get("msg") or body.get("error_description") or body.get("message") or "Erro ao criar conta."
            )
        if resp.status_code not in (200, 201):
            raise SupabaseAuthError(f"Erro ao cadastrar usuário ({resp.status_code}).")

        data = resp.json()
        if isinstance(data.get("identities"), list) and len(data["identities"]) == 0:
            raise SupabaseAuthError(
                "Este e-mail já está cadastrado ou aguardando confirmação. Use Entrar ou cheque sua caixa de entrada."
            )

        if data.get("access_token"):
            user = data.get("user") or {}
            user_id = user.get("id") or data.get("id")
            profile = _ensure_profile(user_id=user_id, email=clean_email, full_name=clean_name)
            return {
                "confirmation_required": False,
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
                "expires_in": int(data.get("expires_in", 3600)),
                "user": {"id": user_id, "email": clean_email, "full_name": profile.get("full_name", clean_name)},
                "profile": profile,
                "license_info": format_profile_as_license_info(profile),
            }

        return {
            "confirmation_required": True,
            "email": clean_email,
            "message": (
                "Conta criada! Verifique sua caixa de entrada para confirmar o e-mail "
                "e liberar suas 3 análises grátis."
            ),
        }
    except SupabaseAuthError:
        raise
    except Exception as exc:
        raise SupabaseAuthError(f"Falha de rede no cadastro Supabase: {exc}") from exc


def supabase_login(
    email: str,
    password: str,
    fallback_name: str = "",
) -> dict[str, Any]:
    """Authenticate an existing Supabase Auth user via password grant and return refresh token."""
    url, anon, _ = _cfg()
    if not (url and anon):
        raise SupabaseAuthError("Supabase não configurado neste ambiente.")

    clean_email = (email or "").strip().lower()
    try:
        resp = httpx.post(
            f"{url}/auth/v1/token?grant_type=password",
            headers={"apikey": anon, "Content-Type": "application/json"},
            json={"email": clean_email, "password": password},
            timeout=8.0,
        )
        if resp.status_code != 200:
            try:
                err_body = resp.json()
                err_desc = (err_body.get("error_description") or err_body.get("msg") or "").lower()
                if "confirm" in err_desc:
                    raise SupabaseAuthError("Confirme seu e-mail antes de entrar (veja sua caixa de entrada).")
            except SupabaseAuthError:
                raise
            except Exception:
                pass
            raise SupabaseAuthError("E-mail ou senha inválidos.")

        data = resp.json()
        access_token = data["access_token"]
        user = data["user"]
        user_id = user["id"]
        name = (user.get("user_metadata") or {}).get("full_name") or fallback_name
        profile = _ensure_profile(user_id=user_id, email=clean_email, full_name=name)
        return {
            "access_token": access_token,
            "refresh_token": data.get("refresh_token", ""),
            "expires_in": int(data.get("expires_in", 3600)),
            "user": {"id": user_id, "email": clean_email, "full_name": profile.get("full_name", name)},
            "profile": profile,
            "license_info": format_profile_as_license_info(profile),
        }
    except SupabaseAuthError:
        raise
    except Exception as exc:
        raise SupabaseAuthError(f"Falha de rede no login Supabase: {exc}") from exc


def supabase_refresh_session(refresh_token: str) -> dict[str, Any]:
    """Exchange a Supabase refresh_token for a fresh access_token and refresh_token."""
    url, anon, _ = _cfg()
    clean_refresh = (refresh_token or "").strip()
    if not (url and anon and clean_refresh):
        raise SupabaseAuthError("Refresh token ausente ou inválido.")

    try:
        resp = httpx.post(
            f"{url}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": anon, "Content-Type": "application/json"},
            json={"refresh_token": clean_refresh},
            timeout=8.0,
        )
        if resp.status_code != 200:
            raise SupabaseAuthError("Sessão expirada. Faça login novamente.")
        data = resp.json()
        access_token = data["access_token"]
        user = data["user"]
        user_id = user["id"]
        email = user.get("email", "")
        name = (user.get("user_metadata") or {}).get("full_name", "")
        profile = _ensure_profile(user_id=user_id, email=email, full_name=name)
        return {
            "access_token": access_token,
            "refresh_token": data.get("refresh_token", clean_refresh),
            "expires_in": int(data.get("expires_in", 3600)),
            "user": {"id": user_id, "email": email, "full_name": profile.get("full_name", name)},
            "profile": profile,
            "license_info": format_profile_as_license_info(profile),
        }
    except SupabaseAuthError:
        raise
    except Exception as exc:
        raise SupabaseAuthError(f"Falha ao renovar sessão: {exc}") from exc


def supabase_get_user_from_jwt(access_token: str) -> dict[str, Any] | None:
    """Validate a Supabase JWT and return user + profile."""
    url, anon, _ = _cfg()
    token = (access_token or "").strip()
    if not (url and anon and token):
        return None

    try:
        resp = httpx.get(
            f"{url}/auth/v1/user",
            headers={"apikey": anon, "Authorization": f"Bearer {token}"},
            timeout=6.0,
        )
        if resp.status_code != 200:
            return None
        user = resp.json()
        user_id = user["id"]
        email = user.get("email", "")
        name = (user.get("user_metadata") or {}).get("full_name", "")
        profile = _ensure_profile(user_id=user_id, email=email, full_name=name)
        return {
            "user": {"id": user_id, "email": email, "full_name": profile.get("full_name", name)},
            "profile": profile,
            "license_info": format_profile_as_license_info(profile),
        }
    except Exception as exc:
        logger.warning("JWT validation failed: %s", exc)
        return None


def check_user_quota(user_ctx: dict[str, Any]) -> dict[str, Any]:
    """Verify if user has remaining trial quota (non-mutating preflight check)."""
    profile = dict(user_ctx["profile"])
    tier = (profile.get("plan_tier") or "TRIAL").upper()
    if tier != "TRIAL":
        return format_profile_as_license_info(profile)

    uses = int(profile.get("trial_uses_remaining", 3))
    if uses <= 0:
        raise SupabaseQuotaExceededError(
            "Suas 3 análises gratuitas acabaram. Aguarde a liberação do checkout Pro ou use as ferramentas gratuitas na página inicial."
        )
    return format_profile_as_license_info(profile)


def decrement_user_quota_atomic(user_ctx: dict[str, Any]) -> dict[str, Any]:
    """Atomically decrement trial quota AFTER a successful analysis via RPC or conditional PATCH."""
    profile = dict(user_ctx["profile"])
    user_id = user_ctx["user"]["id"]
    tier = (profile.get("plan_tier") or "TRIAL").upper()

    if tier != "TRIAL":
        return format_profile_as_license_info(profile)

    uses = int(profile.get("trial_uses_remaining", 3))
    if uses <= 0:
        raise SupabaseQuotaExceededError("Suas 3 análises gratuitas acabaram.")

    url, _, _ = _cfg()
    try:
        r_rpc = httpx.post(
            f"{url}/rest/v1/rpc/consume_trial_use",
            headers=_headers(use_service_role=True),
            json={"p_user_id": user_id},
            timeout=5.0,
        )
        if r_rpc.status_code == 200:
            remaining = r_rpc.json()
            if remaining is None:
                raise SupabaseQuotaExceededError("Suas 3 análises gratuitas acabaram.")
            profile["trial_uses_remaining"] = int(remaining)
            return format_profile_as_license_info(profile)

        if r_rpc.status_code == 404:
            r_patch = httpx.patch(
                f"{url}/rest/v1/profiles?id=eq.{user_id}&plan_tier=eq.TRIAL&trial_uses_remaining=eq.{uses}&trial_uses_remaining=gt.0",
                headers=_headers(use_service_role=True, prefer="return=representation"),
                json={"trial_uses_remaining": uses - 1},
                timeout=5.0,
            )
            if r_patch.status_code in (200, 204):
                rows = r_patch.json() if r_patch.text else []
                if not rows:
                    raise SupabaseQuotaExceededError("Suas 3 análises gratuitas acabaram.")
                profile["trial_uses_remaining"] = int(rows[0]["trial_uses_remaining"])
                return format_profile_as_license_info(profile)
    except SupabaseQuotaExceededError:
        raise
    except Exception as exc:
        logger.warning("Atomic quota decrement failed: %s", exc)

    # Fallback local projection only on transient DB network glitch
    profile["trial_uses_remaining"] = max(0, uses - 1)
    return format_profile_as_license_info(profile)


def save_analysis_record(
    user_id: str,
    symbol: str,
    timeframe: str,
    exchange: str,
    verdict: str,
    regime: str,
    last_price: float,
    drawdown_saved_pct: float,
    window_status: str,
) -> bool:
    """Persist an analysis snapshot in public.analysis_history."""
    url, _, _ = _cfg()
    if not (url and user_id):
        return False
    try:
        resp = httpx.post(
            f"{url}/rest/v1/analysis_history",
            headers=_headers(use_service_role=True),
            json={
                "user_id": user_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "exchange": exchange,
                "verdict": verdict,
                "regime": regime,
                "last_price": last_price,
                "drawdown_saved_pct": drawdown_saved_pct,
                "window_status": window_status,
            },
            timeout=5.0,
        )
        return resp.status_code in (200, 201)
    except Exception as exc:
        logger.warning("save_analysis_record skipped: %s", exc)
        return False


def get_user_analysis_history(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Fetch recent analyses for an authenticated user."""
    url, _, _ = _cfg()
    if not (url and user_id):
        return []
    try:
        resp = httpx.get(
            f"{url}/rest/v1/analysis_history?user_id=eq.{user_id}&order=created_at.desc&limit={limit}",
            headers=_headers(use_service_role=True),
            timeout=5.0,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.warning("get_user_analysis_history skipped: %s", exc)
    return []


def save_trap_check_db(
    symbol: str,
    claimed_side: str,
    verdict_seal: str,
    current_price: float,
    user_id: str | None = None,
) -> bool:
    """Log trap check audits in Supabase Postgres if table exists."""
    url, _, _ = _cfg()
    if not url:
        return False
    try:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "claimed_side": claimed_side,
            "verdict_seal": verdict_seal,
            "current_price": current_price,
        }
        if user_id:
            payload["user_id"] = user_id
        resp = httpx.post(
            f"{url}/rest/v1/trap_checks",
            headers=_headers(use_service_role=True),
            json=payload,
            timeout=4.0,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def update_user_telegram_chat_id(user_id: str, chat_id: str) -> dict[str, Any]:
    """Persist or clear a user's Telegram Chat ID in public.profiles."""
    url, _, _ = _cfg()
    clean_chat = (chat_id or "").strip()
    if len(clean_chat) > 64:
        raise ValueError("Chat ID inválido.")
    if clean_chat and not (clean_chat.lstrip("-").isdigit() or clean_chat.startswith("@")):
        raise ValueError("Informe um Telegram Chat ID numérico válido (descubra enviando /id ao bot).")
    if not (url and user_id):
        raise SupabaseFeatureUnavailableError("Supabase não configurado.")

    resp = httpx.patch(
        f"{url}/rest/v1/profiles?id=eq.{user_id}",
        headers=_headers(use_service_role=True, prefer="return=representation"),
        json={"telegram_chat_id": clean_chat or None, "updated_at": datetime.now(timezone.utc).isoformat()},
        timeout=5.0,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"Erro ao salvar Telegram Chat ID ({resp.status_code}).")
    rows = resp.json() if resp.text else []
    profile = rows[0] if rows else {"id": user_id, "telegram_chat_id": clean_chat or None}
    return {"updated": True, "telegram_chat_id": profile.get("telegram_chat_id")}


def get_profile_by_telegram_chat_id(chat_id: str) -> dict[str, Any] | None:
    """Look up a linked user profile by Telegram chat_id for bot plan gating."""
    url, _, _ = _cfg()
    clean_chat = (chat_id or "").strip()
    if not (url and clean_chat):
        return None
    try:
        resp = httpx.get(
            f"{url}/rest/v1/profiles?telegram_chat_id=eq.{clean_chat}&select=id,email,full_name,plan_tier,trial_uses_remaining,telegram_chat_id&limit=1",
            headers=_headers(use_service_role=True),
            timeout=5.0,
        )
        if resp.status_code == 200:
            rows = resp.json()
            return rows[0] if rows else None
    except Exception as exc:
        logger.warning("get_profile_by_telegram_chat_id failed: %s", exc)
    return None


def list_user_alerts(user_id: str) -> list[dict[str, Any]]:
    """List proactive FX alerts owned by user_id."""
    url, _, _ = _cfg()
    if not (url and user_id):
        return []
    try:
        resp = httpx.get(
            f"{url}/rest/v1/price_alerts?user_id=eq.{user_id}&order=created_at.desc",
            headers=_headers(use_service_role=True),
            timeout=5.0,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("price_alerts read status %s: %s", resp.status_code, resp.text[:160])
    except Exception as exc:
        logger.warning("list_user_alerts failed: %s", exc)
    return []


def create_user_alert(
    user_id: str,
    plan_tier: str,
    symbol: str,
    direction: str,
    target_rate: float,
    channel: str = "telegram",
) -> dict[str, Any]:
    """Create a proactive EUR/BRL or USDT/BRL alert with plan tier limits."""
    url, _, _ = _cfg()
    if not (url and user_id):
        raise SupabaseFeatureUnavailableError("Supabase não configurado.")

    sym_norm = (symbol or "EUR/BRL").strip().upper()
    dir_norm = (direction or "ABOVE").strip().upper()
    ch_norm = (channel or "telegram").strip().lower()
    if sym_norm not in ("EUR/BRL", "USDT/BRL"):
        raise ValueError("Par suportado apenas: EUR/BRL ou USDT/BRL.")
    if dir_norm not in ("ABOVE", "BELOW"):
        raise ValueError("Direção deve ser ABOVE (acima/igual) ou BELOW (abaixo/igual).")
    if ch_norm not in ("telegram", "email"):
        raise ValueError("Canal deve ser telegram ou email.")
    if target_rate <= 0:
        raise ValueError("Cotação alvo deve ser maior que zero.")

    r_check = httpx.get(
        f"{url}/rest/v1/price_alerts?user_id=eq.{user_id}&active=eq.true&select=id",
        headers=_headers(use_service_role=True),
        timeout=5.0,
    )
    if r_check.status_code == 404:
        raise SupabaseFeatureUnavailableError(
            "Tabela price_alerts ainda não existe no Supabase. Execute o arquivo supabase/schema.sql no SQL Editor."
        )
    if r_check.status_code != 200:
        raise SupabaseFeatureUnavailableError(
            f"Não foi possível acessar price_alerts ({r_check.status_code}). Verifique supabase/schema.sql."
        )

    existing = r_check.json()
    tier_upper = (plan_tier or "TRIAL").upper()
    max_active = 1 if tier_upper == "TRIAL" else 20
    if len(existing) >= max_active:
        if tier_upper == "TRIAL":
            raise ValueError(
                "O plano gratuito permite até 1 alerta ativo. Remova o alerta atual para criar outro."
            )
        raise ValueError("Limite máximo de 20 alertas ativos atingido.")

    r_ins = httpx.post(
        f"{url}/rest/v1/price_alerts",
        headers=_headers(use_service_role=True, prefer="return=representation"),
        json={
            "user_id": user_id,
            "symbol": sym_norm,
            "direction": dir_norm,
            "target_rate": round(float(target_rate), 4),
            "channel": ch_norm,
            "active": True,
        },
        timeout=5.0,
    )
    if r_ins.status_code not in (200, 201):
        raise RuntimeError(f"Erro ao criar alerta ({r_ins.status_code}): {r_ins.text[:160]}")
    rows = r_ins.json()
    return rows[0]


def delete_user_alert(user_id: str, alert_id: int) -> bool:
    """Delete an alert owned by user_id."""
    url, _, _ = _cfg()
    if not (url and user_id):
        return False
    resp = httpx.delete(
        f"{url}/rest/v1/price_alerts?id=eq.{int(alert_id)}&user_id=eq.{user_id}",
        headers=_headers(use_service_role=True, prefer="return=representation"),
        timeout=5.0,
    )
    if resp.status_code == 404:
        raise SupabaseFeatureUnavailableError("Tabela price_alerts ainda não existe no Supabase.")
    if resp.status_code == 200:
        return len(resp.json()) > 0
    return resp.status_code == 204


def list_active_alerts_all() -> list[dict[str, Any]]:
    """Fetch all active alerts enriched with each owner's telegram_chat_id and email."""
    url, _, _ = _cfg()
    if not url:
        return []
    try:
        r_alerts = httpx.get(
            f"{url}/rest/v1/price_alerts?active=eq.true&select=id,user_id,symbol,direction,target_rate,channel,last_triggered_at",
            headers=_headers(use_service_role=True),
            timeout=6.0,
        )
        if r_alerts.status_code != 200:
            return []
        alerts = r_alerts.json()
        if not alerts:
            return []

        r_profiles = httpx.get(
            f"{url}/rest/v1/profiles?select=id,email,telegram_chat_id,plan_tier",
            headers=_headers(use_service_role=True),
            timeout=6.0,
        )
        prof_map: dict[str, dict[str, Any]] = {}
        if r_profiles.status_code == 200:
            for p in r_profiles.json():
                prof_map[p["id"]] = p

        enriched = []
        for a in alerts:
            prof = prof_map.get(a["user_id"], {})
            enriched.append(
                {
                    **a,
                    "telegram_chat_id": prof.get("telegram_chat_id"),
                    "email": prof.get("email"),
                    "plan_tier": prof.get("plan_tier", "TRIAL"),
                }
            )
        return enriched
    except Exception as exc:
        logger.warning("list_active_alerts_all failed: %s", exc)
        return []


def mark_alert_triggered(alert_id: int) -> bool:
    """Stamp last_triggered_at after dispatching a price alert."""
    url, _, _ = _cfg()
    if not url:
        return False
    try:
        resp = httpx.patch(
            f"{url}/rest/v1/price_alerts?id=eq.{int(alert_id)}",
            headers=_headers(use_service_role=True),
            json={"last_triggered_at": datetime.now(timezone.utc).isoformat()},
            timeout=5.0,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False


def record_audit_snapshot(
    symbol: str,
    rate: float,
    rsi_14: float,
    signal_label: str,
    source: str,
    is_live: bool,
    min_interval_sec: float = 3600.0,
    observed_at: str | None = None,
) -> bool:
    """Persist an auditable live rate/signal snapshot at most once per symbol per interval (DB-deduplicated)."""
    url, _, _ = _cfg()
    if not is_live or not url:
        return False

    now_mono = time.monotonic()
    with _AUDIT_LOCK:
        last_ts = _LAST_AUDIT_TS.get(symbol, 0.0)
        if last_ts > 0 and (now_mono - last_ts) < min_interval_sec:
            return False

        if last_ts == 0.0:
            try:
                latest_resp = httpx.get(
                    f"{url}/rest/v1/alert_audit_log",
                    headers=_headers(use_service_role=True),
                    params={
                        "select": "created_at",
                        "symbol": f"eq.{symbol}",
                        "order": "created_at.desc",
                        "limit": "1",
                    },
                    timeout=3.0,
                )
                if latest_resp.status_code == 200:
                    existing = latest_resp.json()
                    if existing and existing[0].get("created_at"):
                        ts_raw = str(existing[0]["created_at"]).replace("Z", "+00:00")
                        dt = datetime.fromisoformat(ts_raw)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - dt).total_seconds() < min_interval_sec:
                            _LAST_AUDIT_TS[symbol] = now_mono
                            return False
            except Exception as exc:
                logger.debug("Audit log DB dedup lookup skipped: %s", exc)

        _LAST_AUDIT_TS[symbol] = now_mono

    try:
        resp = httpx.post(
            f"{url}/rest/v1/alert_audit_log",
            headers=_headers(use_service_role=True),
            json={
                "symbol": symbol,
                "rate": round(float(rate), 4),
                "rsi_14": round(float(rsi_14), 2),
                "signal_label": signal_label[:140],
                "source": source[:120],
                "is_live": True,
            },
            timeout=4.0,
        )
        if resp.status_code not in (200, 201):
            logger.warning("alert_audit_log insert status %s (apply schema.sql if missing)", resp.status_code)
            return False
        return True
    except Exception as exc:
        logger.warning("record_audit_snapshot skipped: %s", exc)
        return False


def get_recent_audit_log(limit: int = 12) -> list[dict[str, Any]]:
    """Return latest auditable market snapshots from Supabase Postgres with provenance metadata."""
    url, _, _ = _cfg()
    if not url:
        return []
    try:
        resp = httpx.get(
            f"{url}/rest/v1/alert_audit_log?select=id,symbol,rate,rsi_14,signal_label,source,is_live,created_at&order=created_at.desc&limit={int(limit)}",
            headers=_headers(use_service_role=True),
            timeout=5.0,
        )
        if resp.status_code == 200:
            rows = resp.json()
            enriched: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["source"] = item.get("source") or "binance (EUR/USDT × USDT/BRL)"
                item["observed_at"] = item.get("created_at")
                item["ingested_at"] = item.get("created_at")
                item["engine_version"] = AUDIT_ENGINE_VERSION
                item["methodology"] = AUDIT_METHODOLOGY
                enriched.append(item)
            return enriched
    except Exception as exc:
        logger.warning("get_recent_audit_log skipped: %s", exc)
    return []
