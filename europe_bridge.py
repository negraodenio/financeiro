"""Europe -> Brazil financial bridge: centralized EUR/BRL crypto-spot cross, BCB Selic, Break-Even & B3 assets."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import threading
import time
from typing import Any

import httpx
import pandas as pd

from market_data import FX_SNAPSHOT_LIMIT, compute_indicators, fetch_market_snapshot

logger = logging.getLogger(__name__)

B3_ASSETS_META: list[dict[str, str]] = [
    {"ticker": "^BVSP", "label": "Ibovespa (Índice B3 • Pontos)", "unit": "points"},
    {"ticker": "BOVA11.SA", "label": "BOVA11 (ETF Ibovespa B3)", "unit": "BRL"},
    {"ticker": "HGLG11.SA", "label": "HGLG11 (FII Logística)", "unit": "BRL"},
    {"ticker": "MXRF11.SA", "label": "MXRF11 (FII Papel)", "unit": "BRL"},
    {"ticker": "ITUB4.SA", "label": "ITUB4 (Itaú Unibanco)", "unit": "BRL"},
]

_BRIDGE_CACHE_LOCK = threading.Lock()
_SELIC_CACHE: tuple[float, dict[str, Any]] | None = None
_LAST_VALID_SELIC: dict[str, Any] | None = None
_B3_QUOTES_CACHE: dict[str, tuple[float, tuple[float, float, str | None]]] = {}


def clear_europe_bridge_cache() -> None:
    """Clear in-memory Selic and Yahoo Finance quote caches."""
    global _SELIC_CACHE, _LAST_VALID_SELIC
    with _BRIDGE_CACHE_LOCK:
        _SELIC_CACHE = None
        _LAST_VALID_SELIC = None
        _B3_QUOTES_CACHE.clear()


def _fmt_brl(value: float, decimals: int = 2) -> str:
    txt = f"{value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {txt}"


def _fmt_utc_short(iso_ts: str | None) -> str:
    if not iso_ts:
        return "--"
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc)
        return dt_utc.strftime("%d/%m %H:%M UTC")
    except Exception:
        return str(iso_ts)[:16].replace("T", " ") + " UTC"


def fetch_selic_metadata() -> dict[str, Any]:
    """Fetch official Selic Meta (% p.a.) from Banco Central SGS API (series 432) without hardcoded stale numbers."""
    global _SELIC_CACHE, _LAST_VALID_SELIC
    now_mono = time.monotonic()
    with _BRIDGE_CACHE_LOCK:
        if _SELIC_CACHE and (now_mono - _SELIC_CACHE[0]) < 21600.0:
            return dict(_SELIC_CACHE[1])

    url = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json"
    try:
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 200:
            rows = resp.json()
            if rows and "valor" in rows[-1]:
                val = float(str(rows[-1]["valor"]).replace(",", "."))
                if 2.0 <= val <= 35.0:
                    observed_date = str(
                        rows[-1].get("data") or datetime.now(timezone.utc).strftime("%d/%m/%Y")
                    ).strip()
                    res = {
                        "rate_pct": round(val, 2),
                        "source": "Banco Central SGS (Série 432)",
                        "is_live": True,
                        "is_stale": False,
                        "observed_date": observed_date,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "status": "LIVE",
                    }
                    with _BRIDGE_CACHE_LOCK:
                        _SELIC_CACHE = (now_mono, res)
                        _LAST_VALID_SELIC = dict(res)
                    return dict(res)
    except Exception as exc:
        logger.warning("BCB Selic API unavailable: %s", exc)

    with _BRIDGE_CACHE_LOCK:
        last_valid = dict(_LAST_VALID_SELIC) if _LAST_VALID_SELIC else None

    if last_valid is not None:
        last_valid["is_live"] = False
        last_valid["is_stale"] = True
        last_valid["status"] = "STALE_CACHED"
        last_valid["source"] = (
            f"Última leitura BCB ({last_valid.get('observed_date') or 'cache'} — BCB temporariamente indisponível)"
        )
        last_valid["fetched_at"] = datetime.now(timezone.utc).isoformat()
        return last_valid

    return {
        "rate_pct": None,
        "source": "Selic indisponível (API do Banco Central sem resposta)",
        "is_live": False,
        "is_stale": False,
        "observed_date": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "UNAVAILABLE",
    }


def fetch_selic_rate() -> tuple[float | None, str, bool]:
    """Thin backward-compatible wrapper over fetch_selic_metadata()."""
    meta = fetch_selic_metadata()
    return meta["rate_pct"], meta["source"], meta["is_live"]


def fetch_eur_brl_snapshot(
    timeframe: str = "4h",
    allow_synthetic: bool | None = None,
) -> dict[str, Any]:
    """Compute crypto-spot EUR/BRL candles by combining EUR/USDT and USDT/BRL with unified cross rate."""
    eur_snap = fetch_market_snapshot(
        symbol="EUR/USDT",
        timeframe=timeframe,
        exchange_id="binance",
        limit=FX_SNAPSHOT_LIMIT,
        allow_synthetic=allow_synthetic,
    )
    usd_snap = fetch_market_snapshot(
        symbol="USDT/BRL",
        timeframe=timeframe,
        exchange_id="binance",
        limit=FX_SNAPSHOT_LIMIT,
        allow_synthetic=allow_synthetic,
    )

    df_eur = eur_snap["df"].copy()
    df_usd = usd_snap["df"].copy()

    merged = pd.merge(
        df_eur[["timestamp", "datetime", "open", "high", "low", "close", "volume"]],
        df_usd[["timestamp", "open", "high", "low", "close"]],
        on="timestamp",
        suffixes=("_eur", "_usd"),
    )

    if len(merged) < 50:
        n = min(len(df_eur), len(df_usd))
        e_tail = df_eur.tail(n).reset_index(drop=True)
        u_tail = df_usd.tail(n).reset_index(drop=True)
        df_fx = pd.DataFrame(
            {
                "timestamp": e_tail["timestamp"],
                "datetime": e_tail["datetime"],
                "open": e_tail["open"] * u_tail["open"],
                "high": e_tail["high"] * u_tail["high"],
                "low": e_tail["low"] * u_tail["low"],
                "close": e_tail["close"] * u_tail["close"],
                "volume": e_tail["volume"],
            }
        )
    else:
        df_fx = pd.DataFrame(
            {
                "timestamp": merged["timestamp"],
                "datetime": merged["datetime"],
                "open": merged["open_eur"] * merged["open_usd"],
                "high": merged["high_eur"] * merged["high_usd"],
                "low": merged["low_eur"] * merged["low_usd"],
                "close": merged["close_eur"] * merged["close_usd"],
                "volume": merged["volume"],
            }
        )

    df_fx = df_fx.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    df_ind = compute_indicators(df_fx)
    last = df_ind.iloc[-1]

    eur_usd_px = round(float(eur_snap["last_price"]), 4)
    usd_brl_px = round(float(usd_snap["last_price"]), 4)
    eur_brl_px = round(eur_usd_px * usd_brl_px, 4)

    bars_24h = max(1, min(len(df_ind) - 1, 6 if timeframe == "4h" else 24))
    ref_close = float(df_ind["close"].iloc[-1 - bars_24h])
    chg_24h = ((eur_brl_px - ref_close) / ref_close * 100.0) if ref_close > 0 else 0.0

    # Window descriptive statistics
    sma_20 = round(float(df_ind["close"].tail(20).mean()), 4)
    tail_120 = df_ind.tail(120)
    period_high = round(float(tail_120["high"].max()), 4)
    period_low = round(float(tail_120["low"].min()), 4)
    pct_vs_sma20 = round(((eur_brl_px - sma_20) / sma_20 * 100.0) if sma_20 > 0 else 0.0, 2)

    # Candles formatted for TradingView Lightweight Charts (strictly ascending unique epoch seconds)
    candles: list[dict[str, Any]] = []
    seen_times: set[int] = set()
    for _, row in tail_120.iterrows():
        t_sec = int(float(row["timestamp"]) // 1000)
        if t_sec in seen_times:
            continue
        seen_times.add(t_sec)
        candles.append(
            {
                "time": t_sec,
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
            }
        )
    candles.sort(key=lambda item: item["time"])
    if candles:
        candles[-1]["close"] = eur_brl_px
        candles[-1]["high"] = round(max(candles[-1]["high"], eur_brl_px), 4)
        candles[-1]["low"] = round(min(candles[-1]["low"], eur_brl_px), 4)

    is_live = bool(eur_snap["is_live"] and usd_snap["is_live"])
    observed_at = str(
        eur_snap.get("observed_at")
        or usd_snap.get("observed_at")
        or datetime.now(timezone.utc).isoformat()
    )
    fetched_at = datetime.now(timezone.utc).isoformat()
    served_ex = str(eur_snap["served_exchange"])

    return {
        "symbol": "EUR/BRL",
        "timeframe": timeframe,
        "is_live": is_live,
        "source": f"{served_ex} (EUR/USDT × USDT/BRL)" if is_live else "synthetic_fx_fallback",
        "quote_type": "crypto_spot_cross" if is_live else "synthetic_fallback",
        "quote_basis": (
            f"{served_ex.capitalize()} EUR/USDT × USDT/BRL (cripto-spot; pode divergir do PTAX/comercial)"
            if is_live
            else "Simulação sintética (apenas teste)"
        ),
        "usd_quote_basis": (
            f"{str(usd_snap['served_exchange']).capitalize()} USDT/BRL (cripto-spot; referência contínua)"
            if is_live
            else "Simulação sintética"
        ),
        "validity_status": "LIVE" if is_live else "SIMULATED",
        "cache_ttl_sec": int(eur_snap.get("cache_ttl_sec", 45)),
        "observed_at": observed_at,
        "fetched_at": fetched_at,
        "eur_usd_rate": eur_usd_px,
        "usd_brl_rate": usd_brl_px,
        "eur_brl_rate": eur_brl_px,
        "change_24h_pct": round(chg_24h, 2),
        "rsi_14": round(float(last["RSI_14"]), 2),
        "bb_upper": round(float(last["BB_UPPER"]), 4),
        "bb_lower": round(float(last["BB_LOWER"]), 4),
        "sma_20": sma_20,
        "period_high": period_high,
        "period_low": period_low,
        "pct_vs_sma20": pct_vs_sma20,
        "candles": candles,
    }


def fetch_brazil_assets_in_eur(eur_brl_rate: float) -> list[dict[str, Any]]:
    """Fetch live B3 index (in points), ETFs, FIIs, and equities via Yahoo Finance (5-min cache)."""
    headers = {"User-Agent": "Mozilla/5.0"}
    results: list[dict[str, Any]] = []
    now_mono = time.monotonic()

    for item in B3_ASSETS_META:
        ticker = item["ticker"]
        unit = item.get("unit", "BRL")
        price_val: float | None = None
        chg_1m: float | None = None
        observed_at: str | None = None
        live = False

        with _BRIDGE_CACHE_LOCK:
            cached = _B3_QUOTES_CACHE.get(ticker)
            if cached and (now_mono - cached[0]) < 300.0:
                price_val, chg_1m, observed_at = cached[1]
                live = True

        if not live:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1mo&interval=1d"
                resp = httpx.get(url, headers=headers, timeout=4.0)
                if resp.status_code == 200:
                    chart = resp.json()["chart"]["result"][0]
                    meta = chart.get("meta") or {}
                    reg_ts = meta.get("regularMarketTime")
                    if reg_ts is not None:
                        try:
                            observed_at = datetime.fromtimestamp(int(reg_ts), tz=timezone.utc).isoformat()
                        except Exception:
                            observed_at = None
                    closes = [c for c in chart["indicators"]["quote"][0]["close"] if c is not None]
                    if len(closes) >= 2:
                        price_val = float(closes[-1])
                        chg_1m = (closes[-1] - closes[0]) / closes[0] * 100.0
                        live = True
                        with _BRIDGE_CACHE_LOCK:
                            _B3_QUOTES_CACHE[ticker] = (now_mono, (price_val, chg_1m, observed_at))
            except Exception as exc:
                logger.debug("Yahoo Finance unavailable for %s: %s", ticker, exc)

        if live and price_val is not None and chg_1m is not None:
            if unit == "points":
                results.append(
                    {
                        "ticker": ticker.replace(".SA", ""),
                        "label": item["label"],
                        "unit": "points",
                        "price_points": int(round(price_val)),
                        "price_brl": None,
                        "price_eur": None,
                        "change_1m_pct": round(chg_1m, 2),
                        "observed_at": observed_at,
                        "source": "Yahoo Finance (B3 • pregão)",
                        "tag": "Índice em pontos (não é preço em R$; veja BOVA11)",
                        "is_live": True,
                    }
                )
            else:
                price_eur = round(price_val / max(eur_brl_rate, 0.1), 2)
                results.append(
                    {
                        "ticker": ticker.replace(".SA", ""),
                        "label": item["label"],
                        "unit": "BRL",
                        "price_points": None,
                        "price_brl": round(price_val, 2),
                        "price_eur": price_eur,
                        "change_1m_pct": round(chg_1m, 2),
                        "observed_at": observed_at,
                        "source": "Yahoo Finance (B3 • pregão)",
                        "tag": "Cotação B3 informativa (30d)",
                        "is_live": True,
                    }
                )
        else:
            results.append(
                {
                    "ticker": ticker.replace(".SA", ""),
                    "label": item["label"],
                    "unit": unit,
                    "price_points": None,
                    "price_brl": None,
                    "price_eur": None,
                    "change_1m_pct": None,
                    "observed_at": None,
                    "source": "Yahoo Finance (B3)",
                    "tag": "Cotação indisponível",
                    "is_live": False,
                }
            )

    return results


def evaluate_expensive_moment(
    symbol: str,
    current_rate: float,
    sma_20: float,
    rsi_14: float,
    bb_pos: float,
) -> dict[str, Any]:
    """Classify FX timing deviation vs 20-period moving average ('Momento Caro / Favorável')."""
    deviation_pct = round(((current_rate - sma_20) / sma_20) * 100.0, 2) if sma_20 > 0 else 0.0
    if deviation_pct >= 1.2 or rsi_14 >= 65.0:
        status = "MOMENTO_CARO"
        label = "Momento Caro (Acima da Média Recente)"
        summary = (
            f"{symbol} está {deviation_pct:+.2f}% acima da média recente de 20 períodos "
            f"(R$ {sma_20:.4f}) — vale comparar spread ou fracionar."
        )
    elif deviation_pct <= -1.2 or rsi_14 <= 38.0:
        status = "MOMENTO_FAVORAVEL"
        label = "Momento Favorável (Abaixo da Média Recente)"
        summary = (
            f"{symbol} está {deviation_pct:+.2f}% abaixo da média recente de 20 períodos "
            f"(R$ {sma_20:.4f})."
        )
    else:
        status = "MOMENTO_NEUTRO"
        label = "Faixa Equilibrada (Próximo da Média)"
        summary = (
            f"{symbol} está próximo da média recente de 20 períodos "
            f"({deviation_pct:+.2f}%; média R$ {sma_20:.4f})."
        )

    base_avg = sma_20 if sma_20 > 0 else current_rate
    return {
        "symbol": symbol,
        "status": status,
        "label": label,
        "summary": summary,
        "deviation_pct": deviation_pct,
        "sma_20": round(base_avg, 4),
        "suggested_expensive_rate": round(base_avg * 1.015, 4),
        "suggested_favorable_rate": round(base_avg * 0.985, 4),
    }


def build_europe_brazil_bridge(
    amount_eur: float = 1500.0,
    investment_type: str = "SELIC",
    timeframe: str = "4h",
    cta_url: str | None = None,
    allow_synthetic: bool | None = None,
) -> dict[str, Any]:
    """Compute descriptive EUR/BRL positioning, honest regulated remittance comparison, and 12m FX break-even."""
    fx = fetch_eur_brl_snapshot(timeframe=timeframe, allow_synthetic=allow_synthetic)
    selic_meta = fetch_selic_metadata()
    selic_pct = selic_meta["rate_pct"]
    selic_source = selic_meta["source"]
    selic_is_live = selic_meta["is_live"]
    selic_is_stale = selic_meta["is_stale"]
    selic_observed_date = selic_meta["observed_date"]

    eur_brl = fx["eur_brl_rate"]
    rsi = fx["rsi_14"]
    bb_span = fx["bb_upper"] - fx["bb_lower"]
    bb_pos = ((eur_brl - fx["bb_lower"]) / bb_span) if bb_span > 1e-9 else 0.5

    if rsi >= 62.0 or bb_pos >= 0.72:
        remittance_signal = "EUR_ACIMA_MEDIA"
        remittance_label = "Euro Acima da Média Recente (20 períodos)"
        remittance_advice = (
            f"EUR/BRL em R$ {eur_brl:.4f} (faixa Bollinger {bb_pos * 100:.0f}%, RSI {rsi:.1f}): "
            "cotação acima da média móvel de 20 períodos ({:.2f}% vs. média). Indicador descritivo da janela recente, não constitui recomendação ou previsão cambial."
        ).format(fx["pct_vs_sma20"])
    elif rsi <= 42.0 or bb_pos <= 0.28:
        remittance_signal = "EUR_ABAIXO_MEDIA"
        remittance_label = "Euro Abaixo da Média Recente (20 períodos)"
        remittance_advice = (
            f"EUR/BRL em R$ {eur_brl:.4f} (faixa Bollinger {bb_pos * 100:.0f}%, RSI {rsi:.1f}): "
            "cotação abaixo da média móvel de 20 períodos ({:.2f}% vs. média). Indicador descritivo da janela recente, sem previsão de movimentação futura."
        ).format(fx["pct_vs_sma20"])
    else:
        remittance_signal = "EUR_NA_MEDIA"
        remittance_label = "Euro Próximo da Média Recente"
        remittance_advice = (
            f"EUR/BRL em R$ {eur_brl:.4f} (faixa Bollinger {bb_pos * 100:.0f}%, RSI {rsi:.1f}): "
            "cotação próxima à média móvel de 20 períodos ({:.2f}% vs. média). Referência descritiva objetiva da faixa atual."
        ).format(fx["pct_vs_sma20"])

    # Honest Remittance Route Comparison (Spot ref vs. Regulated Remitter vs. Traditional Bank)
    gross_brl = round(amount_eur * eur_brl, 2)
    bank_fee_pct = 3.8
    remitter_fee_pct = 1.1
    iof_outbound_pct = 1.1
    iof_return_pct = 0.38
    bank_brl = round(gross_brl * (1.0 - bank_fee_pct / 100.0), 2)
    remitter_brl = round(gross_brl * (1.0 - remitter_fee_pct / 100.0), 2)
    bank_cost_brl = round(gross_brl - bank_brl, 2)
    remitter_cost_brl = round(gross_brl - remitter_brl, 2)
    brl_saved_vs_bank = round(remitter_brl - bank_brl, 2)

    eu_yield_pct = 2.50
    horizon_months = 12
    inv_norm = (investment_type or "SELIC").strip().upper()

    if inv_norm == "FIIS":
        carry_available = True
        gross_br_yield: float | None = 10.8
        net_br_yield: float | None = 10.8
        ir_tax_pct = 0.0
        inv_label = "Fundos Imobiliários (Ref. informativa ~10,8% a.a.)"
        ir_note = "Rendimentos de FIIs isentos de IR para PF que atendam critérios legais (ganho de capital de cotas: 20%)."
    elif selic_pct is None:
        carry_available = False
        gross_br_yield = None
        net_br_yield = None
        ir_tax_pct = 17.5
        inv_label = (
            "Tesouro IPCA+ (aguardando Selic BCB)"
            if inv_norm == "IPCA"
            else "Tesouro Selic / CDB 100% CDI (aguardando Selic BCB)"
        )
        ir_note = "IR regressivo de 17,5% em 365 dias indisponível enquanto a API do BCB estiver fora do ar."
    elif inv_norm == "IPCA":
        carry_available = True
        gross_br_yield = round(selic_pct + 1.0, 2)
        net_br_yield = round(gross_br_yield * 0.825, 2)
        ir_tax_pct = 17.5
        inv_label = "Tesouro IPCA+ (Estimativa 12m líquida IR)"
        ir_note = "Alíquota regressiva de IR Renda Fixa de 17,5% (prazo de 12 meses / 365 dias)."
    else:
        inv_norm = "SELIC"
        carry_available = True
        gross_br_yield = selic_pct
        net_br_yield = round(selic_pct * 0.825, 2)
        ir_tax_pct = 17.5
        inv_label = f"Tesouro Selic / CDB 100% CDI ({selic_pct:.2f}% a.a. bruto)"
        ir_note = "Alíquota regressiva de IR Renda Fixa de 17,5% (prazo de 12 meses / 365 dias)."

    outbound_cost_pct = round(remitter_fee_pct + iof_outbound_pct, 2)
    return_cost_pct = round(remitter_fee_pct + iof_return_pct, 2)

    if carry_available and net_br_yield is not None:
        break_even_eur_brl_12m: float | None = round(
            eur_brl * ((1.0 + net_br_yield / 100.0) / (1.0 + eu_yield_pct / 100.0)), 4
        )
        safety_margin_pct: float | None = round(((break_even_eur_brl_12m - eur_brl) / eur_brl) * 100.0, 2)
        rt_factor = (1.0 - outbound_cost_pct / 100.0) * (1.0 - return_cost_pct / 100.0)
        break_even_round_trip_12m: float | None = round(
            eur_brl * rt_factor * ((1.0 + net_br_yield / 100.0) / (1.0 + eu_yield_pct / 100.0)), 4
        )
        safety_margin_round_trip_pct: float | None = round(
            ((break_even_round_trip_12m - eur_brl) / eur_brl) * 100.0, 2
        )
    else:
        break_even_eur_brl_12m = None
        safety_margin_pct = None
        break_even_round_trip_12m = None
        safety_margin_round_trip_pct = None

    assumptions = {
        "horizon_months": horizon_months,
        "ir_tax_pct": ir_tax_pct,
        "ir_note": ir_note,
        "remitter_fee_pct": remitter_fee_pct,
        "iof_outbound_pct": iof_outbound_pct,
        "iof_return_pct": iof_return_pct,
        "total_outbound_cost_pct": outbound_cost_pct,
        "total_return_cost_pct": return_cost_pct,
        "iof_note": (
            f"Remessadora ~{remitter_fee_pct:.1f}% + IOF {iof_outbound_pct:.1f}% (mesma titularidade; 0,38% para terceiros) "
            f"na ida e {iof_return_pct:.2f}% na volta."
        ),
        "eu_benchmark_pct": eu_yield_pct,
        "eu_benchmark_note": f"Referência líquida estimada em Euro de {eu_yield_pct:.2f}% a.a. (conta remunerada / títulos EUR).",
        "formula_pure": "Break-Even (juros) = EUR/BRL × ((1 + r_BR_líq) / (1 + r_EU))",
        "formula_round_trip": (
            f"Break-Even (ida e volta) = EUR/BRL × (1 - {outbound_cost_pct:.2f}%) × (1 - {return_cost_pct:.2f}%) × "
            "((1 + r_BR_líq) / (1 + r_EU))"
        ),
        "fx_risk_warning": (
            "Se o Euro subir acima do Break-Even no período, o ganho de juros no Brasil não compensa a desvalorização cambial do Real."
        ),
    }

    assets_in_eur = fetch_brazil_assets_in_eur(eur_brl)
    fiscal_notice = (
        "Nota Fiscal e Regulatória: residentes no exterior que entregaram Declaração de Saída Definitiva "
        "devem observar regras de conta CDE / investidor não-residente (CVM) antes de aplicar no Brasil. "
        "Remessas internacionais e operações com ativos virtuais seguem normas cambiais e tributárias vigentes "
        "(incluindo BCB Res. 521/2025 e IOF). Estimativas apenas informativas — não é consultoria financeira, jurídica ou fiscal."
    )

    effective_cta = (cta_url or os.getenv("TELEGRAM_CTA_URL") or "http://localhost:8000").strip()
    sim_banner = "[DADOS SIMULADOS - APENAS TESTE]\n" if not fx["is_live"] else ""
    fetched_short = _fmt_utc_short(fx["fetched_at"])

    moment_diagnosis = evaluate_expensive_moment(
        symbol="EUR/BRL",
        current_rate=eur_brl,
        sma_20=fx["sma_20"],
        rsi_14=rsi,
        bb_pos=bb_pos,
    )
    fx["moment_diagnosis"] = moment_diagnosis

    if carry_available and selic_pct is not None and net_br_yield is not None and break_even_eur_brl_12m is not None:
        carry_lines = [
            f"🧮 *Break-Even Cambial 12m ({inv_label}):*",
            (
                f"• Taxa ref. ({selic_source}): {selic_pct:.2f}% a.a. | Líquido est. BR (IR {ir_tax_pct:.1f}%): "
                f"{net_br_yield:.2f}% vs. Europa {eu_yield_pct:.2f}%"
            ),
            f"• Break-Even juros (12m, sem remessa/IOF): *R$ {break_even_eur_brl_12m:,.4f}* ({safety_margin_pct:+.1f}% de folga cambial).",
            f"• Ida e volta (com remessa+IOF est. {outbound_cost_pct:.2f}% / {return_cost_pct:.2f}%): *R$ {break_even_round_trip_12m:,.4f}* ({safety_margin_round_trip_pct:+.1f}%).",
        ]
    elif carry_available and net_br_yield is not None and break_even_eur_brl_12m is not None:
        carry_lines = [
            f"🧮 *Break-Even Cambial 12m ({inv_label}):*",
            f"• Líquido est. BR: {net_br_yield:.2f}% vs. Europa {eu_yield_pct:.2f}%",
            f"• Break-Even juros (12m): *R$ {break_even_eur_brl_12m:,.4f}* ({safety_margin_pct:+.1f}% de folga cambial).",
            f"• Ida e volta (com remessa+IOF est. {outbound_cost_pct:.2f}% / {return_cost_pct:.2f}%): *R$ {break_even_round_trip_12m:,.4f}* ({safety_margin_round_trip_pct:+.1f}%).",
        ]
    else:
        carry_lines = [
            "🧮 *Break-Even Cambial 12m:*",
            "• Selic indisponível no Banco Central no momento — Break-Even temporariamente indisponível.",
        ]

    whatsapp_share_text = sim_banner + "\n".join(
        [
            "🇪🇺➡️🇧🇷 *PONTE EUROPA → BRASIL | RADAR INFORMATIVO*",
            f"EUR/BRL ({fx['source']} • cripto-spot): *R$ {eur_brl:,.4f}* (RSI {rsi:.1f})",
            f"Atualizado: {fetched_short} | Tipo: {fx['quote_type']}",
            f"📌 Leitura da faixa recente: *{remittance_label}*",
            "",
            f"💸 *Simulação de Remessa (€ {amount_eur:,.0f}):*",
            f"• Câmbio spot de referência (0% tarifa): {_fmt_brl(gross_brl)}",
            f"• Banco tradicional (~3,8% spread/tarifas): {_fmt_brl(bank_brl)} (custo est. {_fmt_brl(bank_cost_brl)})",
            f"• Remessadora regulada (ex.: Wise / Remessa Online ~1,1%): *{_fmt_brl(remitter_brl)}* (custo est. {_fmt_brl(remitter_cost_brl)})",
            f"• *Diferença estimada vs. banco tradicional:* *+{_fmt_brl(brl_saved_vs_bank)}*",
            "",
            *carry_lines,
            "",
            f"⚠️ _{fiscal_notice}_",
            f"👉 Simulador gratuito e alertas USD/EUR: {effective_cta}#studio",
        ]
    )

    return {
        "fx": fx,
        "moment_diagnosis": moment_diagnosis,
        "amount_eur": round(amount_eur, 2),
        "remittance_signal": remittance_signal,
        "remittance_label": remittance_label,
        "remittance_advice": remittance_advice,
        "routes": {
            "gross_spot_brl": gross_brl,
            "bank_fee_pct": bank_fee_pct,
            "bank_brl": bank_brl,
            "bank_cost_brl": bank_cost_brl,
            "remitter_fee_pct": remitter_fee_pct,
            "remitter_brl": remitter_brl,
            "remitter_cost_brl": remitter_cost_brl,
            "brl_saved_vs_bank": brl_saved_vs_bank,
        },
        "carry_trade": {
            "available": carry_available,
            "investment_type": inv_norm,
            "investment_label": inv_label,
            "selic_pct": selic_pct,
            "selic_source": selic_source,
            "selic_is_live": selic_is_live,
            "selic_is_stale": selic_is_stale,
            "selic_observed_date": selic_observed_date,
            "gross_br_yield_pct": gross_br_yield,
            "net_br_yield_pct": net_br_yield,
            "eu_benchmark_pct": eu_yield_pct,
            "break_even_eur_brl_12m": break_even_eur_brl_12m,
            "safety_margin_pct": safety_margin_pct,
            "break_even_round_trip_12m": break_even_round_trip_12m,
            "safety_margin_round_trip_pct": safety_margin_round_trip_pct,
            "assumptions": assumptions,
        },
        "brazil_assets_in_eur": assets_in_eur,
        "fiscal_notice": fiscal_notice,
        "whatsapp_share_text": whatsapp_share_text,
    }
