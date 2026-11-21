"""Telegram interactive bot (InlineKeyboard menus, /alerta, plan gate) & proactive FX alert dispatcher."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import sys
import threading
import time
from typing import Any

import httpx

from agents import _load_dotenv, run_committee
from backtester import run_ab_backtest
from crisis_advisor import (
    build_crisis_shield_plan,
    build_fx_treasury_signal,
    evaluate_impulse_brake,
    evaluate_trap_check,
    format_whatsapp_report,
    simulate_smart_dca_brl,
)
from db import (
    SupabaseFeatureUnavailableError,
    create_user_alert,
    delete_user_alert,
    get_profile_by_telegram_chat_id,
    list_active_alerts_all,
    list_user_alerts,
    mark_alert_triggered,
    record_audit_snapshot,
)
from europe_bridge import build_europe_brazil_bridge, evaluate_expensive_moment, fetch_eur_brl_snapshot
from market_data import (
    FX_SNAPSHOT_LIMIT,
    TIMEFRAME_SECONDS,
    MarketDataUnavailableError,
    fetch_market_snapshot,
)

_load_dotenv()

VERDICT_ICONS: dict[str, str] = {
    "APPROVE_LONG": "🟢",
    "APPROVE_SHORT": "🔴",
    "REDUCE_SIZE_50%": "🟡",
    "VETO_PROTECTION": "🛡️",
    "WAIT_NEUTRAL": "⚪",
}

_CMD_LOCK = threading.Lock()
_LAST_CMD_BY_CHAT: dict[str, float] = {}


def clear_command_cooldowns() -> None:
    """Clear per-chat command cooldown timestamps (used in automated tests)."""
    with _CMD_LOCK:
        _LAST_CMD_BY_CHAT.clear()


def _cta_url() -> str:
    return (os.getenv("TELEGRAM_CTA_URL") or "http://localhost:8000").strip().rstrip("/")


def _maybe_url_button(label: str, path: str = "") -> list[dict[str, str]]:
    """Telegram rejects http://localhost in inline_keyboard url buttons; only attach URL buttons for HTTPS origins."""
    cta = _cta_url()
    if cta.startswith("https://"):
        return [{"text": label, "url": f"{cta}{path}"}]
    return []


def _main_menu_keyboard() -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = [
        [
            {"text": "💱 Meu Câmbio (EUR→BR)", "callback_data": "cb:menu_euro"},
            {"text": "💵 Meu Câmbio (Dólar)", "callback_data": "cb:menu_fx"},
        ],
        [
            {"text": "🛡️ Meu Escudo (Ciladas)", "callback_data": "cb:menu_trap"},
            {"text": "🛑 Freio de Impulso", "callback_data": "cb:menu_brake"},
        ],
        [
            {"text": "📊 Meu Quant (Pro)", "callback_data": "cb:menu_quant"},
            {"text": "🩺 Raio-X Vazamentos", "callback_data": "cb:menu_xray"},
        ],
        [
            {"text": "🔔 Meus Alertas (USD/EUR)", "callback_data": "cb:menu_alerts"},
            {"text": "🆔 Plano & Conta", "callback_data": "cb:menu_account"},
        ],
        [{"text": "❓ Metodologia & Suporte", "callback_data": "cb:menu_support"}],
    ]
    url_row = _maybe_url_button("🌐 Abrir Site & Planos Pro (€ 14 / R$ 79)", "/#planos")
    if url_row:
        rows.append(url_row)
    return {"inline_keyboard": rows}


def _brake_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🛑 Freio BTC/BRL (R$ 1.000)", "callback_data": "cb:brake:BTC/BRL:1000"},
                {"text": "🛑 Freio SOL/BRL (R$ 1.000)", "callback_data": "cb:brake:SOL/BRL:1000"},
            ],
            [
                {"text": "🛑 Freio USDT/BRL (R$ 2.000)", "callback_data": "cb:brake:USDT/BRL:2000"},
                {"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"},
            ],
        ]
    }


def _euro_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "€ 500 (Selic)", "callback_data": "cb:euro:500:SELIC"},
                {"text": "€ 1.500 (Selic)", "callback_data": "cb:euro:1500:SELIC"},
            ],
            [
                {"text": "€ 3.000 (Selic)", "callback_data": "cb:euro:3000:SELIC"},
                {"text": "€ 5.000 (Selic)", "callback_data": "cb:euro:5000:SELIC"},
            ],
            [
                {"text": "€ 1.500 (FIIs)", "callback_data": "cb:euro:1500:FIIS"},
                {"text": "€ 1.500 (IPCA+)", "callback_data": "cb:euro:1500:IPCA"},
            ],
            [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
        ]
    }


def _trap_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "BTC/BRL Compra", "callback_data": "cb:trap:BTC/BRL:BUY"},
                {"text": "SOL/BRL Compra", "callback_data": "cb:trap:SOL/BRL:BUY"},
            ],
            [
                {"text": "ETH/BRL Compra", "callback_data": "cb:trap:ETH/BRL:BUY"},
                {"text": "USDT/BRL Compra", "callback_data": "cb:trap:USDT/BRL:BUY"},
            ],
            [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
        ]
    }


def _fx_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "💵 Recebo US$ 2.500", "callback_data": "cb:fx:EARN_USD:2500"},
                {"text": "💵 Recebo US$ 5.000", "callback_data": "cb:fx:EARN_USD:5000"},
            ],
            [{"text": "🛒 Compro US$ 1.000", "callback_data": "cb:fx:BUY_USD:1000"}],
            [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
        ]
    }


def _quant_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "USDT/BRL (4h) + A/B", "callback_data": "cb:quant:USDT/BRL:4h"},
                {"text": "BTC/USDT (1h) + A/B", "callback_data": "cb:quant:BTC/USDT:1h"},
            ],
            [{"text": "🇧🇷 Boletim Brasil (/crise)", "callback_data": "cb:quant:CRISE:4h"}],
            [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
        ]
    }


def _post_action_keyboard(retry_cb: str, retry_label: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🔔 Criar Alerta (USD & EUR)", "callback_data": "cb:menu_alerts"}],
            [
                {"text": retry_label, "callback_data": retry_cb},
                {"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"},
            ],
        ]
    }


