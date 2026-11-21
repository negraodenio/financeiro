"""Financial protection shield, honest Smart DCA comparison (vs Fixed DCA), trap audit & FX spread calculator."""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

from europe_bridge import evaluate_expensive_moment
from market_data import TIMEFRAME_SECONDS


def _fmt_currency(value: float, symbol: str = "R$", decimals: int = 2) -> str:
    formatted = f"{value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{symbol} {formatted}"


def build_crisis_shield_plan(
    snapshot: dict[str, Any],
    committee: dict[str, Any],
    available_capital_brl: float = 10000.0,
    monthly_contribution_brl: float = 500.0,
    monthly_expenses_brl: float = 3000.0,
    has_emergency_fund: bool = False,
) -> dict[str, Any]:
    """Build a realistic family protection allocator and timing thermometer in BRL."""
    ind = snapshot["latest_indicators"]
    close = float(ind["close"])
    rsi = float(ind["RSI_14"])
    bb_upper = float(ind["BB_UPPER"])
    bb_lower = float(ind["BB_LOWER"])
    regime = committee["regime"]

    runway_months = round(available_capital_brl / max(monthly_expenses_brl, 1.0), 1)

    if (not has_emergency_fund) or runway_months < 3.0:
        alert_level = "ALERTA_VERMELHO_SOBREVIVENCIA"
        alert_label = "Alerta Vermelho — Prioridade: Caixa & Sobrevivência"
        leverage_allowed = False
        tactical_allowed = False
        buckets = [
            {
                "name": "Caixa de Liquidez Diária (CDB 100% CDI / Tesouro Selic / Pix)",
                "pct": 70,
                "amount_brl": round(available_capital_brl * 0.70, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.70, 2),
                "purpose": "Segurar contas essenciais e impedir dívida no cartão/cheque especial.",
            },
            {
                "name": "Reserva em Moeda Forte (Dólar/Euro sem alavancagem)",
                "pct": 25,
                "amount_brl": round(available_capital_brl * 0.25, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.25, 2),
                "purpose": "Proteger parte da reserva contra perda de poder de compra do Real.",
            },
            {
                "name": "Acumulação Educacional Fracionada",
                "pct": 5,
                "amount_brl": round(available_capital_brl * 0.05, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.05, 2),
                "purpose": "Exposição mínima de longo prazo sem risco de comprometer a casa.",
            },
        ]
    elif runway_months < 6.0:
        alert_level = "ALERTA_AMARELO_TRANSICAO"
        alert_label = "Alerta Amarelo — Construção de Blindagem"
        leverage_allowed = False
        tactical_allowed = False
        buckets = [
            {
                "name": "Reserva de Emergência em Reais (Liquidez Imediata)",
                "pct": 40,
                "amount_brl": round(available_capital_brl * 0.40, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.40, 2),
                "purpose": "Completar 6 meses de tranquilidade familiar antes de correr risco.",
            },
            {
                "name": "Dolarização Gradual (USDT/BRL)",
                "pct": 40,
                "amount_brl": round(available_capital_brl * 0.40, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.40, 2),
                "purpose": "Diversificação cambial gradual sem alavancagem.",
            },
            {
                "name": "Acumulação Fracionada (BTC/BRL)",
                "pct": 20,
                "amount_brl": round(available_capital_brl * 0.20, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.20, 2),
                "purpose": "Aportes programados com controle de volatilidade.",
            },
        ]
    else:
        alert_level = "ESCUDO_VERDE_EXPANSAO"
        alert_label = "Escudo Verde — Reserva Coberta & Alocação Controlada"
        leverage_allowed = False
        tactical_allowed = True
        buckets = [
            {
                "name": "Caixa de Oportunidade (Reais com Liquidez)",
                "pct": 20,
                "amount_brl": round(available_capital_brl * 0.20, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.20, 2),
                "purpose": "Liquidez imediata para rebalanceamento sem mexer na reserva.",
            },
            {
                "name": "Reserva em Moeda Forte (USDT/BRL ou Euro)",
                "pct": 45,
                "amount_brl": round(available_capital_brl * 0.45, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.45, 2),
                "purpose": "Base patrimonial diversificada em moeda forte.",
            },
            {
                "name": "Acumulação Programada (Smart DCA)",
                "pct": 25,
                "amount_brl": round(available_capital_brl * 0.25, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.25, 2),
                "purpose": "Aportes escalonados conforme volatilidade histórica.",
            },
            {
                "name": "Parcela Tática com Limite de Risco (máx. 1%)",
                "pct": 10,
                "amount_brl": round(available_capital_brl * 0.10, 2),
                "monthly_brl": round(monthly_contribution_brl * 0.10, 2),
                "purpose": "Parcela isolada para estratégias educacionais auditadas.",
            },
        ]

    bb_span = bb_upper - bb_lower
    bb_pos = ((close - bb_lower) / bb_span) if bb_span > 1e-9 else 0.5
    weekly_base = monthly_contribution_brl / 4.0

    if rsi >= 64.0 or bb_pos >= 0.85 or regime == "EXTREME_SHOCK":
        window_status = "ESTICADO_SEGURE_IMPULSO"
        window_label = "Acima da Faixa — Reduza Ritmo (Anti-Impulso)"
        window_multiplier = 0.4
        window_advice = (
            "Leitura anti-impulso: cotação esticada no curto prazo. Na simulação educativa, "
            "reduz o aporte para 0.4x e mantém o restante em caixa."
        )
    elif rsi <= 42.0 or bb_pos <= 0.25:
        window_status = "JANELA_DESCONTO"
        window_label = "Abaixo da Média Recente"
        window_multiplier = 1.5
        window_advice = (
            "Cotação abaixo da faixa média recente — no modelo de aporte escalonado, "
            "usa parcela de 1.5x (sem usar dinheiro das contas essenciais)."
        )
    else:
        window_status = "ZONA_NEUTRA_FRACIONAR"
        window_label = "Zona Equilibrada — Ritmo Padrão"
        window_multiplier = 1.0
        window_advice = (
            "Preço dentro da faixa média recente — mantém ritmo padrão (1.0x) sem pressa."
        )

    suggested_today_brl = round(weekly_base * window_multiplier, 2)
    anti_scam_warning = (
        "Regra de Ouro Anti-Golpe: nunca envie PIX para terceiros prometendo lucro fixo, "
        "nunca use dinheiro de aluguel/alimentação e evite alavancagem."
    )

    if alert_level == "ALERTA_VERMELHO_SOBREVIVENCIA":
        crisis_summary = (
            f"Com reserva estimada para {runway_months:.1f} meses de custo fixo "
            f"({_fmt_currency(monthly_expenses_brl)}/mês), qualquer risco alavancado está VETADO. "
            f"Prioridade total para liquidez imediata (70%); referência fracionada de {_fmt_currency(suggested_today_brl)} ({window_label})."
        )
    elif alert_level == "ALERTA_AMARELO_TRANSICAO":
        crisis_summary = (
            f"Reserva intermediária ({runway_months:.1f} meses de cobertura). "
            f"Foco em completar reserva de emergência antes de ampliar volatilidade; referência hoje: "
            f"{_fmt_currency(suggested_today_brl)} ({window_multiplier:.1f}x da parcela semanal)."
        )
    else:
        crisis_summary = (
            f"Reserva sólida ({runway_months:.1f} meses de cobertura). "
            f"Referência educativa para a janela atual ({window_label}): "
            f"{_fmt_currency(suggested_today_brl)}, mantendo 90% em proteção patrimonial."
        )

    return {
        "alert_level": alert_level,
        "alert_label": alert_label,
        "runway_months": runway_months,
        "has_emergency_fund": has_emergency_fund,
        "leverage_allowed": leverage_allowed,
        "tactical_allowed": tactical_allowed,
        "available_capital_brl": round(available_capital_brl, 2),
        "monthly_contribution_brl": round(monthly_contribution_brl, 2),
        "monthly_expenses_brl": round(monthly_expenses_brl, 2),
        "buckets": buckets,
        "window_status": window_status,
        "window_label": window_label,
        "window_multiplier": window_multiplier,
        "window_advice": window_advice,
        "weekly_base_brl": round(weekly_base, 2),
        "suggested_today_brl": suggested_today_brl,
        "bb_position_pct": round(max(0.0, min(1.0, bb_pos)) * 100.0, 1),
        "anti_scam_warning": anti_scam_warning,
        "crisis_summary": crisis_summary,
    }


