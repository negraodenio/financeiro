"""FastAPI backend for Scudex — hardened, deterministic, non-custodial SaaS."""

from __future__ import annotations

from collections import deque
import hmac
import os
from pathlib import Path
import threading
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agents import run_committee
from backtester import STRATEGY_META, run_ab_backtest
from crisis_advisor import (
    build_crisis_shield_plan,
    build_fx_treasury_signal,
    evaluate_impulse_brake,
    evaluate_trap_check,
    format_whatsapp_report,
    simulate_smart_dca_brl,
)
from db import (
    AUDIT_ENGINE_VERSION,
    AUDIT_METHODOLOGY,
    SupabaseAuthError,
    SupabaseFeatureUnavailableError,
    SupabaseQuotaExceededError,
    check_tables_ready,
    check_user_quota,
    create_user_alert,
    decrement_user_quota_atomic,
    delete_user_alert,
    get_recent_audit_log,
    get_user_analysis_history,
    is_supabase_configured,
    list_user_alerts,
    record_audit_snapshot,
    save_analysis_record,
    save_trap_check_db,
    supabase_get_user_from_jwt,
    supabase_login,
    supabase_refresh_session,
    supabase_signup,
    update_user_telegram_chat_id,
)
from europe_bridge import build_europe_brazil_bridge
from leak_xray import analyze_leak_xray, get_sample_leak_csv
from market_data import (
    FX_SNAPSHOT_LIMIT,
    SUPPORTED_EXCHANGES,
    TIMEFRAME_SECONDS,
    MarketDataUnavailableError,
    fetch_market_snapshot,
)
from telegram_bot import check_and_dispatch_price_alerts, format_telegram_report, handle_update, send_telegram_message

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Scudex — Câmbio Global, Raio-X de Vazamentos & Disjuntor Quant",
    version="2.0.0",
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[tuple[str, str], deque[float]] = {}


def clear_rate_limits() -> None:
    """Reset in-memory sliding-window rate limiter buckets (used in tests)."""
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return (request.client.host if request.client else None) or "unknown"


def _enforce_rate_limit(request: Request, bucket: str, limit: int, window_sec: int = 60) -> None:
    ip = _client_ip(request)
    key = (bucket, ip)
    now_mono = time.monotonic()
    with _RATE_LOCK:
        dq = _RATE_BUCKETS.get(key)
        if dq is None:
            dq = deque()
            _RATE_BUCKETS[key] = dq
        cutoff = now_mono - window_sec
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Muitas requisições em pouco tempo. Aguarde alguns segundos e tente novamente.",
            )
        dq.append(now_mono)


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


@app.exception_handler(MarketDataUnavailableError)
async def handle_market_unavailable(_request: Request, exc: MarketDataUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


class AnalyzeRequest(BaseModel):
    exchange: str = Field(default="binance", description="CCXT exchange ID")
    symbol: str = Field(default="USDT/BRL", description="Trading pair e.g. USDT/BRL")
    timeframe: str = Field(default="1h", description="Candle timeframe")
    strategy: str = Field(default="trend_ema_crossover", description="Backtest strategy ID")
    capital_usdt: float = Field(default=10000.0, gt=10.0, le=100_000_000.0)
    available_capital_brl: float = Field(default=10000.0, gt=10.0, le=100_000_000.0)
    risk_per_trade_pct: float = Field(default=1.0, ge=0.1, le=10.0)
    limit: int = Field(default=400, ge=100, le=1000)
    monthly_contribution_brl: float = Field(default=500.0, ge=50.0, le=1_000_000.0)
    monthly_expenses_brl: float = Field(default=3000.0, ge=100.0, le=1_000_000.0)
    has_emergency_fund: bool = Field(default=False)


class TelegramSendRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=3500)


class TrapCheckRequest(BaseModel):
    symbol: str = Field(default="BTC/BRL")
    claimed_side: str = Field(default="BUY")
    timeframe: str = Field(default="1h")
    exchange: str = Field(default="binance")


class FxTreasuryRequest(BaseModel):
    monthly_usd_volume: float = Field(default=2500.0, ge=50.0, le=1_000_000.0)
    profile: str = Field(default="EARN_USD")
    timeframe: str = Field(default="4h")


class SupabaseSignupRequest(BaseModel):
    email: str = Field(..., min_length=4)
    password: str = Field(..., min_length=6)
    full_name: str = Field(default="")