def format_telegram_report(
    snapshot: dict[str, Any],
    committee: dict[str, Any],
    backtest: dict[str, Any],
    cta_url: str | None = None,
) -> str:
    """Format a plain-text Telegram message with deterministic committee verdict and A/B proof."""
    verdict = committee["verdict"]
    icon = VERDICT_ICONS.get(verdict, "📊")
    symbol = snapshot["symbol"]
    timeframe = snapshot["timeframe"]
    curr = snapshot.get("currency_symbol", "R$" if symbol.endswith("/BRL") else "$")
    ind = snapshot["latest_indicators"]
    micro = snapshot["microstructure"]
    plan = committee["execution_plan"]
    raw_bt = backtest["mode_a_raw"]
    guard_bt = backtest["mode_b_guarded"]

    effective_cta = (cta_url or _cta_url()).strip()
    source_tag = snapshot["served_exchange"].upper() if snapshot["is_live"] else "SIMULACAO"

    lines = [
        f"{icon} SCUDEX | {symbol} ({timeframe})",
        f"Fonte: {source_tag} | Preço: {curr}{snapshot['last_price']:,.4f} ({snapshot['change_24h_pct']:+.2f}%)",
        (
            f"RSI(14): {ind['RSI_14']} | Spread: {micro['bid_ask_spread_bps']} bps | "
            f"Book Imbalance: {micro['orderbook_imbalance'] * 100:+.1f}%"
        ),
        "",
        f"Leitura Determinística: {verdict}",
        f"Regime: {committee['regime']} | Score: {committee['combined_score']:+.1f}",
        f"Resumo: {committee['executive_summary']}",
        "",
    ]

    if plan["is_executable"]:
        lines.extend(
            [
                "Referência Quantitativa (Educacional):",
                f"• Referência: {curr}{plan['entry_price']} | Stop ATR: {curr}{plan['stop_loss']}",
                f"• Alvo 1 (1.5R): {curr}{plan['take_profit_1']} | Alvo 2 (3R): {curr}{plan['take_profit_2']}",
                f"• Lote Simulado: {curr}{plan['position_size_usdt']:,.2f} (Perda máx.: {curr}{plan['max_loss_usdt']:,.2f})",
            ]
        )
    else:
        lines.append(
            f"Capital protegido (0 exposto) — {committee['agents'][-1]['reasons'][0]}"
        )

    lines.extend(
        [
            "",
            f"Backtest A/B Justo ({backtest['bars_evaluated']} candles • {backtest['strategy_name']}):",
            f"• Sem Filtro (mesmo lote ATR): MaxDD -{raw_bt['max_drawdown_pct']}% | Retorno {raw_bt['total_return_pct']:+.2f}%",
            f"• Com Filtro de Regime: MaxDD -{guard_bt['max_drawdown_pct']}% | Retorno {guard_bt['total_return_pct']:+.2f}%",
            f"• Entradas bloqueadas: {guard_bt['blocked_bad_trades']} | Drawdown reduzido: {guard_bt['drawdown_saved_pct']}%",
            "",
            f"👉 Simulador e alertas gratuitos: {effective_cta}",
        ]
    )

    return "\n".join(lines)


