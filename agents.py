"""Deterministic multi-agent quantitative committee & risk guardrail."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load key=value pairs from local .env without extra dependencies."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round_price(value: float) -> float:
    if value >= 1000:
        return round(value, 2)
    if value >= 10:
        return round(value, 3)
    return round(value, 5)


class TrendMomentumAgent:
    """Evaluates multi-horizon EMA structure, MACD momentum, and RSI."""

    name = "Trend & Momentum Agent"
    role = "Analista Quantitativo de Tendência"

    def evaluate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        ind = snapshot["latest_indicators"]
        close = float(ind["close"])
        ema9 = float(ind["EMA_9"])
        ema21 = float(ind["EMA_21"])
        ema50 = float(ind["EMA_50"])
        ema200 = float(ind["EMA_200"])
        rsi = float(ind["RSI_14"])
        macd = float(ind["MACD"])
        macd_sig = float(ind["MACD_SIGNAL"])
        macd_hist = float(ind["MACD_HIST"])

        score = 0.0
        reasons: list[str] = []

        if ema9 > ema21:
            score += 22
            reasons.append(f"EMA9 ({_round_price(ema9)}) acima da EMA21 ({_round_price(ema21)}): viés curto altista.")
        else:
            score -= 22
            reasons.append(f"EMA9 ({_round_price(ema9)}) abaixo da EMA21 ({_round_price(ema21)}): pressão vendedora curta.")

        if close > ema50 and ema50 > ema200:
            score += 28
            reasons.append("Estrutura macro alinhada em alta (Preço > EMA50 > EMA200).")
        elif close < ema50 and ema50 < ema200:
            score -= 28
            reasons.append("Estrutura macro alinhada em baixa (Preço < EMA50 < EMA200).")
        elif close > ema200:
            score += 10
            reasons.append("Preço sustentado acima da EMA200 (suporte primário).")
        else:
            score -= 10
            reasons.append("Preço abaixo da EMA200 (resistência estrutural).")

        if macd > macd_sig and macd_hist > 0:
            score += 22
            reasons.append("MACD positivo e acima da linha de sinal (aceleração compradora).")
        elif macd < macd_sig and macd_hist < 0:
            score -= 22
            reasons.append("MACD abaixo da linha de sinal com histograma negativo.")
        else:
            reasons.append("MACD em transição sem aceleração clara.")

        if 52 <= rsi <= 68:
            score += 18
            reasons.append(f"RSI(14) em {rsi:.1f}: zona saudável de força compradora.")
        elif 32 <= rsi <= 48:
            score -= 18
            reasons.append(f"RSI(14) em {rsi:.1f}: zona de pressão vendedora controlada.")
        elif rsi > 72:
            score -= 8
            reasons.append(f"RSI(14) esticado em {rsi:.1f} (>72): risco de exaustão/correção.")
        elif rsi < 28:
            score += 8
            reasons.append(f"RSI(14) sobrevendido em {rsi:.1f} (<28): potencial repique técnico.")
        else:
            reasons.append(f"RSI(14) neutro em {rsi:.1f}.")

        score = _clamp(score, -100.0, 100.0)
        if score >= 20:
            signal = "BULLISH"
        elif score <= -20:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"

        confidence = int(_clamp(abs(score) * 0.9 + 10, 15, 96))

        return {
            "agent": self.name,
            "role": self.role,
            "signal": signal,
            "score": round(score, 1),
            "confidence": confidence,
            "reasons": reasons,
        }


class OrderFlowLiquidityAgent:
    """Evaluates L2 orderbook imbalance, bid-ask spread, and relative volume."""

    name = "Order Flow & Liquidity Agent"
    role = "Especialista em Microestrutura"

    def evaluate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        ind = snapshot["latest_indicators"]
        micro = snapshot["microstructure"]

        imbalance = float(micro.get("orderbook_imbalance", 0.0))
        spread_bps = float(micro.get("bid_ask_spread_bps", 0.0))
        vol_ratio = float(ind.get("VOL_RATIO", 1.0))
        close_px = float(ind["close"])
        open_px = float(ind["open"])
        candle_dir = 1 if close_px >= open_px else -1

        score = 0.0
        reasons: list[str] = []

        if imbalance >= 0.12:
            boost = _clamp(imbalance * 100.0, 15, 45)
            score += boost
            reasons.append(
                f"Livro de ofertas comprador (desequilíbrio +{imbalance * 100:.1f}% nos top 20 níveis)."
            )
        elif imbalance <= -0.12:
            penalty = _clamp(abs(imbalance) * 100.0, 15, 45)
            score -= penalty
            reasons.append(
                f"Livro de ofertas pressionado por venda (desequilíbrio {imbalance * 100:.1f}% nos top 20 níveis)."
            )
        else:
            reasons.append(f"Livro de ofertas equilibrado (imbalance {imbalance * 100:+.1f}%).")

        if vol_ratio >= 1.35:
            vol_impact = 28 * candle_dir
            score += vol_impact
            dir_label = "comprador" if candle_dir > 0 else "vendedor"
            reasons.append(f"Anomalia de volume ({vol_ratio:.2f}x da média) confirmando fluxo {dir_label}.")
        elif vol_ratio < 0.70:
            reasons.append(f"Volume fraco ({vol_ratio:.2f}x da média): baixa convicção institucional.")
        else:
            score += 8 * candle_dir
            reasons.append(f"Volume dentro do padrão normal ({vol_ratio:.2f}x da média).")

        if spread_bps <= 3.0:
            reasons.append(f"Spread excelente ({spread_bps:.2f} bps): alta liquidez e baixo slippage.")
        elif spread_bps <= 12.0:
            reasons.append(f"Spread aceitável ({spread_bps:.2f} bps) para execução.")
        else:
            score *= 0.6
            reasons.append(f"Spread aberto ({spread_bps:.2f} bps): liquidez rasa, risco elevado de slippage.")

        score = _clamp(score, -100.0, 100.0)
        if score >= 18:
            signal = "BULLISH"
        elif score <= -18:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"

        confidence = int(_clamp(abs(score) * 0.85 + (15 if vol_ratio > 1.1 else 5), 15, 95))

        return {
            "agent": self.name,
            "role": self.role,
            "signal": signal,
            "score": round(score, 1),
            "confidence": confidence,
            "reasons": reasons,
        }


class VolatilityRegimeAgent:
    """Classifies market regime to guard against choppy or shock conditions."""

    name = "Volatility & Regime Agent"
    role = "Analista de Regime de Mercado"

    def evaluate(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        ind = snapshot["latest_indicators"]
        df = snapshot.get("df")

        atr_pct = float(ind["ATR_PCT"])
        bb_width = float(ind["BB_WIDTH"])
        ker = float(ind["KER_14"])
        atr = float(ind["ATR_14"])

        last_range = float(ind["high"]) - float(ind["low"])
        range_vs_atr = (last_range / atr) if atr > 0 else 1.0

        atr_baseline = float(df["ATR_PCT"].tail(80).median()) if (df is not None and len(df) >= 20) else atr_pct
        vol_expansion = (atr_pct / atr_baseline) if atr_baseline > 0 else 1.0

        reasons: list[str] = []

        if range_vs_atr >= 2.6 or vol_expansion >= 2.1 or atr_pct >= 4.5:
            regime = "EXTREME_SHOCK"
            signal = "RISK_OFF"
            score = -85.0
            confidence = 92
            reasons.append(
                f"Choque de volatilidade detectado (ATR {atr_pct:.2f}%, {vol_expansion:.2f}x da mediana, candle {range_vs_atr:.1f}x ATR)."
            )
            reasons.append("Regime impróprio para alavancagem ou entradas direcionais cegas.")
        elif ker < 0.22 and (vol_expansion >= 1.2 or bb_width >= 0.045):
            regime = "HIGH_VOL_CHOP"
            signal = "CAUTION"
            score = -45.0
            confidence = 80
            reasons.append(
                f"Mercado lateral volátil / serra-elétrica (KER {ker:.2f} baixo com BB Width {bb_width * 100:.2f}%)."
            )
            reasons.append("Alta probabilidade de violinadas (stop-hunts) em estratégias de tendência.")
        elif ker >= 0.34:
            regime = "TRENDING_CLEAN"
            signal = "FAVORABLE"
            score = round(_clamp(ker * 110.0, 35.0, 90.0), 1)
            confidence = int(_clamp(55 + ker * 45, 55, 94))
            reasons.append(
                f"Tendência limpa e direcional (Kaufman Efficiency Ratio {ker:.2f}, ATR {atr_pct:.2f}%)."
            )
            reasons.append("Ambiente estatístico com menor ruído direcional e gestão por ATR.")
        else:
            regime = "MEAN_REVERTING"
            signal = "NEUTRAL"
            score = 15.0
            confidence = 68
            reasons.append(
                f"Regime de equilíbrio/reversão à média (KER {ker:.2f}, ATR {atr_pct:.2f}%, BB Width {bb_width * 100:.2f}%)."
            )
            reasons.append("Preferência por entradas perto das bandas de suporte/resistência e alvos curtos.")

        return {
            "agent": self.name,
            "role": self.role,
            "signal": signal,
            "regime": regime,
            "score": score,
            "confidence": confidence,
            "reasons": reasons,
            "metrics": {
                "atr_pct": round(atr_pct, 3),
                "bb_width_pct": round(bb_width * 100.0, 2),
                "kaufman_efficiency": round(ker, 3),
                "vol_expansion": round(vol_expansion, 2),
            },
        }


class ChiefRiskOfficerAgent:
    """Deterministic Risk Guardrail ('Disjuntor') that aggregates votes, vetoes bad setups, and sizes orders."""

    name = "Chief Risk Officer (Guardrail)"
    role = "Gestor de Risco & Disjuntor"
    engine_name = "Scudex Quant v2 (Motor Determinístico)"

    def evaluate(
        self,
        snapshot: dict[str, Any],
        trend_vote: dict[str, Any],
        flow_vote: dict[str, Any],
        vol_vote: dict[str, Any],
        capital_usdt: float = 10000.0,
        risk_per_trade_pct: float = 1.0,
    ) -> dict[str, Any]:
        ind = snapshot["latest_indicators"]
        micro = snapshot["microstructure"]
        symbol = snapshot["symbol"]
        timeframe = snapshot["timeframe"]
        curr = snapshot.get("currency_symbol", "R$" if symbol.endswith("/BRL") else "$")

        entry_price = float(ind["close"])
        atr = max(float(ind["ATR_14"]), entry_price * 0.001)
        spread_bps = float(micro.get("bid_ask_spread_bps", 0.0))
        regime = vol_vote["regime"]

        trend_score = float(trend_vote["score"])
        flow_score = float(flow_vote["score"])
        combined_score = 0.62 * trend_score + 0.38 * flow_score

        divergent = (
            (trend_score >= 20 and flow_score <= -18)
            or (trend_score <= -20 and flow_score >= 18)
        )

        reasons: list[str] = []
        size_multiplier = 1.0

        if regime == "EXTREME_SHOCK":
            action = "VETO_PROTECTION"
            direction = "FLAT"
            size_multiplier = 0.0
            reasons.append("DISJUNTOR ATIVADO: Choque extremo de volatilidade. Novas exposições bloqueadas.")
        elif regime == "HIGH_VOL_CHOP" and (divergent or abs(combined_score) < 35):
            action = "VETO_PROTECTION"
            direction = "FLAT"
            size_multiplier = 0.0
            reasons.append(
                "DISJUNTOR ATIVADO: Regime de serra-elétrica (chop) sem consenso entre tendência e fluxo."
            )
        elif spread_bps > 25.0:
            action = "VETO_PROTECTION"
            direction = "FLAT"
            size_multiplier = 0.0
            reasons.append(f"DISJUNTOR ATIVADO: Spread excessivo ({spread_bps:.1f} bps) deterioraria a execução.")
        elif abs(combined_score) < 18.0:
            action = "WAIT_NEUTRAL"
            direction = "FLAT"
            size_multiplier = 0.0
            reasons.append(
                f"Sem assimetria estatística suficiente (Score consolidado {combined_score:+.1f}). Aguardar melhor leitura."
            )
        else:
            direction = "LONG" if combined_score > 0 else "SHORT"
            dir_pt = "alta" if direction == "LONG" else "baixa"
            caution_reasons = []
            if regime == "HIGH_VOL_CHOP":
                caution_reasons.append("volatilidade ruidosa")
            if divergent:
                caution_reasons.append("divergência entre tendência e livro de ofertas")
            if spread_bps > 12.0:
                caution_reasons.append(f"spread elevado ({spread_bps:.1f} bps)")

            if caution_reasons:
                action = "REDUCE_SIZE_50%"
                size_multiplier = 0.5
                reasons.append(
                    f"Viés técnico de {dir_pt} com redução de 50% do lote simulado por: {', '.join(caution_reasons)}."
                )
            else:
                action = f"APPROVE_{direction}"
                size_multiplier = 1.0
                reasons.append(
                    f"Indicadores técnicos alinhados em cenário de {dir_pt} (Score {combined_score:+.1f}) no regime {regime}."
                )

        stop_dist = 2.0 * atr
        stop_dist_pct = stop_dist / entry_price

        if direction == "SHORT":
            stop_loss = entry_price + stop_dist
            tp1 = entry_price - 1.5 * stop_dist
            tp2 = entry_price - 3.0 * stop_dist
        else:
            stop_loss = max(entry_price - stop_dist, entry_price * 0.05)
            tp1 = entry_price + 1.5 * stop_dist
            tp2 = entry_price + 3.0 * stop_dist

        risk_budget_usdt = capital_usdt * (risk_per_trade_pct / 100.0) * size_multiplier
        if size_multiplier > 0 and stop_dist_pct > 0:
            raw_notional = risk_budget_usdt / stop_dist_pct
            max_cap = capital_usdt * size_multiplier
            position_size_usdt = min(raw_notional, max_cap)
            position_size_asset = position_size_usdt / entry_price
            actual_max_loss = position_size_usdt * stop_dist_pct
        else:
            position_size_usdt = 0.0
            position_size_asset = 0.0
            actual_max_loss = 0.0

        cro_confidence = int(
            _clamp(
                (trend_vote["confidence"] + flow_vote["confidence"] + vol_vote["confidence"]) / 3.0,
                25,
                97,
            )
        )

        is_executable = action in ("APPROVE_LONG", "APPROVE_SHORT", "REDUCE_SIZE_50%")
        ccxt_side = "buy" if direction == "LONG" else ("sell" if direction == "SHORT" else "none")
        ccxt_order_payload = {
            "custody_mode": "NON_CUSTODIAL_EDUCATIONAL_REF",
            "execution_status": "READY_FOR_CLIENT_REVIEW" if is_executable else f"BLOCKED ({action})",
            "exchange": snapshot["served_exchange"],
            "symbol": symbol,
            "type": "limit" if is_executable else "none",
            "side": ccxt_side,
            "amount_asset": round(position_size_asset, 6),
            "notional_usdt": round(position_size_usdt, 2),
            "price": _round_price(entry_price),
            "params": {
                "stopLoss": {"triggerPrice": _round_price(stop_loss)},
                "takeProfit": {"triggerPrice": _round_price(tp1)},
                "secondaryTarget": _round_price(tp2),
                "clientRiskPct": risk_per_trade_pct,
                "guardrailMultiplier": size_multiplier,
            },
        }

        if action == "VETO_PROTECTION":
            executive_summary = (
                f"O Disjuntor Quantitativo vetou exposição em {symbol} ({timeframe}) devido ao regime {regime}. "
                f"100% da banca simulada ({curr} {capital_usdt:,.0f}) permanece protegida em caixa até a normalização da microestrutura."
            )
        elif action == "WAIT_NEUTRAL":
            executive_summary = (
                f"Leitura neutra: ausência de confluência estatística clara em {symbol} ({timeframe}, "
                f"score consolidado {combined_score:+.1f}). O motor determinístico aguarda melhor estrutura entre tendência e fluxo."
            )
        else:
            dir_pt = "alta" if direction == "LONG" else "baixa"
            executive_summary = (
                f"Condições técnicas alinhadas para cenário de {dir_pt} em {symbol} ({timeframe}, score {combined_score:+.1f}). "
                f"Referência quantitativa educacional com lote simulado de {curr} {position_size_usdt:,.2f} e perda máxima simulada de {curr} {actual_max_loss:,.2f} (stop 2×ATR)."
            )

        telegram_alert_preview = (
            f"🛡️ [Scudex | {symbol} {timeframe}]\n"
            f"Leitura Determinística: {action}\n"
            f"Regime: {regime} | Score Comitê: {combined_score:+.1f}\n"
            + (
                f"Referência: {curr}{_round_price(entry_price)} | Stop ATR: {curr}{_round_price(stop_loss)}\n"
                f"Alvo 1 (1.5R): {curr}{_round_price(tp1)} | Alvo 2 (3R): {curr}{_round_price(tp2)}\n"
                f"Lote Simulado: {curr}{position_size_usdt:,.2f} (Perda máx. simulada: {curr}{actual_max_loss:,.2f})\n"
                "Uso educacional — não constitui recomendação de investimento."
                if is_executable
                else f"Capital protegido (0 exposto). Motivo: {reasons[0]}"
            )
        )

        cro_vote = {
            "agent": self.name,
            "role": self.role,
            "signal": action,
            "score": round(combined_score, 1),
            "confidence": cro_confidence,
            "reasons": reasons,
        }

        execution_plan = {
            "action": action,
            "direction": direction,
            "is_executable": is_executable,
            "regime": regime,
            "size_multiplier": size_multiplier,
            "entry_price": _round_price(entry_price),
            "stop_loss": _round_price(stop_loss),
            "take_profit_1": _round_price(tp1),
            "take_profit_2": _round_price(tp2),
            "stop_distance_pct": round(stop_dist_pct * 100.0, 2),
            "position_size_usdt": round(position_size_usdt, 2),
            "position_size_asset": round(position_size_asset, 6),
            "max_loss_usdt": round(actual_max_loss, 2),
            "capital_usdt": round(capital_usdt, 2),
            "risk_per_trade_pct": round(risk_per_trade_pct, 2),
        }

        return {
            "verdict": action,
            "direction": direction,
            "combined_score": round(combined_score, 1),
            "regime": regime,
            "executive_summary": executive_summary,
            "engine_name": self.engine_name,
            "agents": [trend_vote, flow_vote, vol_vote, cro_vote],
            "execution_plan": execution_plan,
            "ccxt_order_payload": ccxt_order_payload,
            "telegram_alert_preview": telegram_alert_preview,
        }


def run_committee(
    snapshot: dict[str, Any],
    capital_usdt: float = 10000.0,
    risk_per_trade_pct: float = 1.0,
) -> dict[str, Any]:
    """Run all 4 deterministic committee agents sequentially on a market snapshot."""
    trend_vote = TrendMomentumAgent().evaluate(snapshot)
    flow_vote = OrderFlowLiquidityAgent().evaluate(snapshot)
    vol_vote = VolatilityRegimeAgent().evaluate(snapshot)
    return ChiefRiskOfficerAgent().evaluate(
        snapshot=snapshot,
        trend_vote=trend_vote,
        flow_vote=flow_vote,
        vol_vote=vol_vote,
        capital_usdt=capital_usdt,
        risk_per_trade_pct=risk_per_trade_pct,
    )