class SupabaseLoginRequest(BaseModel):
    email: str = Field(..., min_length=4)
    password: str = Field(..., min_length=6)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10)


class EuropeBridgeRequest(BaseModel):
    amount_eur: float = Field(default=1500.0, ge=50.0, le=1_000_000.0)
    investment_type: str = Field(default="SELIC")
    timeframe: str = Field(default="4h")


class CreateAlertRequest(BaseModel):
    symbol: str = Field(default="EUR/BRL")
    direction: str = Field(default="ABOVE")
    target_rate: float = Field(..., gt=0.01, le=1000.0)
    channel: str = Field(default="telegram")


class UpdateTelegramRequest(BaseModel):
    telegram_chat_id: str = Field(default="")


class ImpulseBrakeRequest(BaseModel):
    symbol: str = Field(default="BTC/BRL")
    claimed_side: str = Field(default="BUY")
    timeframe: str = Field(default="1h")
    exchange: str = Field(default="binance")
    planned_amount: float = Field(default=1000.0, ge=10.0, le=10_000_000.0)
    source_origin: str = Field(default="grupo_whatsapp")


class LeakXrayRequest(BaseModel):
    csv_text: str = Field(default="", max_length=512_000)
    provider: str = Field(default="auto")
    base_currency: str = Field(default="EUR")
    use_sample: bool = Field(default=False)


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _optional_user_ctx(authorization: str | None) -> dict[str, Any] | None:
    token = _extract_bearer(authorization)
    if not token:
        return None
    return supabase_get_user_from_jwt(token)


DEFAULT_SITE_URL = "https://scudex.app"


def _public_site_url() -> str:
    env_site = (os.getenv("SITE_URL") or "").strip().rstrip("/")
    if env_site.startswith(("http://", "https://")):
        return env_site
    cta = (os.getenv("TELEGRAM_CTA_URL") or "").strip().rstrip("/")
    if cta.startswith("https://"):
        return cta
    return DEFAULT_SITE_URL


def _require_user_ctx(authorization: str | None) -> dict[str, Any]:
    ctx = _optional_user_ctx(authorization)
    if not ctx:
        raise HTTPException(status_code=401, detail="Sessão ausente, inválida ou expirada. Faça login novamente.")
    return ctx


@app.get("/", response_class=HTMLResponse)
def serve_landing() -> HTMLResponse:
    """Serve the focused commercial SaaS landing page with dynamic canonical/OG origin."""
    html = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    site_url = _public_site_url()
    if site_url != DEFAULT_SITE_URL:
        html = html.replace(DEFAULT_SITE_URL, site_url)
    return HTMLResponse(content=html)


@app.get("/app")
def serve_dashboard() -> FileResponse:
    """Serve the interactive terminal."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def serve_favicon() -> FileResponse:
    """Serve browser favicon without 404."""
    return FileResponse(STATIC_DIR / "assets" / "favicon.ico", media_type="image/x-icon")


@app.get("/robots.txt", response_class=PlainTextResponse)
def serve_robots() -> PlainTextResponse:
    """Serve search engine crawl rules keeping authenticated app and API endpoints out of index."""
    site_url = _public_site_url()
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /app\n"
        "Disallow: /api/\n\n"
        f"Sitemap: {site_url}/sitemap.xml\n"
    )
    return PlainTextResponse(content=body, media_type="text/plain; charset=utf-8")


@app.get("/sitemap.xml")
def serve_sitemap() -> Response:
    """Serve dynamic XML sitemap for public indexable routes."""
    site_url = _public_site_url()
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{site_url}/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{site_url}/termos</loc>
    <changefreq>monthly</changefreq>
    <priority>0.3</priority>
  </url>
  <url>
    <loc>{site_url}/privacidade</loc>
    <changefreq>monthly</changefreq>
    <priority>0.3</priority>
  </url>
</urlset>"""
    return Response(content=xml, media_type="application/xml; charset=utf-8")