def simulate_smart_dca_brl(
    df: pd.DataFrame,
    monthly_contribution_brl: float = 500.0,
    timeframe: str = "1h",
) -> dict[str, Any]:
    """Compare Fixed DCA vs Smart DCA (Escudo) including uninvested cash reserve in total return."""
    n = len(df)
    warmup = min(30, max(5, n // 8))
    usable = max(1, n - warmup)
    checkpoints_target = min(20, usable)
    step = max(1, usable // checkpoints_target)
    indices = list(range(warmup, n, step))
    if not indices:
        indices = [n - 1]

    slice_brl = monthly_contribution_brl / 4.0
    close_arr = df["close"].to_numpy(dtype=float)
    rsi_arr = df["RSI_14"].to_numpy(dtype=float)
    last_price = float(close_arr[-1])

    # 1. Fixed DCA (100% of each period slice deployed immediately)
    brl_dca = 0.0
    units_dca = 0.0

    # 2. Smart DCA (adjusts deployment within [0.4x, 1.8x] and keeps remaining budget in cash reserve)
    cash_reserve = 0.0
    brl_smart_deployed = 0.0
    units_smart = 0.0
    budget_total = 0.0

    for idx in indices:
        px = max(float(close_arr[idx]), 1e-8)
        rsi = float(rsi_arr[idx])
        budget_total += slice_brl

        # Fixed DCA
        brl_dca += slice_brl
        units_dca += slice_brl / px

        # Smart DCA
        cash_reserve += slice_brl
        if rsi >= 64.0:
            deploy = min(cash_reserve, slice_brl * 0.4)
        elif rsi <= 42.0:
            deploy = min(cash_reserve, slice_brl * 1.8)
        else:
            deploy = min(cash_reserve, slice_brl * 1.0)
        cash_reserve -= deploy
        brl_smart_deployed += deploy
        units_smart += deploy / px

    avg_dca = (brl_dca / units_dca) if units_dca > 0 else last_price
    avg_smart = (brl_smart_deployed / units_smart) if units_smart > 0 else avg_dca

    vs_fixed_dca_price_pct = ((avg_dca - avg_smart) / avg_dca * 100.0) if avg_dca > 0 else 0.0
    final_value_dca_brl = units_dca * last_price
    final_value_smart_brl = units_smart * last_price + cash_reserve
    final_value_diff_brl = final_value_smart_brl - final_value_dca_brl
    final_value_diff_pct = (
        (final_value_diff_brl / final_value_dca_brl * 100.0) if final_value_dca_brl > 0 else 0.0
    )

    step_sec = TIMEFRAME_SECONDS.get(timeframe, 3600)
    days_covered = round((usable * step_sec) / 86400.0, 1)

    return {
        "checkpoints": len(indices),
        "days_covered": days_covered,
        "slice_brl": round(slice_brl, 2),
        "total_budget_brl": round(budget_total, 2),
        "total_invested_brl": round(brl_smart_deployed, 2),
        "cash_reserve_left_brl": round(cash_reserve, 2),
        "avg_price_dca": round(avg_dca, 4),
        "avg_price_smart_dca": round(avg_smart, 4),
        "vs_fixed_dca_price_pct": round(vs_fixed_dca_price_pct, 2),
        "final_value_dca_brl": round(final_value_dca_brl, 2),
        "final_value_smart_dca_brl": round(final_value_smart_brl, 2),
        "final_value_diff_brl": round(final_value_diff_brl, 2),
        "final_value_diff_pct": round(final_value_diff_pct, 2),
    }


def format_whatsapp_report(
    snapshot: dict[str, Any],
    committee: dict[str, Any],
    crisis_plan: dict[str, Any],
    dca_sim: dict[str, Any],
    cta_url: str | None = None,
) -> str:
    """Format a plain-language, honest WhatsApp bulletin (*bold* / _italic_) in BRL."""
    symbol = snapshot["symbol"]
    timeframe = snapshot["timeframe"]
    is_live = bool(snapshot.get("is_live", False))
    curr = snapshot.get("currency_symbol", "R$" if symbol.endswith("/BRL") else "$")
    ind = snapshot["latest_indicators"]
    effective_cta = (cta_url or os.getenv("TELEGRAM_CTA_URL") or "http://localhost:8000").strip()

    status_icon = {
        "JANELA_DESCONTO": "🟢",
        "ZONA_NEUTRA_FRACIONAR": "🟡",
        "ESTICADO_SEGURE_IMPULSO": "🛑",
    }.get(crisis_plan["window_status"], "🛡️")

    px_decimals = 2 if snapshot["last_price"] >= 10 else 4
    avg_decimals = 2 if dca_sim["avg_price_smart_dca"] >= 10 else 4

    top_buckets = " | ".join(f"{b['pct']}% {b['name'].split('(')[0].strip()}" for b in crisis_plan["buckets"][:3])
    sim_prefix = "[DADOS SIMULADOS — APENAS TESTE]\n" if not is_live else ""
    price_label = "Cotação agora" if is_live else "Cotação SIMULADA"

    lines = [
        f"🛡️ *SCUDEX | {symbol} ({timeframe})*",
        f"{price_label}: *{curr} {snapshot['last_price']:,.{px_decimals}f}* ({snapshot['change_24h_pct']:+.2f}% 24h)",
        f"RSI(14): {ind['RSI_14']} | Regime: {committee['regime']}",
        "",
        f"{status_icon} *Leitura da Faixa:* {crisis_plan['window_label']}",
        f"_{crisis_plan['window_advice']}_",
        f"💵 *Parcela de referência:* *{_fmt_currency(crisis_plan['suggested_today_brl'])}* (base semanal {_fmt_currency(crisis_plan['weekly_base_brl'])})",
        "",
        f"🏦 *Nível de Proteção Familiar:* {crisis_plan['alert_label']}",
        f"Divisão educativa: {top_buckets}",
        "",
        f"📊 *Comparativo Honesto ({dca_sim['days_covered']} dias):*",
        f"• Preço médio DCA Fixo: {curr} {dca_sim['avg_price_dca']:,.{avg_decimals}f} (Patrimônio: {_fmt_currency(dca_sim['final_value_dca_brl'])})",
        f"• Preço médio Smart DCA: *{curr} {dca_sim['avg_price_smart_dca']:,.{avg_decimals}f}* (Patrimônio + caixa: {_fmt_currency(dca_sim['final_value_smart_dca_brl'])})",
        f"• Caixa ainda em reserva: {_fmt_currency(dca_sim['cash_reserve_left_brl'])} | Diferença total vs. DCA fixo: *{dca_sim['final_value_diff_pct']:+.2f}%*",
        "",
        f"⚠️ _{crisis_plan['anti_scam_warning']}_",
        f"👉 Simule grátis: {effective_cta}",
    ]
    return sim_prefix + "\n".join(lines)


def evaluate_trap_check(
    snapshot: dict[str, Any],
    committee: dict[str, Any],
    claimed_side: str = "BUY",
    cta_url: str | None = None,
) -> dict[str, Any]:
    """Audit a buy/sell tip against live orderbook and regime without fabricating live status."""
    side_norm = (claimed_side or "BUY").strip().upper()
    if side_norm in ("COMPRA", "COMPRAR", "LONG", "BUY"):
        side_norm = "BUY"
        side_pt = "COMPRA"
    else:
        side_norm = "SELL"
        side_pt = "VENDA"

    is_live = bool(snapshot.get("is_live", False))
    served_exchange = snapshot.get("served_exchange", "")
    ind = snapshot["latest_indicators"]
    micro = snapshot["microstructure"]
    symbol = snapshot["symbol"]
    timeframe = snapshot["timeframe"]
    curr = snapshot.get("currency_symbol", "R$" if symbol.endswith("/BRL") else "$")
    close = float(ind["close"])
    rsi = float(ind["RSI_14"])
    bb_upper = float(ind["BB_UPPER"])
    bb_lower = float(ind["BB_LOWER"])
    bb_span = bb_upper - bb_lower
    bb_pos = ((close - bb_lower) / bb_span) if bb_span > 1e-9 else 0.5
    imbalance = float(micro.get("orderbook_imbalance", 0.0))
    spread_bps = float(micro.get("bid_ask_spread_bps", 0.0))
    regime = committee["regime"]
    score = float(committee["combined_score"])
    atr = max(float(ind["ATR_14"]), close * 0.002)

    trap_reasons: list[str] = []
    caution_reasons: list[str] = []

    if regime == "EXTREME_SHOCK":
        trap_reasons.append("Choque extremo de volatilidade ativo — altíssimo risco de violino/liquidação.")

    if side_norm == "BUY":
        if rsi >= 66.0 or bb_pos >= 0.85:
            trap_reasons.append(
                f"Preço esticado no topo curto (RSI {rsi:.1f}, Banda Superior {bb_pos * 100:.0f}%): risco alto de comprar topo."
            )
        if imbalance <= -0.25:
            trap_reasons.append(
                f"Livro de ofertas pesado na venda ({imbalance * 100:+.1f}%): pressão vendedora contra a dica."
            )
        if score <= -25.0:
            trap_reasons.append(
                f"Leitura Quantitativa aponta fluxo vendedor dominante (Score {score:+.1f})."
            )
    else:
        if rsi <= 34.0 or bb_pos <= 0.15:
            trap_reasons.append(
                f"Preço muito descontado/sobrevendido (RSI {rsi:.1f}): risco de vender exatamente no fundo."
            )
        if imbalance >= 0.25:
            trap_reasons.append(
                f"Livro de ofertas absorvendo vendas ({imbalance * 100:+.1f}% comprador)."
            )
        if score >= 25.0:
            trap_reasons.append(
                f"Leitura Quantitativa aponta força compradora dominante (Score {score:+.1f})."
            )

    if regime == "HIGH_VOL_CHOP":
        caution_reasons.append("Mercado em serra-elétrica (chop) — alta taxa de stops curtos.")
    if spread_bps > 15.0:
        caution_reasons.append(f"Spread largo ({spread_bps:.1f} bps) indicando baixa liquidez na ponta.")
    if not trap_reasons and (
        (side_norm == "BUY" and score < 15.0) or (side_norm == "SELL" and score > -15.0)
    ):
        caution_reasons.append(f"Sem confluência clara dos 4 filtros para {side_pt} (Score {score:+.1f}).")

    if trap_reasons:
        verdict_seal = "ALERTA_CILADA"
        seal_label = "ALERTA DE CILADA — RISCO ELEVADO DE IMPULSO"
        seal_icon = "🚨"
        reasons = trap_reasons + caution_reasons
    elif caution_reasons:
        verdict_seal = "RISCO_ELEVADO_FRACIONAR"
        seal_label = "RISCO ELEVADO — CAUTELA E LOTE REDUZIDO"
        seal_icon = "⚠️"
        reasons = caution_reasons
    else:
        verdict_seal = "COERENTE_COM_STOP"
        seal_label = "SEM ALERTA CRÍTICO — REFERÊNCIA TÉCNICA (USE STOP)"
        seal_icon = "✅"
        reasons = [
            f"Tendência, livro de ofertas e regime ({regime}) sem alerta de esticamento contra {side_pt} (Score {score:+.1f}).",
            "Leitura meramente técnica/educacional. Nunca arrisque dinheiro de contas essenciais.",
        ]

    stop_ref = round(close - 2.0 * atr, 4) if side_norm == "BUY" else round(close + 2.0 * atr, 4)
    effective_cta = (cta_url or os.getenv("TELEGRAM_CTA_URL") or "http://localhost:8000").strip()
    px_dec = 2 if close >= 10 else 4
    sim_banner = "[DADOS SIMULADOS — NÃO USE PARA DECISÃO REAL]\n" if not is_live else ""
    px_desc = "Cotação real agora" if is_live else "Cotação SIMULADA"

    whatsapp_share_text = sim_banner + "\n".join(
        [
            f"{seal_icon} *AUDITORIA SCUDEX | {symbol} ({timeframe})*",
            f"Dica avaliada: *{side_pt}* | {px_desc}: *{curr} {close:,.{px_dec}f}*",
            f"Veredito: *{seal_label}*",
            "",
            *[f"• {r}" for r in reasons[:3]],
            f"• Referência técnica de proteção (2×ATR): {curr} {stop_ref:,.{px_dec}f}",
            "",
            "🛡️ _Auditoria determinística pelo Scudex Quant v2 (informativo, não é recomendação financeira)._",
            f"👉 Audite qualquer dica antes de arriscar: {effective_cta}",
        ]
    )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "is_live": is_live,
        "served_exchange": served_exchange,
        "observed_at": snapshot.get("observed_at") or snapshot.get("fetched_at"),
        "fetched_at": snapshot.get("fetched_at"),
        "engine_version": "Scudex Quant v2 (Motor Determinístico)",
        "claimed_side": side_norm,
        "claimed_side_pt": side_pt,
        "verdict_seal": verdict_seal,
        "seal_label": seal_label,
        "current_price": round(close, 6),
        "currency_symbol": curr,
        "rsi_14": round(rsi, 2),
        "orderbook_imbalance_pct": round(imbalance * 100.0, 1),
        "regime": regime,
        "committee_score": round(score, 1),
        "stop_reference": stop_ref,
        "reasons": reasons,
        "whatsapp_share_text": whatsapp_share_text,
    }


def evaluate_impulse_brake(
    snapshot: dict[str, Any],
    committee: dict[str, Any],
    df: pd.DataFrame | None = None,
    planned_amount: float = 1000.0,
    claimed_side: str = "BUY",
    source_origin: str = "grupo_whatsapp",
    cta_url: str | None = None,
) -> dict[str, Any]:
    """Deterministic Impulse Brake: 10-min pause rule, historical pullback scan & 2xATR swing risk."""
    trap = evaluate_trap_check(snapshot, committee, claimed_side=claimed_side, cta_url=cta_url)
    side_norm = trap["claimed_side"]
    side_pt = trap["claimed_side_pt"]
    curr = trap.get("currency_symbol", "R$")
    ind = snapshot["latest_indicators"]
    close = float(ind["close"])
    atr = max(float(ind["ATR_14"]), close * 0.002)
    atr_2x_risk_pct = round(min(35.0, (2.0 * atr / close) * 100.0), 2) if close > 0 else 0.0
    planned_amt = max(10.0, float(planned_amount))
    estimated_swing_risk_amount = round(planned_amt * (atr_2x_risk_pct / 100.0), 2)

    current_rsi = float(ind["RSI_14"])
    bb_upper = float(ind["BB_UPPER"])
    bb_lower = float(ind["BB_LOWER"])
    bb_span = bb_upper - bb_lower
    bb_pos = ((close - bb_lower) / bb_span) if bb_span > 1e-9 else 0.5

    episodes: list[dict[str, Any]] = []
    if df is not None and len(df) >= 40 and {"close", "RSI_14", "BB_UPPER", "BB_LOWER"}.issubset(df.columns):
        closes_arr = df["close"].astype(float).tolist()
        rsi_arr = df["RSI_14"].astype(float).tolist()
        bbu_arr = df["BB_UPPER"].astype(float).tolist()
        bbl_arr = df["BB_LOWER"].astype(float).tolist()
        last_match_idx = -999
        for i in range(20, len(closes_arr) - 12):
            if (i - last_match_idx) < 6:
                continue
            c_i = closes_arr[i]
            if c_i <= 0:
                continue
            rsi_i = rsi_arr[i]
            span_i = bbu_arr[i] - bbl_arr[i]
            bb_pos_i = ((c_i - bbl_arr[i]) / span_i) if span_i > 1e-9 else 0.5

            matched = False
            if side_norm == "BUY" and (current_rsi >= 60.0 or bb_pos >= 0.75):
                matched = rsi_i >= max(58.0, current_rsi - 5.0) or bb_pos_i >= 0.75
            elif side_norm == "SELL" and (current_rsi <= 40.0 or bb_pos <= 0.25):
                matched = rsi_i <= min(42.0, current_rsi + 5.0) or bb_pos_i <= 0.25
            else:
                matched = abs(rsi_i - current_rsi) <= 6.0

            if not matched:
                continue
            last_match_idx = i
            fwd6 = ((closes_arr[i + 6] - c_i) / c_i) * 100.0
            fwd12 = ((closes_arr[i + 12] - c_i) / c_i) * 100.0
            adverse_6 = fwd6 < 0 if side_norm == "BUY" else fwd6 > 0
            adverse_12 = fwd12 < 0 if side_norm == "BUY" else fwd12 > 0
            signed_adv_12 = fwd12 if side_norm == "BUY" else -fwd12
            episodes.append(
                {
                    "adverse_6": adverse_6,
                    "adverse_12": adverse_12,
                    "signed_adv_12": signed_adv_12,
                }
            )

    if len(episodes) >= 3:
        historical_available = True
        pct_adverse_6_bars: float | None = round(
            sum(1 for e in episodes if e["adverse_6"]) / len(episodes) * 100.0, 1
        )
        pct_adverse_12_bars: float | None = round(
            sum(1 for e in episodes if e["adverse_12"]) / len(episodes) * 100.0, 1
        )
        adv_moves = [e["signed_adv_12"] for e in episodes if e["adverse_12"]]
        avg_adverse_move_pct: float | None = (
            round(sum(adv_moves) / len(adv_moves), 2) if adv_moves else 0.0
        )
        historical_summary = (
            f"Nos últimos {len(df)} candles ({snapshot['timeframe']}), encontramos {len(episodes)} momentos parecidos: "
            f"{pct_adverse_12_bars:.0f}% andaram contra essa entrada nas 12 barras seguintes "
            f"(variação adversa média: {avg_adverse_move_pct:+.2f}%)."
        )
    else:
        historical_available = False
        pct_adverse_6_bars = None
        pct_adverse_12_bars = None
        avg_adverse_move_pct = None
        historical_summary = (
            "Menos de 3 episódios idênticos na janela recente — use a volatilidade real 2×ATR abaixo como referência de risco."
        )

    if trap["verdict_seal"] == "ALERTA_CILADA" or (historical_available and (pct_adverse_12_bars or 0.0) >= 60.0):
        brake_level = "PAUSA_OBRIGATORIA_10M"
        brake_label = "🛑 FREIO ATIVADO — PAUSA DE 10 MINUTOS RECOMENDADA"
    elif trap["verdict_seal"] == "RISCO_ELEVADO_FRACIONAR":
        brake_level = "CAUTELA_FRACIONAR"
        brake_label = "⚠️ CAUTELA — PAUSE E REDUZA O LOTE"
    else:
        brake_level = "SEGUIR_COM_STOP"
        brake_label = "✅ SEM SINAL DE FOMO EXTREMO (USE STOP TÉCNICO)"

    origin_map = {
        "grupo_whatsapp": "Grupo de WhatsApp / Telegram",
        "redes_sociais": "Instagram / TikTok / YouTube",
        "amigo": "Indicação de amigo / conhecido",
        "analise_propria": "Análise própria",
    }
    origin_key = (source_origin or "grupo_whatsapp").strip().lower()
    origin_label = origin_map.get(origin_key, source_origin or "Não informada")

    reflection_questions = [
        f"Você aceita uma oscilação técnica de curto prazo (2×ATR) estimada em ~{_fmt_currency(estimated_swing_risk_amount, curr)} sobre essa entrada?",
        f"A origem da dica ({origin_label}) apresentou stop claro ou apenas urgência/FOMO?",
        "Sua reserva de emergência familiar está 100% separada e intocada?",
    ]

    effective_cta = (cta_url or os.getenv("TELEGRAM_CTA_URL") or "http://localhost:8000").strip()
    px_dec = 2 if close >= 10 else 4
    sim_banner = "[DADOS SIMULADOS — APENAS TESTE]\n" if not trap["is_live"] else ""
    telegram_text = sim_banner + "\n".join(
        [
            f"🛑 *FREIO DE IMPULSO SCUDEX | {trap['symbol']} ({trap['timeframe']})*",
            f"Veredito: *{brake_label}*",
            f"Cotação: {curr} {close:,.{px_dec}f} | RSI(14): {current_rsi:.1f} | Lote planejado: {_fmt_currency(planned_amt, curr)}",
            f"• Oscilação técnica 2×ATR: ~{_fmt_currency(estimated_swing_risk_amount, curr)} ({atr_2x_risk_pct:.2f}%)",
            f"• Histórico recente: {historical_summary}",
            "",
            "⏱️ *Pausa de 10 minutos sugerida antes de agir:*",
            f"1. {reflection_questions[0]}",
            f"2. {reflection_questions[1]}",
            f"3. {reflection_questions[2]}",
            "",
            "🛡️ _Ferramenta educativa de controle comportamental; não é recomendação._",
            f"👉 Diário de impulsos e simuladores: {effective_cta}#studio",
        ]
    )

    return {
        "symbol": trap["symbol"],
        "timeframe": trap["timeframe"],
        "is_live": trap["is_live"],
        "brake_level": brake_level,
        "brake_label": brake_label,
        "pause_seconds": 600,
        "planned_amount": round(planned_amt, 2),
        "currency_symbol": curr,
        "atr_2x_risk_pct": atr_2x_risk_pct,
        "estimated_swing_risk_amount": estimated_swing_risk_amount,
        "historical_available": historical_available,
        "similar_episodes_count": len(episodes),
        "pct_adverse_6_bars": pct_adverse_6_bars,
        "pct_adverse_12_bars": pct_adverse_12_bars,
        "avg_adverse_move_pct": avg_adverse_move_pct,
        "historical_summary": historical_summary,
        "source_origin": origin_key,
        "source_origin_label": origin_label,
        "reflection_questions": reflection_questions,
        "trap_audit": trap,
        "telegram_text": telegram_text,
        "whatsapp_share_text": telegram_text,
    }


def build_fx_treasury_signal(
    snapshot: dict[str, Any],
    monthly_usd_volume: float = 2500.0,
    profile: str = "EARN_USD",
    cta_url: str | None = None,
) -> dict[str, Any]:
    """Compute honest FX spread comparison and descriptive price band metrics for USDT/BRL."""
    is_live = bool(snapshot.get("is_live", False))
    ind = snapshot["latest_indicators"]
    rate = float(ind["close"])
    rsi = float(ind["RSI_14"])
    bb_upper = float(ind["BB_UPPER"])
    bb_lower = float(ind["BB_LOWER"])
    bb_span = bb_upper - bb_lower
    bb_pos = ((rate - bb_lower) / bb_span) if bb_span > 1e-9 else 0.5

    prof = (profile or "EARN_USD").strip().upper()
    if prof not in ("EARN_USD", "BUY_USD"):
        prof = "EARN_USD"

    monthly_brl_base = monthly_usd_volume * rate
    bank_fee_pct = 3.5
    remitter_fee_pct = 1.1
    bank_cost_brl = round(monthly_brl_base * (bank_fee_pct / 100.0), 2)
    remitter_cost_brl = round(monthly_brl_base * (remitter_fee_pct / 100.0), 2)
    spread_saved_monthly_brl = round(bank_cost_brl - remitter_cost_brl, 2)
    spread_saved_annual_brl = round(spread_saved_monthly_brl * 12.0, 2)

    if prof == "EARN_USD":
        if rsi >= 62.0 or bb_pos >= 0.72:
            action = "USD_ACIMA_MEDIA"
            action_label = "Dólar Acima da Média Recente (Faixa Superior)"
            headline = (
                f"USDT/BRL na faixa superior recente (RSI {rsi:.1f}, banda {bb_pos * 100:.0f}%)."
            )
            advice = (
                "Leitura descritiva de curto prazo: cotação acima da média das últimas 20 barras. "
                "Não constitui previsão ou recomendação cambial."
            )
        elif rsi <= 42.0 or bb_pos <= 0.28:
            action = "USD_ABAIXO_MEDIA"
            action_label = "Dólar Abaixo da Média Recente (Faixa Inferior)"
            headline = (
                f"USDT/BRL na faixa inferior recente (RSI {rsi:.1f}, banda {bb_pos * 100:.0f}%)."
            )
            advice = (
                "Leitura descritiva de curto prazo: dólar recuado em relação à média recente. "
                "Referência informativa para acompanhamento de remessas."
            )
        else:
            action = "USD_NA_MEDIA"
            action_label = "Dólar Próximo da Média Recente"
            headline = (
                f"USDT/BRL em região equilibrada (RSI {rsi:.1f}, banda {bb_pos * 100:.0f}%)."
            )
            advice = (
                "Cotação próxima à média móvel de 20 períodos; sem desvio estatístico extremo no curto prazo."
            )
    else:
        if rsi <= 42.0 or bb_pos <= 0.28:
            action = "USD_ABAIXO_MEDIA"
            action_label = "Dólar Abaixo da Média Recente (Faixa Inferior)"
            headline = (
                f"USDT/BRL recuado na faixa recente (RSI {rsi:.1f}, banda {bb_pos * 100:.0f}%)."
            )
            advice = (
                "Leitura descritiva: cotação abaixo da média móvel de 20 períodos. "
                "Compare spreads antes de converter — sem promessa de piso futuro."
            )
        elif rsi >= 62.0 or bb_pos >= 0.72:
            action = "USD_ACIMA_MEDIA"
            action_label = "Dólar Acima da Média Recente (Faixa Superior)"
            headline = (
                f"USDT/BRL esticado acima da média recente (RSI {rsi:.1f}, banda {bb_pos * 100:.0f}%)."
            )
            advice = (
                "Cotação na banda superior de 20 períodos; atenção ao ágio/spread cobrado na ponta."
            )
        else:
            action = "USD_NA_MEDIA"
            action_label = "Dólar Próximo da Média Recente"
            headline = (
                f"USDT/BRL em zona neutra (RSI {rsi:.1f}, banda {bb_pos * 100:.0f}%)."
            )
            advice = "Cotação próxima à média recente sem desvio extremo."

    effective_cta = (cta_url or os.getenv("TELEGRAM_CTA_URL") or "http://localhost:8000").strip()
    sim_banner = "[DADOS SIMULADOS — APENAS TESTE]\n" if not is_live else ""
    observed_at = snapshot.get("observed_at") or snapshot.get("fetched_at")
    fetched_at = snapshot.get("fetched_at")
    fetched_utc = f"{str(fetched_at)[:16].replace('T', ' ')} UTC" if fetched_at else "--"
    served_ex = str(snapshot.get("served_exchange", "binance"))
    rate_source_lbl = (
        f"Cotação {served_ex.upper()} (cripto-spot USDT/BRL; pode divergir do PTAX)"
        if is_live
        else "Cotação SIMULADA"
    )
    quote_basis = (
        f"{served_ex.capitalize()} USDT/BRL (cripto-spot; referência contínua)"
        if is_live
        else "Simulação sintética"
    )

    whatsapp_share_text = sim_banner + "\n".join(
        [
            "💱 *COMPARADOR CAMBIAL SCUDEX (USDT/BRL)*",
            f"{rate_source_lbl}: *R$ {rate:,.4f}* (RSI {rsi:.1f})",
            f"Atualizado: {fetched_utc}",
            f"Perfil: *{'Recebe em Dólar' if prof == 'EARN_USD' else 'Compra Dólar'}*",
            f"📌 Leitura da faixa: *{action_label}*",
            f"_{advice}_",
            "",
            f"💰 *Comparativo de Spread p/ US$ {monthly_usd_volume:,.0f}/mês:*",
            f"• Custo estimado banco tradicional (~3,5%): {_fmt_currency(bank_cost_brl)}/mês",
            f"• Custo estimado conta global/remessadora (~1,1%): {_fmt_currency(remitter_cost_brl)}/mês",
            f"• *Diferença verificável de spread:* *+{_fmt_currency(spread_saved_monthly_brl)}/mês* ({_fmt_currency(spread_saved_annual_brl)}/ano)",
            "",
            "ℹ️ _Simulação informativa de spread; não garante ganho de timing futuro._",
            f"👉 Calcule grátis: {effective_cta}",
        ]
    )

    sma_20 = round(float(ind.get("BB_MID") or rate), 4)
    moment_diagnosis = evaluate_expensive_moment(
        symbol=snapshot["symbol"],
        current_rate=rate,
        sma_20=sma_20,
        rsi_14=rsi,
        bb_pos=bb_pos,
    )

    return {
        "symbol": snapshot["symbol"],
        "served_exchange": served_ex,
        "is_live": is_live,
        "observed_at": observed_at,
        "fetched_at": fetched_at,
        "quote_type": snapshot.get("quote_type", "crypto_spot" if is_live else "synthetic_fallback"),
        "quote_basis": quote_basis,
        "sma_20": sma_20,
        "moment_diagnosis": moment_diagnosis,
        "profile": prof,
        "monthly_usd_volume": round(monthly_usd_volume, 2),
        "current_rate_brl": round(rate, 4),
        "rsi_14": round(rsi, 2),
        "bb_position_pct": round(max(0.0, min(1.0, bb_pos)) * 100.0, 1),
        "action": action,
        "action_label": action_label,
        "headline": headline,
        "advice": advice,
        "bank_fee_pct": bank_fee_pct,
        "remitter_fee_pct": remitter_fee_pct,
        "bank_cost_brl": bank_cost_brl,
        "remitter_cost_brl": remitter_cost_brl,
        "spread_saved_monthly_brl": spread_saved_monthly_brl,
        "spread_saved_annual_brl": spread_saved_annual_brl,
        "whatsapp_share_text": whatsapp_share_text,
    }