def send_telegram_message(
    text: str,
    bot_token: str | None = None,
    chat_id: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a message via Telegram Bot API with optional InlineKeyboardMarkup."""
    effective_token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    effective_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip()

    if not effective_token or not effective_chat:
        res: dict[str, Any] = {
            "sent": False,
            "mode": "dry_run",
            "reason": "Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env ou vincule na aba Alertas.",
            "preview": text,
        }
        if reply_markup is not None:
            res["reply_markup"] = reply_markup
        return res

    if not text.strip():
        return {
            "sent": False,
            "mode": "error",
            "reason": "Mensagem vazia para envio no Telegram.",
        }

    url = f"https://api.telegram.org/bot{effective_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": effective_chat,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            return {
                "sent": True,
                "mode": "telegram_api",
                "chat_id": effective_chat,
                "message_id": data["result"]["message_id"],
            }
        return {
            "sent": False,
            "mode": "error",
            "status_code": resp.status_code,
            "reason": data.get("description", resp.text[:200]),
        }
    except Exception as exc:
        return {
            "sent": False,
            "mode": "error",
            "reason": str(exc),
        }


def answer_callback_query(callback_query_id: str, bot_token: str, text: str = "") -> bool:
    """Acknowledge inline keyboard button clicks cleanly in Telegram."""
    if not bot_token or not callback_query_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:180]
    try:
        resp = httpx.post(url, json=payload, timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


def register_bot_commands(bot_token: str) -> bool:
    """Register slash commands in Telegram's native Menu button via setMyCommands."""
    if not bot_token:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/setMyCommands"
    commands = [
        {"command": "start", "description": "Abrir menu principal interativo"},
        {"command": "euro", "description": "Simular remessa EUR->BRL e Selic Break-Even"},
        {"command": "freio", "description": "Freio de Impulso (pausa 10 min + histórico real)"},
        {"command": "raiox", "description": "Raio-X de Vazamentos (CSV em RAM)"},
        {"command": "alerta", "description": "Criar alerta de cotação (ex: /alerta 6.55)"},
        {"command": "momentocaro", "description": "Alerta se USD/EUR ficar +1,5% acima da média"},
        {"command": "cilada", "description": "Auditar dica antes de arriscar"},
        {"command": "cambio", "description": "Comparar spread USD/BRL"},
        {"command": "id", "description": "Ver Chat ID para vincular sua conta"},
    ]
    try:
        resp = httpx.post(url, json={"commands": commands}, timeout=6.0)
        return resp.status_code == 200
    except Exception:
        return False


def run_analysis_and_format(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    exchange: str = "binance",
    strategy: str = "trend_ema_crossover",
    capital_usdt: float = 10000.0,
    risk_per_trade_pct: float = 1.0,
) -> str:
    """Fetch snapshot, run deterministic committee + A/B backtest, and return formatted Telegram text."""
    snapshot = fetch_market_snapshot(
        symbol=symbol,
        timeframe=timeframe,
        exchange_id=exchange,
    )
    committee = run_committee(
        snapshot=snapshot,
        capital_usdt=capital_usdt,
        risk_per_trade_pct=risk_per_trade_pct,
    )
    df = snapshot.pop("df")
    backtest = run_ab_backtest(
        df=df,
        strategy=strategy,
        capital_usdt=capital_usdt,
        risk_per_trade_pct=risk_per_trade_pct,
        timeframe=timeframe,
    )
    return format_telegram_report(snapshot, committee, backtest)


def _normalize_pair(raw: str) -> str:
    clean = raw.strip().upper()
    if "/" not in clean:
        if clean.endswith("USDT") and len(clean) > 4:
            return f"{clean[:-4]}/USDT"
        if clean.endswith("BRL") and len(clean) > 3:
            return f"{clean[:-3]}/BRL"
    return clean


def run_crisis_brief_and_format(
    exchange: str = "binance",
    timeframe: str = "4h",
    monthly_contribution_brl: float = 500.0,
    monthly_expenses_brl: float = 3000.0,
    has_emergency_fund: bool = False,
) -> dict[str, str]:
    """Generate Brazil brief for USDT/BRL + BTC/BRL using honest Fixed DCA vs Smart DCA metrics."""
    parts_tg: list[str] = ["🇧🇷 BOLETIM SCUDEX — LEITURA QUANTITATIVA"]
    parts_wa: list[str] = []

    for sym in ("USDT/BRL", "BTC/BRL"):
        snap = fetch_market_snapshot(symbol=sym, timeframe=timeframe, exchange_id=exchange)
        comm = run_committee(snapshot=snap, capital_usdt=10000.0, risk_per_trade_pct=1.0)
        df = snap.pop("df")
        shield = build_crisis_shield_plan(
            snapshot=snap,
            committee=comm,
            available_capital_brl=10000.0,
            monthly_contribution_brl=monthly_contribution_brl,
            monthly_expenses_brl=monthly_expenses_brl,
            has_emergency_fund=has_emergency_fund,
        )
        dca = simulate_smart_dca_brl(
            df=df,
            monthly_contribution_brl=monthly_contribution_brl,
            timeframe=timeframe,
        )
        wa_block = format_whatsapp_report(snap, comm, shield, dca)
        parts_wa.append(wa_block)

        ind = snap["latest_indicators"]
        parts_tg.append(
            f"\n📌 {sym} ({snap['served_exchange'].upper()}): R$ {snap['last_price']:,.2f} "
            f"({snap['change_24h_pct']:+.2f}% 24h | RSI {ind['RSI_14']})\n"
            f"• Faixa Hoje: {shield['window_label']}\n"
            f"• Parcela referência: R$ {shield['suggested_today_brl']:,.2f} ({shield['window_advice']})\n"
            f"• Comparativo ({dca['days_covered']}d): Smart DCA R$ {dca['avg_price_smart_dca']:,.2f} "
            f"vs DCA Fixo R$ {dca['avg_price_dca']:,.2f} (patrimônio + caixa: {dca['final_value_diff_pct']:+.2f}%)"
        )

    cta_url = _cta_url()
    parts_tg.append(
        "\n⚠️ Regra Anti-Golpe: nunca envie PIX para promessas de lucro fixo.\n"
        f"👉 Crie seus alertas e simulações grátis: {cta_url}"
    )

    return {
        "telegram_text": "\n".join(parts_tg),
        "whatsapp_text": "\n\n━━━━━━━━━━━━━━\n\n".join(parts_wa),
    }


def _check_cooldown(chat_id: str, min_sec: float = 10.0) -> bool:
    now_mono = time.monotonic()
    with _CMD_LOCK:
        last = _LAST_CMD_BY_CHAT.get(chat_id, 0.0)
        if last > 0 and (now_mono - last) < min_sec:
            return False
        _LAST_CMD_BY_CHAT[chat_id] = now_mono
    return True


def _require_paid_telegram_profile(chat_id: str, bot_token: str) -> bool:
    """Verify that chat_id is linked to a paid Supabase profile; otherwise inform user."""
    prof = get_profile_by_telegram_chat_id(chat_id)
    if not prof:
        send_telegram_message(
            f"🔒 Este comando exige conta vinculada.\n"
            f"Seu Chat ID é {chat_id}. Acesse {_cta_url()}/app#alerts para vincular sua conta.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_main_menu_keyboard(),
        )
        return False

    tier = (prof.get("plan_tier") or "TRIAL").upper()
    if tier == "TRIAL":
        send_telegram_message(
            "🔒 Este comando completo é exclusivo para assinantes dos planos pagos.\n"
            f"No plano gratuito você pode usar os menus de remessa EUR→BRL, Detector de Cilada, Spread USD e 1 alerta.\n"
            f"Planos: {_cta_url()}/#planos",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_main_menu_keyboard(),
        )
        return False
    return True


def _send_welcome_menu(chat_id: str, bot_token: str) -> None:
    cta = _cta_url()
    msg = (
        "🛡️ Scudex — Proteção Patrimonial, Câmbio & Risco\n\n"
        "Para quem investe no Brasil, EUA, Europa ou exterior — o mesmo motor determinístico e a mesma base ao vivo da Web, organizados em 3 pilares:\n"
        "• 💱 Meu Câmbio: compare USD/BRL e EUR→BRL com remessadora regulada + Break-Even da Selic BCB\n"
        "• 🛡️ Meu Escudo: audite dicas de grupos contra o livro real de ofertas e receba alertas USD/EUR\n"
        "• 📊 Meu Quant: comitê de 4 módulos e Backtest A/B Justo\n\n"
        f"🌐 Site e planos: {cta}/#planos"
    )
    send_telegram_message(msg, bot_token=bot_token, chat_id=chat_id, reply_markup=_main_menu_keyboard())


def _run_euro_simulation(chat_id: str, bot_token: str, amount_eur: float, inv_type: str) -> None:
    amt = max(50.0, float(amount_eur))
    inv = inv_type.upper() if inv_type.upper() in ("SELIC", "FIIS", "IPCA") else "SELIC"
    bridge = build_europe_brazil_bridge(amount_eur=amt, investment_type=inv)
    clean_text = bridge["whatsapp_share_text"].replace("*", "").replace("_", "")
    send_telegram_message(
        clean_text,
        bot_token=bot_token,
        chat_id=chat_id,
        reply_markup=_post_action_keyboard("cb:menu_euro", "🔁 Outro Valor (€)"),
    )


def _run_trap_audit(chat_id: str, bot_token: str, symbol: str, side_arg: str) -> None:
    norm_sym = _normalize_pair(symbol)
    snap = fetch_market_snapshot(symbol=norm_sym, timeframe="1h", exchange_id="binance")
    comm = run_committee(snapshot=snap, capital_usdt=5000.0, risk_per_trade_pct=1.0)
    snap.pop("df", None)
    trap = evaluate_trap_check(snap, comm, claimed_side=side_arg)
    clean_text = trap["whatsapp_share_text"].replace("*", "").replace("_", "")
    send_telegram_message(
        clean_text,
        bot_token=bot_token,
        chat_id=chat_id,
        reply_markup=_post_action_keyboard("cb:menu_trap", "🔍 Auditar Outro Par"),
    )


def _run_fx_comparison(chat_id: str, bot_token: str, profile: str, monthly_vol: float) -> None:
    prof_norm = "BUY_USD" if profile.upper() in ("BUY_USD", "COMPRAR", "BUY") else "EARN_USD"
    snap = fetch_market_snapshot(
        symbol="USDT/BRL",
        timeframe="4h",
        exchange_id="binance",
        limit=FX_SNAPSHOT_LIMIT,
    )
    snap.pop("df", None)
    fx = build_fx_treasury_signal(snap, monthly_usd_volume=max(50.0, float(monthly_vol)), profile=prof_norm)
    clean_text = fx["whatsapp_share_text"].replace("*", "").replace("_", "")
    send_telegram_message(
        clean_text,
        bot_token=bot_token,
        chat_id=chat_id,
        reply_markup=_post_action_keyboard("cb:menu_fx", "💱 Outro Volume US$"),
    )


def _run_impulse_brake(
    chat_id: str,
    bot_token: str,
    symbol: str,
    planned_amount: float = 1000.0,
    side_arg: str = "BUY",
) -> None:
    norm_sym = _normalize_pair(symbol)
    snap = fetch_market_snapshot(symbol=norm_sym, timeframe="1h", exchange_id="binance")
    comm = run_committee(snapshot=snap, capital_usdt=5000.0, risk_per_trade_pct=1.0)
    df = snap.pop("df", None)
    brake = evaluate_impulse_brake(
        snapshot=snap,
        committee=comm,
        df=df,
        planned_amount=planned_amount,
        claimed_side=side_arg,
        source_origin="grupo_whatsapp",
    )
    clean_text = brake["telegram_text"].replace("*", "").replace("_", "")
    send_telegram_message(
        clean_text,
        bot_token=bot_token,
        chat_id=chat_id,
        reply_markup=_post_action_keyboard("cb:menu_brake", "🛑 Frear Outro Ativo"),
    )


def _send_xray_privacy_card(chat_id: str, bot_token: str) -> None:
    cta = _cta_url()
    rows: list[list[dict[str, str]]] = [
        [{"text": "🔔 Criar Alerta Momento Caro", "callback_data": "cb:menu_alerts"}],
        [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
    ]
    url_row = _maybe_url_button("🩺 Abrir Raio-X na Web (HTTPS)", "/#studio")
    if url_row:
        rows.insert(0, url_row)
    msg = (
        "🩺 Raio-X de Vazamentos (Extrato CSV Wise, Revolut & Binance)\n\n"
        "🔒 Por privacidade real, NÃO recebo extratos aqui no chat do Telegram (mensagens de bot não têm criptografia ponta-a-ponta).\n\n"
        "Na Web (HTTPS), seu CSV é lido apenas em memória RAM e descartado imediatamente — nenhuma linha ou dado bancário fica gravado.\n\n"
        f"👉 Acesse o Simulador -> Aba '4. Raio-X de Vazamentos' (ou teste o Exemplo 1-Clique): {cta}/#studio"
    )
    send_telegram_message(msg, bot_token=bot_token, chat_id=chat_id, reply_markup={"inline_keyboard": rows})


def _create_expensive_moment_alert(chat_id: str, bot_token: str, symbol: str = "EUR/BRL") -> None:
    prof = get_profile_by_telegram_chat_id(chat_id)
    if not prof:
        _show_alerts_menu(chat_id, bot_token)
        return

    raw = str(symbol or "EUR/BRL").strip().upper()
    sym_norm = "USDT/BRL" if raw in ("USDT/BRL", "USD/BRL", "USD", "USDT", "DOLAR", "DÓLAR") else "EUR/BRL"

    if sym_norm == "EUR/BRL":
        fx = fetch_eur_brl_snapshot(timeframe="4h", allow_synthetic=False)
        if not fx.get("is_live") or float(fx.get("eur_brl_rate") or 0) <= 0:
            raise MarketDataUnavailableError("EUR/BRL indisponível no momento.")
        current_rate = float(fx["eur_brl_rate"])
        sma_20 = float(fx["sma_20"])
        rsi = float(fx["rsi_14"])
        span = float(fx["bb_upper"]) - float(fx["bb_lower"])
        bb_pos = ((current_rate - float(fx["bb_lower"])) / span) if span > 1e-9 else 0.5
    else:
        snap = fetch_market_snapshot(
            symbol="USDT/BRL",
            timeframe="4h",
            exchange_id="binance",
            limit=FX_SNAPSHOT_LIMIT,
            allow_synthetic=False,
        )
        ind = snap["latest_indicators"]
        current_rate = float(snap["last_price"])
        sma_20 = float(ind.get("BB_MID") or current_rate)
        rsi = float(ind["RSI_14"])
        span = float(ind["BB_UPPER"]) - float(ind["BB_LOWER"])
        bb_pos = ((current_rate - float(ind["BB_LOWER"])) / span) if span > 1e-9 else 0.5

    diag = evaluate_expensive_moment(sym_norm, current_rate, sma_20, rsi, bb_pos)
    target_rate = float(diag["suggested_expensive_rate"])

    try:
        created = create_user_alert(
            user_id=prof["id"],
            plan_tier=prof.get("plan_tier", "TRIAL"),
            symbol=sym_norm,
            direction="ABOVE",
            target_rate=target_rate,
            channel="telegram",
        )
        send_telegram_message(
            f"🚨 Alerta de Momento Caro #{created['id']} ativado!\n"
            f"Par: {sym_norm} (agora R$ {current_rate:,.4f} | Média 20p: R$ {sma_20:,.4f} | Desvio: {diag['deviation_pct']:+.2f}%)\n"
            f"Avisar quando subir para >= R$ {target_rate:,.4f} (+1,5% acima da média).",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_post_action_keyboard("cb:menu_alerts", "🔔 Ver Meus Alertas"),
        )
    except ValueError as exc:
        cta = _cta_url()
        send_telegram_message(
            f"⚠️ {exc}\n\nGerencie seus alertas ou veja os planos em {cta}/#planos.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_post_action_keyboard("cb:menu_alerts", "🔔 Gerenciar Alertas"),
        )
    except SupabaseFeatureUnavailableError as exc:
        send_telegram_message(f"⚠️ {exc}", bot_token=bot_token, chat_id=chat_id, reply_markup=_main_menu_keyboard())


def _show_alerts_menu(chat_id: str, bot_token: str) -> None:
    cta = _cta_url()
    prof = get_profile_by_telegram_chat_id(chat_id)
    if not prof:
        rows: list[list[dict[str, str]]] = [
            [{"text": "🔄 Já Vinculei — Verificar", "callback_data": "cb:menu_alerts"}],
            [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
        ]
        url_row = _maybe_url_button("🔗 Vincular Conta no Site", "/app#alerts")
        if url_row:
            rows.insert(1, url_row)
        send_telegram_message(
            "🔔 Alertas Proativos EUR/BRL & USDT/BRL\n\n"
            "Para ativar alertas direto aqui no Telegram, vincule seu Chat ID em 2 passos:\n"
            f"1. Copie seu Chat ID: {chat_id}\n"
            f"2. Entre no site ({cta}/app#alerts), cole esse número e clique em Salvar.\n\n"
            "Depois toque em '🔄 Já Vinculei — Verificar'.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup={"inline_keyboard": rows},
        )
        return

    tier = (prof.get("plan_tier") or "TRIAL").upper()
    max_alerts = 1 if tier == "TRIAL" else 20
    alerts = list_user_alerts(prof["id"])

    lines = [
        f"🔔 Meus Alertas Ativos ({len(alerts)}/{max_alerts} • Plano {tier})",
        "",
    ]
    rows = []
    if not alerts:
        lines.append("Você ainda não tem alertas cadastrados.")
    else:
        for a in alerts:
            if not a.get("active", True):
                continue
            dir_sym = ">=" if str(a.get("direction", "")).upper() == "ABOVE" else "<="
            rate_val = float(a.get("target_rate") or 0.0)
            lines.append(f"• #{a['id']} {a['symbol']} {dir_sym} R$ {rate_val:,.4f} ({a.get('channel', 'telegram')})")
            rows.append(
                [
                    {
                        "text": f"❌ Remover #{a['id']} ({a['symbol']} R$ {rate_val:.4f})",
                        "callback_data": f"cb:alert_del:{a['id']}",
                    }
                ]
            )

    lines.extend(
        [
            "",
            "Crie rápido com os botões abaixo ou digite:",
            "• /alerta 6.55  (avisa se EUR/BRL subir >= R$ 6,55)",
            "• /alerta abaixo 6.10",
            "• /alerta USDT/BRL abaixo 5.50",
        ]
    )

    rows.append(
        [
            {"text": "📈 EUR/BRL +1%", "callback_data": "cb:alert_quick:EUR/BRL:ABOVE:1"},
            {"text": "📉 EUR/BRL -1%", "callback_data": "cb:alert_quick:EUR/BRL:BELOW:1"},
        ]
    )
    rows.append(
        [
            {"text": "🚨 Momento Caro EUR (+1,5% Média)", "callback_data": "cb:alert_moment:EUR/BRL"},
            {"text": "🚨 Momento Caro USD (+1,5% Média)", "callback_data": "cb:alert_moment:USDT/BRL"},
        ]
    )
    rows.append(
        [
            {"text": "✏️ Meta Personalizada", "callback_data": "cb:alert_custom_help"},
            {"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"},
        ]
    )
    send_telegram_message(
        "\n".join(lines),
        bot_token=bot_token,
        chat_id=chat_id,
        reply_markup={"inline_keyboard": rows},
    )


def _create_quick_alert(
    chat_id: str,
    bot_token: str,
    symbol: str,
    direction: str,
    pct_offset: float,
) -> None:
    prof = get_profile_by_telegram_chat_id(chat_id)
    if not prof:
        _show_alerts_menu(chat_id, bot_token)
        return

    sym_norm = symbol.upper() if symbol.upper() in ("EUR/BRL", "USDT/BRL") else "EUR/BRL"
    dir_norm = "BELOW" if direction.upper() == "BELOW" else "ABOVE"

    if sym_norm == "EUR/BRL":
        fx = fetch_eur_brl_snapshot(timeframe="4h", allow_synthetic=False)
        if not fx.get("is_live") or float(fx.get("eur_brl_rate") or 0) <= 0:
            raise MarketDataUnavailableError("EUR/BRL indisponível no momento.")
        current_rate = float(fx["eur_brl_rate"])
    else:
        snap = fetch_market_snapshot(
            symbol="USDT/BRL",
            timeframe="4h",
            exchange_id="binance",
            limit=FX_SNAPSHOT_LIMIT,
            allow_synthetic=False,
        )
        current_rate = float(snap["last_price"])

    factor = (1.0 + pct_offset / 100.0) if dir_norm == "ABOVE" else (1.0 - pct_offset / 100.0)
    target_rate = round(current_rate * factor, 4)

    try:
        created = create_user_alert(
            user_id=prof["id"],
            plan_tier=prof.get("plan_tier", "TRIAL"),
            symbol=sym_norm,
            direction=dir_norm,
            target_rate=target_rate,
            channel="telegram",
        )
        dir_txt = "subir para >=" if dir_norm == "ABOVE" else "cair para <="
        send_telegram_message(
            f"✅ Alerta #{created['id']} ativado!\n"
            f"Par: {sym_norm} (agora R$ {current_rate:,.4f})\n"
            f"Avisar quando {dir_txt} R$ {target_rate:,.4f}.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_post_action_keyboard("cb:menu_alerts", "🔔 Ver Meus Alertas"),
        )
    except ValueError as exc:
        cta = _cta_url()
        rows: list[list[dict[str, str]]] = [
            [{"text": "🔔 Gerenciar Meus Alertas", "callback_data": "cb:menu_alerts"}],
            [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
        ]
        url_row = _maybe_url_button("⭐ Liberar 20 Alertas (Pro)", "/#planos")
        if url_row:
            rows.insert(1, url_row)
        send_telegram_message(
            f"⚠️ {exc}\n\nVocê pode remover o alerta atual abaixo ou liberar até 20 alertas em {cta}/#planos.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup={"inline_keyboard": rows},
        )
    except SupabaseFeatureUnavailableError as exc:
        send_telegram_message(f"⚠️ {exc}", bot_token=bot_token, chat_id=chat_id, reply_markup=_main_menu_keyboard())


def _handle_alerta_command(parts: list[str], chat_id: str, bot_token: str) -> None:
    prof = get_profile_by_telegram_chat_id(chat_id)
    if not prof:
        _show_alerts_menu(chat_id, bot_token)
        return

    if len(parts) == 1:
        _show_alerts_menu(chat_id, bot_token)
        return

    symbol = "EUR/BRL"
    direction = "ABOVE"
    target_rate: float | None = None

    for raw_tok in parts[1:]:
        tok = raw_tok.strip().lower()
        if tok in ("eur/brl", "eurbrl", "eur", "euro"):
            symbol = "EUR/BRL"
        elif tok in ("usdt/brl", "usdtbrl", "usdt", "usd", "dolar", "dólar"):
            symbol = "USDT/BRL"
        elif tok in ("acima", "above", "subir", ">="):
            direction = "ABOVE"
        elif tok in ("abaixo", "below", "cair", "<="):
            direction = "BELOW"
        else:
            try:
                target_rate = float(tok.replace(",", "."))
            except ValueError:
                pass

    if target_rate is None or target_rate <= 0:
        send_telegram_message(
            "✏️ Formato inválido para /alerta.\n\nUse um dos exemplos:\n"
            "• /alerta 6.55\n"
            "• /alerta abaixo 6.10\n"
            "• /alerta USDT/BRL abaixo 5.50",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_post_action_keyboard("cb:menu_alerts", "🔔 Ver Meus Alertas"),
        )
        return

    try:
        created = create_user_alert(
            user_id=prof["id"],
            plan_tier=prof.get("plan_tier", "TRIAL"),
            symbol=symbol,
            direction=direction,
            target_rate=target_rate,
            channel="telegram",
        )
        dir_txt = "subir para >=" if direction == "ABOVE" else "cair para <="
        send_telegram_message(
            f"✅ Alerta #{created['id']} criado com sucesso!\n"
            f"{symbol}: avisar no Telegram quando {dir_txt} R$ {target_rate:,.4f}.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_post_action_keyboard("cb:menu_alerts", "🔔 Ver Meus Alertas"),
        )
    except ValueError as exc:
        cta = _cta_url()
        send_telegram_message(
            f"⚠️ {exc}\n\nGerencie seu alerta atual ou conheça o plano Pro em {cta}/#planos.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_post_action_keyboard("cb:menu_alerts", "🔔 Gerenciar Alertas"),
        )
    except SupabaseFeatureUnavailableError as exc:
        send_telegram_message(f"⚠️ {exc}", bot_token=bot_token, chat_id=chat_id, reply_markup=_main_menu_keyboard())


def _delete_alert_cb(chat_id: str, bot_token: str, alert_id: int) -> None:
    prof = get_profile_by_telegram_chat_id(chat_id)
    if not prof:
        _show_alerts_menu(chat_id, bot_token)
        return
    try:
        ok = delete_user_alert(prof["id"], alert_id)
        if ok:
            send_telegram_message(f"🗑️ Alerta #{alert_id} removido.", bot_token=bot_token, chat_id=chat_id)
        else:
            send_telegram_message(f"⚠️ Alerta #{alert_id} não encontrado na sua conta.", bot_token=bot_token, chat_id=chat_id)
    except SupabaseFeatureUnavailableError as exc:
        send_telegram_message(f"⚠️ {exc}", bot_token=bot_token, chat_id=chat_id)
    _show_alerts_menu(chat_id, bot_token)


def _show_account_menu(chat_id: str, bot_token: str) -> None:
    cta = _cta_url()
    prof = get_profile_by_telegram_chat_id(chat_id)
    if not prof:
        rows: list[list[dict[str, str]]] = [
            [{"text": "🔔 Como Vincular Conta", "callback_data": "cb:menu_alerts"}],
            [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
        ]
        url_row = _maybe_url_button("🌐 Criar Conta Grátis", "/app#alerts")
        if url_row:
            rows.insert(1, url_row)
        send_telegram_message(
            f"🆔 Chat ID: {chat_id}\n"
            "Status: Conta ainda não vinculada.\n\n"
            f"Crie sua conta grátis em {cta} e cole esse Chat ID na aba Alertas do /app.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup={"inline_keyboard": rows},
        )
        return

    tier = (prof.get("plan_tier") or "TRIAL").upper()
    uses = int(prof.get("trial_uses_remaining", 3))
    alerts = list_user_alerts(prof["id"])
    quota_txt = "Ilimitado" if tier != "TRIAL" else f"{uses} análises restantes no Terminal"
    max_alerts = 1 if tier == "TRIAL" else 20

    rows = [
        [{"text": "🔔 Meus Alertas (USD & EUR)", "callback_data": "cb:menu_alerts"}],
        [{"text": "◀️ Menu Principal", "callback_data": "cb:menu_main"}],
    ]
    url_row = _maybe_url_button("⭐ Ver Planos Pro (€ 14 / R$ 79)", "/#planos")
    if url_row:
        rows.insert(1, url_row)

    send_telegram_message(
        f"🆔 Conta Vinculada\n"
        f"• E-mail: {prof.get('email', '--')}\n"
        f"• Plano: {tier}\n"
        f"• Terminal (/app): {quota_txt}\n"
        f"• Alertas ativos: {len(alerts)}/{max_alerts}\n\n"
        f"Gerenciar planos e histórico: {cta}/#planos",
        bot_token=bot_token,
        chat_id=chat_id,
        reply_markup={"inline_keyboard": rows},
    )


def _handle_command(text: str, chat_id: str, bot_token: str) -> None:
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].split("@")[0].lower()

    if cmd in ("/start", "/help", "/menu"):
        _send_welcome_menu(chat_id=chat_id, bot_token=bot_token)
        return

    if cmd == "/id":
        send_telegram_message(
            f"🆔 Chat ID desta conversa: {chat_id}\n"
            f"Cole esse número em {_cta_url()}/app#alerts para ativar seus alertas EUR/BRL.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_main_menu_keyboard(),
        )
        return

    if cmd == "/alerta":
        _handle_alerta_command(parts, chat_id=chat_id, bot_token=bot_token)
        return

    if cmd == "/raiox":
        _send_xray_privacy_card(chat_id=chat_id, bot_token=bot_token)
        return

    if not _check_cooldown(chat_id, min_sec=10.0):
        send_telegram_message("⏳ Aguarde alguns segundos antes de enviar outro comando.", bot_token, chat_id)
        return

    try:
        if cmd == "/momentocaro":
            sym_arg = parts[1] if len(parts) > 1 else "EUR/BRL"
            _create_expensive_moment_alert(chat_id=chat_id, bot_token=bot_token, symbol=sym_arg)
            return

        if cmd == "/freio":
            symbol = _normalize_pair(parts[1]) if len(parts) > 1 else "BTC/BRL"
            planned_amt = 1000.0
            side_arg = "BUY"
            for tok in parts[2:]:
                t_up = tok.strip().upper()
                if t_up in ("BUY", "SELL", "COMPRA", "COMPRAR", "VENDA", "VENDER"):
                    side_arg = t_up
                else:
                    try:
                        planned_amt = float(tok.replace(",", "."))
                    except ValueError:
                        pass
            _run_impulse_brake(
                chat_id=chat_id,
                bot_token=bot_token,
                symbol=symbol,
                planned_amount=planned_amt,
                side_arg=side_arg,
            )
            return

        if cmd == "/euro":
            try:
                eur_amt = float(parts[1].replace(",", ".")) if len(parts) > 1 else 1500.0
            except ValueError:
                eur_amt = 1500.0
            inv_type = parts[2].upper() if len(parts) > 2 else "SELIC"
            _run_euro_simulation(chat_id=chat_id, bot_token=bot_token, amount_eur=eur_amt, inv_type=inv_type)
            return

        if cmd == "/cilada":
            symbol = _normalize_pair(parts[1]) if len(parts) > 1 else "BTC/BRL"
            side_arg = parts[2].upper() if len(parts) > 2 else "BUY"
            _run_trap_audit(chat_id=chat_id, bot_token=bot_token, symbol=symbol, side_arg=side_arg)
            return

        if cmd == "/cambio":
            mode_word = parts[1].lower() if len(parts) > 1 else "receber"
            profile = "BUY_USD" if mode_word in ("comprar", "buy", "pagar") else "EARN_USD"
            try:
                vol = float(parts[2].replace(",", ".")) if len(parts) > 2 else 2500.0
            except ValueError:
                vol = 2500.0
            _run_fx_comparison(chat_id=chat_id, bot_token=bot_token, profile=profile, monthly_vol=vol)
            return

        if cmd in ("/dolar", "/crise", "/btc", "/eth", "/sol", "/analisar"):
            if not _require_paid_telegram_profile(chat_id, bot_token):
                return

        if cmd == "/dolar":
            timeframe = parts[1].lower() if len(parts) > 1 and parts[1].lower() in TIMEFRAME_SECONDS else "4h"
            send_telegram_message(f"⏳ Auditando USDT/BRL ({timeframe})...", bot_token, chat_id)
            report = run_analysis_and_format(symbol="USDT/BRL", timeframe=timeframe)
            send_telegram_message(report, bot_token, chat_id, reply_markup=_main_menu_keyboard())
            return

        if cmd == "/crise":
            timeframe = parts[1].lower() if len(parts) > 1 and parts[1].lower() in TIMEFRAME_SECONDS else "4h"
            send_telegram_message("⏳ Gerando Boletim Scudex...", bot_token, chat_id)
            brief = run_crisis_brief_and_format(timeframe=timeframe)
            send_telegram_message(brief["telegram_text"], bot_token, chat_id, reply_markup=_main_menu_keyboard())
            return

        quick_map = {"/btc": "BTC/USDT", "/eth": "ETH/USDT", "/sol": "SOL/USDT"}
        if cmd in quick_map:
            symbol = quick_map[cmd]
            timeframe = parts[1].lower() if len(parts) > 1 and parts[1].lower() in TIMEFRAME_SECONDS else "1h"
            send_telegram_message(f"⏳ Auditando {symbol} ({timeframe})...", bot_token, chat_id)
            report = run_analysis_and_format(symbol=symbol, timeframe=timeframe)
            send_telegram_message(report, bot_token, chat_id, reply_markup=_main_menu_keyboard())
            return

        if cmd == "/analisar":
            symbol = _normalize_pair(parts[1]) if len(parts) > 1 else "BTC/USDT"
            timeframe = parts[2].lower() if len(parts) > 2 and parts[2].lower() in TIMEFRAME_SECONDS else "1h"
            send_telegram_message(f"⏳ Auditando {symbol} ({timeframe})...", bot_token, chat_id)
            report = run_analysis_and_format(symbol=symbol, timeframe=timeframe)
            send_telegram_message(report, bot_token, chat_id, reply_markup=_main_menu_keyboard())
            return

        _send_welcome_menu(chat_id=chat_id, bot_token=bot_token)
    except MarketDataUnavailableError:
        send_telegram_message(
            "⚠️ Cotações ao vivo temporariamente indisponíveis nas corretoras. Tente novamente em instantes.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_main_menu_keyboard(),
        )
    except Exception as exc:
        send_telegram_message(
            f"⚠️ Não foi possível processar o comando agora ({type(exc).__name__}).",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_main_menu_keyboard(),
        )


def _handle_callback_query(cb: dict[str, Any], bot_token: str) -> None:
    cb_id = str(cb.get("id") or "")
    data = str(cb.get("data") or "").strip()
    msg = cb.get("message") or {}
    chat = msg.get("chat") or cb.get("from") or {}
    chat_id = str(chat.get("id") or "")

    answer_callback_query(cb_id, bot_token)
    if not chat_id or not data:
        return

    try:
        if data == "cb:menu_main":
            _send_welcome_menu(chat_id, bot_token)
        elif data == "cb:menu_euro":
            send_telegram_message(
                "🇪🇺➡️🇧🇷 Simulador Ponte Europa → Brasil\nEscolha o valor e a referência no Brasil:",
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=_euro_menu_keyboard(),
            )
        elif data == "cb:menu_alerts":
            _show_alerts_menu(chat_id, bot_token)
        elif data == "cb:menu_trap":
            send_telegram_message(
                "🛡️ Detector de Cilada\nEscolha um par abaixo ou envie /cilada PAR compra:",
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=_trap_menu_keyboard(),
            )
        elif data == "cb:menu_brake":
            send_telegram_message(
                "🛑 Freio de Impulso (Pausa 10 min + Histórico Real)\n"
                "Antes de entrar no impulso, escolha um par abaixo ou envie /freio BTC/BRL 1000:",
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=_brake_menu_keyboard(),
            )
        elif data == "cb:menu_xray":
            _send_xray_privacy_card(chat_id, bot_token)
        elif data == "cb:menu_fx":
            send_telegram_message(
                "💱 Comparador de Spread Dólar (USDT/BRL)\nEscolha um perfil:",
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=_fx_menu_keyboard(),
            )
        elif data == "cb:menu_quant":
            send_telegram_message(
                "📊 Análise Quant & Backtest A/B Justo (Planos Pagos)\n\n"
                "Comitê determinístico de 4 módulos (Tendência, Microestrutura, Volatilidade, Disjuntor ATR) "
                "+ comparativo A/B justo.\nEscolha uma consulta ou use /analisar PAR 4h:",
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=_quant_menu_keyboard(),
            )
        elif data == "cb:menu_account":
            _show_account_menu(chat_id, bot_token)
        elif data == "cb:menu_support":
            cta = _cta_url()
            send_telegram_message(
                "❓ Metodologia, Fontes & Suporte — Scudex\n\n"
                "• Motor: Scudex Quant v2 (Motor Determinístico — zero custódia, sem caixa preta).\n"
                "• Cotações USD/BRL & EUR/BRL: Binance cripto-spot (USDT/BRL e EUR/USDT × USDT/BRL) contínuo conforme disponibilidade (pode divergir do PTAX/bancário).\n"
                "• Selic & B3: API oficial BCB (Série 432) e Yahoo Finance (horário de pregão).\n\n"
                f"📄 Termos: {cta}/termos\n"
                f"🔒 Privacidade: {cta}/privacidade\n"
                f"⭐ Planos e gestão de conta: {cta}/#planos",
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=_main_menu_keyboard(),
            )
        elif data == "cb:alert_custom_help":
            send_telegram_message(
                "✏️ Criar Alerta Personalizado\n\nEnvie uma mensagem com a sua meta exata:\n"
                "• /alerta 6.55  (EUR/BRL subir para >= R$ 6,55)\n"
                "• /alerta abaixo 6.10  (EUR/BRL cair para <= R$ 6,10)\n"
                "• /alerta USDT/BRL abaixo 5.50",
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=_post_action_keyboard("cb:menu_alerts", "🔔 Ver Meus Alertas"),
            )
        elif data.startswith("cb:euro:"):
            if not _check_cooldown(chat_id, min_sec=5.0):
                send_telegram_message("⏳ Aguarde alguns segundos antes da próxima consulta.", bot_token, chat_id)
                return
            _, _, amt_str, inv_type = data.split(":", 3)
            _run_euro_simulation(chat_id, bot_token, amount_eur=float(amt_str), inv_type=inv_type)
        elif data.startswith("cb:trap:"):
            if not _check_cooldown(chat_id, min_sec=5.0):
                send_telegram_message("⏳ Aguarde alguns segundos antes da próxima auditoria.", bot_token, chat_id)
                return
            _, _, symbol, side = data.split(":", 3)
            _run_trap_audit(chat_id, bot_token, symbol=symbol, side_arg=side)
        elif data.startswith("cb:brake:"):
            if not _check_cooldown(chat_id, min_sec=5.0):
                send_telegram_message("⏳ Aguarde alguns segundos antes de ativar outro freio.", bot_token, chat_id)
                return
            _, _, symbol, amt_str = data.split(":", 3)
            _run_impulse_brake(chat_id, bot_token, symbol=symbol, planned_amount=float(amt_str))
        elif data.startswith("cb:alert_moment:"):
            if not _check_cooldown(chat_id, min_sec=5.0):
                send_telegram_message("⏳ Aguarde alguns segundos antes de criar outro alerta.", bot_token, chat_id)
                return
            _, _, symbol = data.split(":", 2)
            _create_expensive_moment_alert(chat_id, bot_token, symbol=symbol)
        elif data.startswith("cb:fx:"):
            if not _check_cooldown(chat_id, min_sec=5.0):
                send_telegram_message("⏳ Aguarde alguns segundos antes do próximo cálculo.", bot_token, chat_id)
                return
            _, _, profile, vol_str = data.split(":", 3)
            _run_fx_comparison(chat_id, bot_token, profile=profile, monthly_vol=float(vol_str))
        elif data.startswith("cb:alert_quick:"):
            if not _check_cooldown(chat_id, min_sec=5.0):
                send_telegram_message("⏳ Aguarde alguns segundos antes de criar outro alerta.", bot_token, chat_id)
                return
            _, _, symbol, direction, pct_str = data.split(":", 4)
            _create_quick_alert(chat_id, bot_token, symbol=symbol, direction=direction, pct_offset=float(pct_str))
        elif data.startswith("cb:alert_del:"):
            alert_id = int(data.split(":", 2)[2])
            _delete_alert_cb(chat_id, bot_token, alert_id=alert_id)
        elif data.startswith("cb:quant:"):
            if not _require_paid_telegram_profile(chat_id, bot_token):
                return
            if not _check_cooldown(chat_id, min_sec=5.0):
                send_telegram_message("⏳ Aguarde alguns segundos antes da próxima análise quant.", bot_token, chat_id)
                return
            _, _, target, tf = data.split(":", 3)
            if target == "CRISE":
                brief = run_crisis_brief_and_format(timeframe=tf)
                send_telegram_message(
                    brief["telegram_text"],
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=_post_action_keyboard("cb:menu_quant", "📊 Outra Análise Quant"),
                )
            else:
                report = run_analysis_and_format(symbol=target, timeframe=tf)
                send_telegram_message(
                    report,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    reply_markup=_post_action_keyboard("cb:menu_quant", "📊 Outra Análise Quant"),
                )
    except MarketDataUnavailableError:
        send_telegram_message(
            "⚠️ Cotações ao vivo temporariamente indisponíveis nas corretoras. Tente novamente em instantes.",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_main_menu_keyboard(),
        )
    except Exception as exc:
        send_telegram_message(
            f"⚠️ Não foi possível processar a ação ({type(exc).__name__}).",
            bot_token=bot_token,
            chat_id=chat_id,
            reply_markup=_main_menu_keyboard(),
        )


def handle_update(update: dict[str, Any], bot_token: str) -> None:
    """Process a single Telegram update (inline callback_query, slash command, or free text)."""
    cb = update.get("callback_query")
    if cb:
        _handle_callback_query(cb, bot_token=bot_token)
        return

    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return

    text = str(msg.get("text") or "").strip()
    chat_id = str(msg["chat"]["id"])
    if not text:
        return

    if text.startswith("/"):
        _handle_command(text, chat_id=chat_id, bot_token=bot_token)
    else:
        _send_welcome_menu(chat_id=chat_id, bot_token=bot_token)


def _cooldown_elapsed(last_triggered_at: str | None, cooldown_sec: float = 14400.0) -> bool:
    if not last_triggered_at:
        return True
    try:
        dt = datetime.fromisoformat(str(last_triggered_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
        return elapsed >= cooldown_sec
    except Exception:
        return True


def check_and_dispatch_price_alerts() -> dict[str, Any]:
    """Check active EUR/BRL and USDT/BRL alerts against live market rates and notify Telegram users."""
    alerts = list_active_alerts_all()
    if not alerts:
        return {"checked": 0, "triggered": 0, "errors": 0}

    needed_symbols = {str(a.get("symbol", "")).upper() for a in alerts}
    live_rates: dict[str, dict[str, Any]] = {}
    errors = 0

    if "EUR/BRL" in needed_symbols:
        try:
            eur_fx = fetch_eur_brl_snapshot(timeframe="4h", allow_synthetic=False)
            if eur_fx.get("is_live"):
                eur_rate = float(eur_fx["eur_brl_rate"])
                eur_sma = float(eur_fx["sma_20"])
                eur_rsi = float(eur_fx["rsi_14"])
                eur_span = float(eur_fx["bb_upper"]) - float(eur_fx["bb_lower"])
                eur_pos = ((eur_rate - float(eur_fx["bb_lower"])) / eur_span) if eur_span > 1e-9 else 0.5
                eur_moment = evaluate_expensive_moment("EUR/BRL", eur_rate, eur_sma, eur_rsi, eur_pos)
                live_rates["EUR/BRL"] = {
                    "rate": eur_rate,
                    "rsi": eur_rsi,
                    "sma_20": eur_sma,
                    "deviation_pct": eur_moment["deviation_pct"],
                    "moment_label": eur_moment["label"],
                    "source": eur_fx["source"],
                }
                record_audit_snapshot(
                    symbol="EUR/BRL",
                    rate=eur_fx["eur_brl_rate"],
                    rsi_14=eur_fx["rsi_14"],
                    signal_label="Monitoramento Agendado",
                    source=eur_fx["source"],
                    is_live=True,
                    observed_at=eur_fx.get("observed_at"),
                )
        except Exception as exc:
            print(f"Alerta EUR/BRL ignorado (dados ao vivo indisponiveis): {exc}")
            errors += 1

    if "USDT/BRL" in needed_symbols:
        try:
            usd_snap = fetch_market_snapshot(
                symbol="USDT/BRL",
                timeframe="4h",
                exchange_id="binance",
                limit=FX_SNAPSHOT_LIMIT,
                allow_synthetic=False,
            )
            if usd_snap.get("is_live"):
                usd_ind = usd_snap["latest_indicators"]
                usd_rate = float(usd_snap["last_price"])
                usd_sma = float(usd_ind.get("BB_MID") or usd_rate)
                usd_rsi = float(usd_ind["RSI_14"])
                usd_span = float(usd_ind["BB_UPPER"]) - float(usd_ind["BB_LOWER"])
                usd_pos = ((usd_rate - float(usd_ind["BB_LOWER"])) / usd_span) if usd_span > 1e-9 else 0.5
                usd_moment = evaluate_expensive_moment("USDT/BRL", usd_rate, usd_sma, usd_rsi, usd_pos)
                live_rates["USDT/BRL"] = {
                    "rate": usd_rate,
                    "rsi": usd_rsi,
                    "sma_20": usd_sma,
                    "deviation_pct": usd_moment["deviation_pct"],
                    "moment_label": usd_moment["label"],
                    "source": usd_snap["served_exchange"],
                }
        except Exception as exc:
            print(f"Alerta USDT/BRL ignorado (dados ao vivo indisponiveis): {exc}")
            errors += 1

    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    triggered = 0

    for alert in alerts:
        sym = str(alert.get("symbol", "")).upper()
        info = live_rates.get(sym)
        if not info:
            continue

        if not _cooldown_elapsed(alert.get("last_triggered_at"), cooldown_sec=14400.0):
            continue

        current_rate = info["rate"]
        target_rate = float(alert.get("target_rate") or 0.0)
        direction = str(alert.get("direction") or "ABOVE").upper()

        hit = (direction == "ABOVE" and current_rate >= target_rate) or (
            direction == "BELOW" and current_rate <= target_rate
        )
        if not hit:
            continue

        channel = str(alert.get("channel") or "telegram").lower()
        chat_id = (alert.get("telegram_chat_id") or "").strip()
        if channel == "telegram" and chat_id:
            dir_txt = "atingiu ou subiu acima de" if direction == "ABOVE" else "caiu para ou abaixo de"
            dev_pct = info.get("deviation_pct")
            moment_lbl = info.get("moment_label") or ""
            moment_line = (
                f"Desvio vs. Média 20p: {dev_pct:+.2f}% ({moment_lbl})\n"
                if dev_pct is not None
                else ""
            )
            msg = (
                f"🔔 ALERTA SCUDEX | {sym}\n"
                f"Cotação ao vivo ({info['source']}): R$ {current_rate:,.4f}\n"
                f"Sua meta: {dir_txt} R$ {target_rate:,.4f}\n"
                f"RSI(14) 4h: {info['rsi']:.1f}\n"
                f"{moment_line}\n"
                "Aviso informativo automático — sem execução ou recomendação."
            )
            send_res = send_telegram_message(
                msg,
                bot_token=bot_token,
                chat_id=chat_id,
                reply_markup=_main_menu_keyboard(),
            )
            if send_res.get("sent"):
                mark_alert_triggered(int(alert["id"]))
                triggered += 1
        elif channel == "telegram" and not bot_token:
            triggered += 1

    return {"checked": len(alerts), "triggered": triggered, "errors": errors}


def listen_loop(bot_token: str, alert_check_interval_sec: float = 300.0) -> None:
    """Long-poll Telegram updates (messages + inline callbacks) and run periodic alert checks."""
    if not bot_token:
        print("ERRO: Defina TELEGRAM_BOT_TOKEN no .env para iniciar o modo interativo (--listen).")
        sys.exit(1)

    register_bot_commands(bot_token)
    print("Scudex Telegram Bot ativo (menus interativos + monitor de alertas a cada 5 min)...")
    offset = 0
    last_alert_check = 0.0
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"

    while True:
        try:
            now_mono = time.monotonic()
            if (now_mono - last_alert_check) >= alert_check_interval_sec:
                last_alert_check = now_mono
                try:
                    summary = check_and_dispatch_price_alerts()
                    if summary["checked"] > 0:
                        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Alertas checados: {summary}")
                except Exception as exc:
                    print(f"Aviso na checagem automatica de alertas: {exc}")

            resp = httpx.get(
                url,
                params={
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": '["message","callback_query","channel_post"]',
                },
                timeout=32.0,
            )
            if resp.status_code != 200:
                print(f"Aviso getUpdates HTTP {resp.status_code}: {resp.text[:150]}")
                time.sleep(3)
                continue

            data = resp.json()
            for update in data.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                handle_update(update, bot_token=bot_token)
        except KeyboardInterrupt:
            print("\nBot encerrado pelo usuario.")
            break
        except Exception as exc:
            print(f"Erro no polling Telegram: {exc}")
            time.sleep(3)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Scudex — Bot Telegram & Alertas EUR/BRL")
    parser.add_argument("--symbol", default="BTC/USDT", help="Par (ex: BTC/USDT)")
    parser.add_argument("--timeframe", default="1h", help="Timeframe (15m, 1h, 4h, 1d)")
    parser.add_argument("--exchange", default="binance", help="Corretora CCXT")
    parser.add_argument("--strategy", default="trend_ema_crossover", help="Estratégia do backtest A/B")
    parser.add_argument("--broadcast", action="store_true", help="Gera e envia um alerta único")
    parser.add_argument("--crisis-brief", action="store_true", help="Gera boletim Brasil (USDT/BRL + BTC/BRL)")
    parser.add_argument("--check-alerts", action="store_true", help="Verifica alertas EUR/BRL e USDT/BRL e dispara notificações")
    parser.add_argument("--loop", action="store_true", help="Publica em intervalo contínuo no canal")
    parser.add_argument("--interval", type=int, default=240, help="Minutos entre envios no modo --loop")
    parser.add_argument("--listen", action="store_true", help="Inicia bot interativo + monitor de alertas")
    args = parser.parse_args()

    if args.listen:
        listen_loop(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
        return

    if args.check_alerts:
        summary = check_and_dispatch_price_alerts()
        print(f"Check alerts summary: {summary}")
        return

    if args.crisis_brief:
        tf = args.timeframe if args.timeframe != "1h" else "4h"
        brief = run_crisis_brief_and_format(exchange=args.exchange, timeframe=tf)
        res = send_telegram_message(brief["telegram_text"])
        print("=" * 72)
        print("SCUDEX — BOLETIM TELEGRAM")
        print("=" * 72)
        print(brief["telegram_text"])
        print("-" * 72)
        print("SCUDEX — BOLETIM WHATSAPP")
        print("-" * 72)
        print(brief["whatsapp_text"])
        print("-" * 72)
        print(f"Status envio Telegram: {res}")
        print("=" * 72)
        return

    if args.loop:
        print(f"Vitrine automatica iniciada: postando {args.symbol} ({args.timeframe}) a cada {args.interval} min.")
        while True:
            try:
                report = run_analysis_and_format(
                    symbol=args.symbol,
                    timeframe=args.timeframe,
                    exchange=args.exchange,
                    strategy=args.strategy,
                )
                res = send_telegram_message(report)
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Status envio:", res.get("mode"), res.get("reason", "OK"))
            except MarketDataUnavailableError as exc:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Sem dados ao vivo: {exc}")
            try:
                time.sleep(max(1, args.interval) * 60)
            except KeyboardInterrupt:
                print("\nLoop encerrado.")
                break
        return

    report = run_analysis_and_format(
        symbol=args.symbol,
        timeframe=args.timeframe,
        exchange=args.exchange,
        strategy=args.strategy,
    )
    res = send_telegram_message(report)
    print("=" * 72)
    print("SCUDEX — TELEGRAM REPORT")
    print("=" * 72)
    print(report)
    print("-" * 72)
    print(f"Status envio: {res}")
    print("=" * 72)


if __name__ == "__main__":
    main()