@app.get("/termos", response_class=HTMLResponse)
def serve_terms() -> HTMLResponse:
    """Serve clean Portuguese Terms of Use with non-custodial & regulatory transparency notices."""
    site_url = _public_site_url()
    html = f"""<!DOCTYPE html>
<html lang="pt-BR" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Termos de Uso — Scudex</title>
  <meta name="description" content="Termos de Uso da Scudex: software informativo não-custodial de câmbio, auditoria técnica e controle de risco." />
  <meta name="robots" content="index, follow" />
  <link rel="canonical" href="{site_url}/termos" />
  <link rel="icon" type="image/svg+xml" href="/static/assets/favicon.svg" />
  <link rel="icon" type="image/x-icon" href="/favicon.ico" />
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#060911] text-slate-200 min-h-screen antialiased">
  <main class="max-w-3xl mx-auto px-5 py-12 space-y-6">
    <div class="flex items-center justify-between border-b border-slate-800 pb-4">
      <a href="/" class="text-emerald-400 font-bold text-sm hover:underline">← Voltar para a Scudex</a>
      <span class="text-xs text-slate-500">Versão Beta Informativa</span>
    </div>
    <h1 class="text-3xl font-extrabold text-white">Termos de Uso — Scudex</h1>
    <div class="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs text-amber-200 leading-relaxed">
      Aviso de Pré-Produção: este documento define as diretrizes operacionais do software em fase beta gratuita e deverá ser revisado por assessoria jurídica antes da habilitação de cobrança comercial na UE/Brasil.
    </div>
    <section class="space-y-2 text-sm text-slate-300 leading-relaxed">
      <h2 class="text-lg font-bold text-white">1. Software Informativo e Não-Custodial</h2>
      <p>A Scudex é uma ferramenta de cálculo matemático, leitura técnica de liquidez e comparação informativa de taxas cambiais e métricas financeiras públicas. <strong>Nunca recebemos, custodiamos, transferimos ou operamos recursos financeiros de usuários.</strong></p>
    </section>
    <section class="space-y-2 text-sm text-slate-300 leading-relaxed">
      <h2 class="text-lg font-bold text-white">2. Ausência de Recomendação de Investimento (CVM / CMVM / MiFID II)</h2>
      <p>Os resultados, alertas de faixa de preço, simulações de Selic/B3 em Euro e auditorias técnicas geradas pelo sistema têm caráter estritamente educacional e informativo. Não constituem consultoria de valores mobiliários (Res. CVM 19), análise de valores mobiliários (Res. CVM 20), gestão discricionária ou aconselhamento financeiro sob MiFID II / CMVM.</p>
    </section>
    <section class="space-y-2 text-sm text-slate-300 leading-relaxed">
      <h2 class="text-lg font-bold text-white">3. Remessas, Câmbio e Impostos</h2>
      <p>Simulações de remessa Europa → Brasil utilizam parâmetros médios estimados para fins comparativos. Residentes no exterior devem observar as regras fiscais e cambiais aplicáveis (incluindo Declaração de Saída Definitiva, conta CDE, IOF e regulamentação vigente do Banco Central do Brasil, como a Res. BCB 521/2025) antes de realizar qualquer transferência ou investimento.</p>
    </section>
    <section class="space-y-2 text-sm text-slate-300 leading-relaxed">
      <h2 class="text-lg font-bold text-white">4. Disponibilidade de Dados e Limitação de Responsabilidade</h2>
      <p>As cotações dependem de fontes públicas externas (corretoras e Banco Central). Quando indisponíveis, o serviço informa explicitamente a indisponibilidade em vez de inventar cotações ao vivo.</p>
    </section>
  </main>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/privacidade", response_class=HTMLResponse)
def serve_privacy() -> HTMLResponse:
    """Serve clean Portuguese Privacy Policy aligned with LGPD and GDPR transparency principles."""
    site_url = _public_site_url()
    html = f"""<!DOCTYPE html>
<html lang="pt-BR" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Política de Privacidade — Scudex</title>
  <meta name="description" content="Política de Privacidade da Scudex alinhada aos princípios da LGPD e GDPR." />
  <meta name="robots" content="index, follow" />
  <link rel="canonical" href="{site_url}/privacidade" />
  <link rel="icon" type="image/svg+xml" href="/static/assets/favicon.svg" />
  <link rel="icon" type="image/x-icon" href="/favicon.ico" />
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#060911] text-slate-200 min-h-screen antialiased">
  <main class="max-w-3xl mx-auto px-5 py-12 space-y-6">
    <div class="flex items-center justify-between border-b border-slate-800 pb-4">
      <a href="/" class="text-emerald-400 font-bold text-sm hover:underline">← Voltar para a Scudex</a>
      <span class="text-xs text-slate-500">LGPD &amp; GDPR</span>
    </div>
    <h1 class="text-3xl font-extrabold text-white">Política de Privacidade (LGPD / GDPR)</h1>
    <div class="p-4 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs text-amber-200 leading-relaxed">
      Aviso de Pré-Produção: texto informativo para o beta fechado; revisar região final de hospedagem e encarregado de dados (DPO) com advogado antes da abertura comercial.
    </div>
    <section class="space-y-2 text-sm text-slate-300 leading-relaxed">
      <h2 class="text-lg font-bold text-white">1. Quais Dados Coletamos</h2>
      <p>Ao criar uma conta, armazenamos apenas seu endereço de e-mail, nome opcional, parâmetros financeiros simulados informados por você (aporte, despesas mensais), histórico de simulações, alertas configurados e, caso deseje receber notificações, seu Telegram Chat ID.</p>
      <p><strong>Raio-X de Vazamentos (CSV):</strong> arquivos CSV enviados no simulador público são processados exclusivamente em memória RAM para o cálculo imediato das métricas agregadas e descartados logo após a resposta — nenhuma linha individual, saldo ou identificação bancária é salva em disco ou banco de dados.</p>
    </section>
    <section class="space-y-2 text-sm text-slate-300 leading-relaxed">
      <h2 class="text-lg font-bold text-white">2. Base Legal e Finalidade</h2>
      <p>Os dados são tratados exclusivamente com base no seu consentimento e para execução do serviço solicitado (autenticação de conta, controle de cota gratuita, salvamento de histórico e envio de alertas solicitados pelo usuário).</p>
    </section>
    <section class="space-y-2 text-sm text-slate-300 leading-relaxed">
      <h2 class="text-lg font-bold text-white">3. Armazenamento e Segurança</h2>
      <p>Autenticação e banco de dados utilizam Supabase (PostgreSQL) com isolamento por usuário via Row Level Security (RLS). Não armazenamos senhas em texto puro nem chaves privadas de corretoras ou contas bancárias.</p>
    </section>
    <section class="space-y-2 text-sm text-slate-300 leading-relaxed">
      <h2 class="text-lg font-bold text-white">4. Seus Direitos (LGPD Art. 18 &amp; GDPR)</h2>
      <p>Você pode solicitar acesso, correção, portabilidade ou exclusão definitiva da sua conta e de todos os dados associados a qualquer momento.</p>
    </section>
  </main>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/api/auth/signup")
def auth_signup(request: Request, req: SupabaseSignupRequest) -> dict[str, Any]:
    """Create a Supabase user account via public signup (honoring email confirmation)."""
    _enforce_rate_limit(request, bucket="auth", limit=6, window_sec=60)
    try:
        return supabase_signup(email=req.email, password=req.password, full_name=req.full_name)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auth/login")
def auth_login(request: Request, req: SupabaseLoginRequest) -> dict[str, Any]:
    """Authenticate an existing Supabase user account via email and password."""
    _enforce_rate_limit(request, bucket="auth", limit=6, window_sec=60)
    try:
        return supabase_login(email=req.email, password=req.password)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/auth/refresh")
def auth_refresh(request: Request, req: RefreshTokenRequest) -> dict[str, Any]:
    """Refresh an expired Supabase JWT using refresh_token."""
    _enforce_rate_limit(request, bucket="auth", limit=6, window_sec=60)
    try:
        return supabase_refresh_session(req.refresh_token)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.get("/api/auth/me")
def auth_me(authorization: str | None = Header(default=None, alias="Authorization")) -> dict[str, Any]:
    """Return authenticated Supabase user profile and quota status."""
    return _require_user_ctx(authorization)


@app.get("/api/history")
def user_history(authorization: str | None = Header(default=None, alias="Authorization")) -> dict[str, Any]:
    """Fetch saved analysis history for the logged-in Supabase user."""
    ctx = _optional_user_ctx(authorization)
    if not ctx:
        return {"authenticated": False, "history": []}
    rows = get_user_analysis_history(ctx["user"]["id"], limit=20)
    return {"authenticated": True, "history": rows}


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    """Return service health, supported exchanges, strategies, and deterministic engine status."""
    sb = check_tables_ready()
    allow_synth = os.getenv("ALLOW_SYNTHETIC_FALLBACK", "false").strip().lower() in ("1", "true", "yes")
    return {
        "status": "ok",
        "exchanges": list(SUPPORTED_EXCHANGES),
        "timeframes": list(TIMEFRAME_SECONDS.keys()),
        "strategies": STRATEGY_META,
        "engine_name": "Scudex Quant v2 (Motor Determinístico)",
        "engine_status": "active",
        "allow_synthetic_fallback": allow_synth,
        "supabase_configured": is_supabase_configured(),
        "supabase_tables_ready": sb["tables_ready"],
        "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
        "telegram_bot_username": (os.getenv("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@"),
    }


@app.post("/api/telegram/send")
def dispatch_telegram(
    request: Request,
    req: TelegramSendRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Send an authenticated paid user's bulletin to their own linked Telegram Chat ID."""
    _enforce_rate_limit(request, bucket="account", limit=20, window_sec=60)
    user_ctx = _require_user_ctx(authorization)
    tier = (user_ctx["profile"].get("plan_tier") or "TRIAL").upper()
    if tier == "TRIAL":
        raise HTTPException(
            status_code=403,
            detail="Envio manual de boletins no Telegram é exclusivo para planos pagos.",
        )

    user_chat_id = (user_ctx["profile"].get("telegram_chat_id") or "").strip()
    if not user_chat_id:
        raise HTTPException(
            status_code=400,
            detail="Vincule primeiro seu Telegram Chat ID na aba Alertas.",
        )

    server_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    return send_telegram_message(
        text=req.text,
        bot_token=server_token or None,
        chat_id=user_chat_id,
    )


@app.post("/api/telegram/webhook")
def telegram_webhook(
    update: dict[str, Any],
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None, alias="X-Telegram-Bot-Api-Secret-Token"
    ),
) -> dict[str, Any]:
    """Receive Telegram Bot API webhook updates on serverless deployments (Vercel)."""
    expected_secret = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    server_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not expected_secret or not server_token:
        raise HTTPException(
            status_code=503,
            detail="Webhook do Telegram não configurado no servidor.",
        )
    provided = (x_telegram_bot_api_secret_token or "").strip()
    if not provided or not hmac.compare_digest(provided, expected_secret):
        raise HTTPException(status_code=403, detail="Token de webhook inválido.")
    handle_update(update, bot_token=server_token)
    return {"ok": True}


@app.get("/api/cron/check-alerts")
def cron_check_alerts(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Run proactive EUR/BRL and USDT/BRL price alert checks (protected by CRON_SECRET)."""
    expected_secret = (os.getenv("CRON_SECRET") or "").strip()
    if not expected_secret:
        raise HTTPException(
            status_code=503,
            detail="Cron de alertas não configurado no servidor.",
        )
    token = _extract_bearer(authorization) or ""
    if not token or not hmac.compare_digest(token, expected_secret):
        raise HTTPException(status_code=401, detail="Autorização de cron inválida.")
    return check_and_dispatch_price_alerts()


@app.post("/api/profile/telegram")
def save_profile_telegram(
    request: Request,
    req: UpdateTelegramRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Link or clear the authenticated user's Telegram Chat ID for proactive alerts."""
    _enforce_rate_limit(request, bucket="account", limit=20, window_sec=60)
    user_ctx = _require_user_ctx(authorization)
    try:
        return update_user_telegram_chat_id(user_ctx["user"]["id"], req.telegram_chat_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SupabaseFeatureUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/alerts")
def get_alerts(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """List proactive EUR/BRL and USDT/BRL price alerts for the authenticated user."""
    _enforce_rate_limit(request, bucket="account", limit=20, window_sec=60)
    user_ctx = _require_user_ctx(authorization)
    alerts = list_user_alerts(user_ctx["user"]["id"])
    return {
        "alerts": alerts,
        "telegram_chat_id": user_ctx["profile"].get("telegram_chat_id"),
        "plan_tier": (user_ctx["profile"].get("plan_tier") or "TRIAL").upper(),
    }


@app.post("/api/alerts")
def create_alert(
    request: Request,
    req: CreateAlertRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Create a proactive EUR/BRL or USDT/BRL alert."""
    _enforce_rate_limit(request, bucket="account", limit=20, window_sec=60)
    user_ctx = _require_user_ctx(authorization)
    tier = (user_ctx["profile"].get("plan_tier") or "TRIAL").upper()
    try:
        created = create_user_alert(
            user_id=user_ctx["user"]["id"],
            plan_tier=tier,
            symbol=req.symbol,
            direction=req.direction,
            target_rate=req.target_rate,
            channel=req.channel,
        )
        return {"alert": created}
    except SupabaseFeatureUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/alerts/{alert_id}")
def remove_alert(
    alert_id: int,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Delete an alert owned by the authenticated user."""
    _enforce_rate_limit(request, bucket="account", limit=20, window_sec=60)
    user_ctx = _require_user_ctx(authorization)
    try:
        deleted = delete_user_alert(user_ctx["user"]["id"], alert_id)
    except SupabaseFeatureUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Alerta não encontrado.")
    return {"deleted": True, "id": alert_id}


@app.post("/api/public/trap-check")
def public_trap_check(
    request: Request,
    req: TrapCheckRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Public deterministic endpoint: audit a buy/sell tip against live orderbook and regime."""
    _enforce_rate_limit(request, bucket="public", limit=20, window_sec=60)
    tf = req.timeframe if req.timeframe in TIMEFRAME_SECONDS else "1h"
    snapshot = fetch_market_snapshot(
        symbol=req.symbol,
        timeframe=tf,
        exchange_id=req.exchange,
        limit=300,
    )
    committee = run_committee(snapshot=snapshot, capital_usdt=5000.0, risk_per_trade_pct=1.0)
    snapshot.pop("df", None)
    result = evaluate_trap_check(
        snapshot=snapshot,
        committee=committee,
        claimed_side=req.claimed_side,
    )
    ctx = _optional_user_ctx(authorization)
    save_trap_check_db(
        symbol=result["symbol"],
        claimed_side=result["claimed_side"],
        verdict_seal=result["verdict_seal"],
        current_price=result["current_price"],
        user_id=ctx["user"]["id"] if ctx else None,
    )
    return result


@app.post("/api/public/impulse-brake")
def public_impulse_brake(
    request: Request,
    req: ImpulseBrakeRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Public deterministic Impulse Brake: 10-min pause, real historical lookback & 2xATR swing risk."""
    _enforce_rate_limit(request, bucket="public", limit=20, window_sec=60)
    tf = req.timeframe if req.timeframe in TIMEFRAME_SECONDS else "1h"
    snapshot = fetch_market_snapshot(
        symbol=req.symbol,
        timeframe=tf,
        exchange_id=req.exchange,
        limit=300,
    )
    committee = run_committee(snapshot=snapshot, capital_usdt=5000.0, risk_per_trade_pct=1.0)
    df = snapshot.pop("df", None)
    result = evaluate_impulse_brake(
        snapshot=snapshot,
        committee=committee,
        df=df,
        planned_amount=req.planned_amount,
        claimed_side=req.claimed_side,
        source_origin=req.source_origin,
    )
    ctx = _optional_user_ctx(authorization)
    save_trap_check_db(
        symbol=result["symbol"],
        claimed_side=req.claimed_side,
        verdict_seal=f"FREIO_{result['brake_level']}",
        current_price=result["trap_audit"]["current_price"],
        user_id=ctx["user"]["id"] if ctx else None,
    )
    return result


@app.post("/api/public/leak-xray")
def public_leak_xray(request: Request, req: LeakXrayRequest) -> dict[str, Any]:
    """RAM-only CSV Leak X-Ray analyzer (Wise, Revolut, Binance, Generic). Zero disk/DB persistence."""
    _enforce_rate_limit(request, bucket="xray", limit=12, window_sec=60)
    csv_payload = get_sample_leak_csv() if req.use_sample else req.csv_text
    if not csv_payload or not csv_payload.strip():
        raise HTTPException(
            status_code=400,
            detail="Envie o conteúdo do arquivo CSV ou clique em 'Testar com Exemplo Ilustrativo'.",
        )
    try:
        return analyze_leak_xray(
            csv_text=csv_payload,
            provider=req.provider,
            base_currency=req.base_currency,
            allow_network=True,
            is_sample=bool(req.use_sample),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/public/europe-bridge")
def public_europe_bridge(request: Request, req: EuropeBridgeRequest) -> dict[str, Any]:
    """Europe -> Brazil FX snapshot, regulated remittance comparison, Selic break-even & B3 assets."""
    _enforce_rate_limit(request, bucket="public", limit=20, window_sec=60)
    tf = req.timeframe if req.timeframe in TIMEFRAME_SECONDS else "4h"
    result = build_europe_brazil_bridge(
        amount_eur=req.amount_eur,
        investment_type=req.investment_type,
        timeframe=tf,
    )
    fx = result["fx"]
    if fx.get("is_live"):
        record_audit_snapshot(
            symbol="EUR/BRL",
            rate=fx["eur_brl_rate"],
            rsi_14=fx["rsi_14"],
            signal_label=result["remittance_label"],
            source=fx["source"],
            is_live=True,
            observed_at=fx.get("observed_at"),
        )
    return result


@app.post("/api/public/fx-calculator")
def public_fx_calculator(request: Request, req: FxTreasuryRequest) -> dict[str, Any]:
    """Public FX spread calculator for USD earners and buyers using live USDT/BRL."""
    _enforce_rate_limit(request, bucket="public", limit=20, window_sec=60)
    tf = req.timeframe if req.timeframe in TIMEFRAME_SECONDS else "4h"
    snapshot = fetch_market_snapshot(
        symbol="USDT/BRL",
        timeframe=tf,
        exchange_id="binance",
        limit=FX_SNAPSHOT_LIMIT,
    )
    snapshot.pop("df", None)
    return build_fx_treasury_signal(
        snapshot=snapshot,
        monthly_usd_volume=req.monthly_usd_volume,
        profile=req.profile,
    )


@app.get("/api/public/audit-log")
def public_audit_log(request: Request) -> dict[str, Any]:
    """Return latest auditable market snapshots stored in Postgres with provenance metadata."""
    _enforce_rate_limit(request, bucket="public", limit=20, window_sec=60)
    return {
        "entries": get_recent_audit_log(limit=12),
        "engine_version": AUDIT_ENGINE_VERSION,
        "methodology": AUDIT_METHODOLOGY,
    }


@app.post("/api/analyze")
def analyze_market(
    request: Request,
    req: AnalyzeRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """Run authenticated live CCXT ingestion, deterministic committee, and fair A/B backtest."""
    _enforce_rate_limit(request, bucket="analyze", limit=15, window_sec=60)
    user_ctx = _require_user_ctx(authorization)

    if req.timeframe not in TIMEFRAME_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Timeframe inválido: {req.timeframe}. Use um de: {list(TIMEFRAME_SECONDS.keys())}",
        )

    try:
        check_user_quota(user_ctx)
    except SupabaseQuotaExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    snapshot = fetch_market_snapshot(
        symbol=req.symbol,
        timeframe=req.timeframe,
        exchange_id=req.exchange,
        limit=req.limit,
    )
    committee = run_committee(
        snapshot=snapshot,
        capital_usdt=req.capital_usdt,
        risk_per_trade_pct=req.risk_per_trade_pct,
    )
    df = snapshot.pop("df")
    backtest = run_ab_backtest(
        df=df,
        strategy=req.strategy,
        capital_usdt=req.capital_usdt,
        risk_per_trade_pct=req.risk_per_trade_pct,
        timeframe=req.timeframe,
    )
    crisis_shield = build_crisis_shield_plan(
        snapshot=snapshot,
        committee=committee,
        available_capital_brl=req.available_capital_brl,
        monthly_contribution_brl=req.monthly_contribution_brl,
        monthly_expenses_brl=req.monthly_expenses_brl,
        has_emergency_fund=req.has_emergency_fund,
    )
    smart_dca = simulate_smart_dca_brl(
        df=df,
        monthly_contribution_brl=req.monthly_contribution_brl,
        timeframe=req.timeframe,
    )
    whatsapp_report = format_whatsapp_report(
        snapshot=snapshot,
        committee=committee,
        crisis_plan=crisis_shield,
        dca_sim=smart_dca,
    )
    committee["telegram_alert_preview"] = format_telegram_report(snapshot, committee, backtest)

    try:
        license_info = decrement_user_quota_atomic(user_ctx)
    except SupabaseQuotaExceededError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    save_analysis_record(
        user_id=user_ctx["user"]["id"],
        symbol=snapshot["symbol"],
        timeframe=req.timeframe,
        exchange=snapshot["served_exchange"],
        verdict=committee["verdict"],
        regime=committee["regime"],
        last_price=snapshot["last_price"],
        drawdown_saved_pct=backtest["mode_b_guarded"]["drawdown_saved_pct"],
        window_status=crisis_shield["window_status"],
    )

    return {
        "market": snapshot,
        "committee": committee,
        "backtest": backtest,
        "crisis_shield": crisis_shield,
        "smart_dca": smart_dca,
        "whatsapp_report": whatsapp_report,
        "license_info": license_info,
    }
