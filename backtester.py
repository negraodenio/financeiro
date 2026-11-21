"""Fair A/B backtesting engine comparing identical position sizing with vs. without regime filter."""

from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
import pandas as pd

from market_data import TIMEFRAME_SECONDS

STRATEGY_META: dict[str, dict[str, str]] = {
    "trend_ema_crossover": {
        "name": "Trend EMA Crossover (9/21 + RSI)",
        "description": "Entrada no cruzamento EMA 9 > EMA 21 com momentum RSI; saída na reversão, stop 2×ATR ou alvo 3R.",
    },
    "bollinger_mean_reversion": {
        "name": "Bollinger Mean Reversion (20, 2σ)",
        "description": "Entrada em toques na Banda Inferior com RSI sobrevendido; saída na média/banda superior, stop 2×ATR ou alvo 3R.",
    },
    "momentum_breakout": {
        "name": "Momentum Breakout (20-Bar High + Volume)",
        "description": "Rompimentos de máxima de 20 períodos confirmados por aceleração de volume com stop 2×ATR e alvo 3R.",
    },
}


def _compute_metrics(
    equity_series: list[float],
    trades_pnl_pct: list[float],
    capital_usdt: float,
    timeframe: str,
) -> dict[str, Any]:
    """Calculate return, max drawdown, annualized Sharpe, win rate, and profit factor."""
    arr = np.asarray(equity_series, dtype=float)
    final_eq = float(arr[-1]) if len(arr) > 0 else capital_usdt
    total_return_pct = ((final_eq - capital_usdt) / capital_usdt * 100.0) if capital_usdt > 0 else 0.0

    peaks = np.maximum.accumulate(arr)
    drawdowns = np.where(peaks > 0, (arr - peaks) / peaks * 100.0, 0.0)
    max_drawdown_pct = abs(float(np.min(drawdowns))) if len(drawdowns) > 0 else 0.0

    sharpe = 0.0
    if len(arr) > 2:
        bar_rets = np.diff(arr) / np.where(arr[:-1] > 0, arr[:-1], 1.0)
        std_ret = float(np.std(bar_rets, ddof=1))
        if std_ret > 1e-10:
            step_sec = TIMEFRAME_SECONDS.get(timeframe, 3600)
            bars_per_year = (365.0 * 86400.0) / step_sec
            sharpe = float(np.mean(bar_rets) / std_ret * math.sqrt(bars_per_year))

    total_trades = len(trades_pnl_pct)
    if total_trades > 0:
        wins = [p for p in trades_pnl_pct if p > 0]
        losses = [abs(p) for p in trades_pnl_pct if p < 0]
        win_rate_pct = len(wins) / total_trades * 100.0
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        if gross_loss > 1e-9:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = 9.99
        else:
            profit_factor = 0.0
    else:
        win_rate_pct = 0.0
        profit_factor = 0.0

    return {
        "final_equity_usdt": round(final_eq, 2),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe, 2),
        "win_rate_pct": round(win_rate_pct, 1),
        "profit_factor": round(min(profit_factor, 99.9), 2),
        "total_trades": total_trades,
    }


def _generate_signals(df: pd.DataFrame, strategy: str) -> tuple[np.ndarray, np.ndarray]:
    """Return boolean arrays (entry_signal, exit_signal) with zero look-ahead."""
    close = df["close"].to_numpy(dtype=float)
    ema9 = df["EMA_9"].to_numpy(dtype=float)
    ema21 = df["EMA_21"].to_numpy(dtype=float)
    rsi = df["RSI_14"].to_numpy(dtype=float)
    bb_lower = df["BB_LOWER"].to_numpy(dtype=float)
    bb_mid = df["BB_MID"].to_numpy(dtype=float)
    bb_upper = df["BB_UPPER"].to_numpy(dtype=float)
    vol_ratio = df["VOL_RATIO"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)

    n = len(df)
    entry = np.zeros(n, dtype=bool)
    exit_sig = np.zeros(n, dtype=bool)

    if strategy == "bollinger_mean_reversion":
        entry = (close <= bb_lower * 1.002) & (rsi < 40.0)
        exit_sig = (close >= bb_upper) | ((close >= bb_mid) & (rsi > 56.0))
    elif strategy == "momentum_breakout":
        prev_20_high = pd.Series(high).shift(1).rolling(window=20, min_periods=20).max().to_numpy(dtype=float)
        prev_10_low = pd.Series(df["low"]).shift(1).rolling(window=10, min_periods=10).min().to_numpy(dtype=float)
        entry = (close > prev_20_high) & (vol_ratio >= 1.1)
        exit_sig = (close < prev_10_low) | (ema9 < ema21)
    else:
        prev_ema9 = np.roll(ema9, 1)
        prev_ema21 = np.roll(ema21, 1)
        prev_ema9[0] = ema9[0]
        prev_ema21[0] = ema21[0]
        crossover_up = (ema9 > ema21) & (prev_ema9 <= prev_ema21)
        pullback_resume = (ema9 > ema21) & (close > ema21) & (rsi >= 48.0) & (rsi <= 66.0)
        entry = crossover_up | pullback_resume
        exit_sig = ema9 < ema21

    return entry, exit_sig


def _guardrail_allows_entry(
    df: pd.DataFrame,
    idx: int,
    strategy: str,
) -> tuple[bool, float]:
    """Evaluate historical regime guardrail conditions at bar idx without look-ahead."""
    atr_pct = float(df["ATR_PCT"].iloc[idx])
    bb_width = float(df["BB_WIDTH"].iloc[idx])
    ker = float(df["KER_14"].iloc[idx])
    atr = float(df["ATR_14"].iloc[idx])
    close = float(df["close"].iloc[idx])
    ema200 = float(df["EMA_200"].iloc[idx])
    bar_range = float(df["high"].iloc[idx] - df["low"].iloc[idx])

    start_hist = max(0, idx - 80)
    atr_median = float(df["ATR_PCT"].iloc[start_hist : idx + 1].median())
    vol_expansion = (atr_pct / atr_median) if atr_median > 0 else 1.0
    range_vs_atr = (bar_range / atr) if atr > 0 else 1.0

    if range_vs_atr >= 2.5 or vol_expansion >= 2.0 or atr_pct >= 4.5:
        return False, 0.0

    if ker < 0.22 and (vol_expansion >= 1.15 or bb_width >= 0.045):
        return False, 0.0

    if strategy in ("trend_ema_crossover", "momentum_breakout") and close < ema200 * 0.995:
        return False, 0.0

    if vol_expansion >= 1.35 or ker < 0.28:
        return True, 0.5

    return True, 1.0


def run_ab_backtest(
    df: pd.DataFrame,
    strategy: str = "trend_ema_crossover",
    capital_usdt: float = 10000.0,
    risk_per_trade_pct: float = 1.0,
    timeframe: str = "1h",
    fee_bps: float = 10.0,
    guardrail_fn: Callable[[pd.DataFrame, int, str], tuple[bool, float]] | None = None,
) -> dict[str, Any]:
    """Run apples-to-apples Mode A (no regime filter) vs Mode B (regime guardrail) with identical ATR sizing."""
    if strategy not in STRATEGY_META:
        strategy = "trend_ema_crossover"

    effective_guardrail = guardrail_fn or _guardrail_allows_entry

    n = len(df)
    warmup = min(50, max(10, n // 5))
    fee_rate = fee_bps / 10000.0

    entry_sig, exit_sig = _generate_signals(df, strategy)

    close_arr = df["close"].to_numpy(dtype=float)
    high_arr = df["high"].to_numpy(dtype=float)
    low_arr = df["low"].to_numpy(dtype=float)
    atr_arr = df["ATR_14"].to_numpy(dtype=float)
    ts_list = df["datetime"].dt.strftime("%Y-%m-%d %H:%M").tolist()
    if "timestamp" in df.columns:
        ts_sec_all = [int(t // 1000) for t in df["timestamp"].tolist()]
    else:
        ts_sec_all = [int(ts.timestamp()) for ts in df["datetime"].tolist()]

    # --- MODE A: No regime filter (IDENTICAL ATR risk sizing + 2x ATR Stop / 3R Target) ---
    eq_a = capital_usdt
    in_pos_a = False
    entry_px_a = 0.0
    notional_a = 0.0
    stop_a = 0.0
    tp_a = 0.0
    trades_a: list[float] = []
    raw_trade_outcomes: dict[int, float] = {}
    current_entry_idx_a = -1
    curve_a: list[float] = []

    # --- MODE B: With Regime Guardrail (IDENTICAL sizing & stops, differing ONLY by regime filter) ---
    eq_b = capital_usdt
    in_pos_b = False
    entry_px_b = 0.0
    notional_b = 0.0
    stop_b = 0.0
    tp_b = 0.0
    trades_b: list[float] = []
    curve_b: list[float] = []

    blocked_signals = 0
    blocked_entry_indices: list[int] = []

    for i in range(warmup, n):
        c = close_arr[i]
        h = high_arr[i]
        lo = low_arr[i]

        # Update Mode A
        if in_pos_a:
            exit_now = False
            exit_px = c
            if lo <= stop_a:
                exit_now = True
                exit_px = stop_a
            elif h >= tp_a:
                exit_now = True
                exit_px = tp_a
            elif exit_sig[i]:
                exit_now = True
                exit_px = c

            if exit_now:
                gross_pnl = notional_a * ((exit_px - entry_px_a) / entry_px_a)
                fees = notional_a * 2.0 * fee_rate
                net_pnl = gross_pnl - fees
                pnl_pct_on_eq = (net_pnl / eq_a * 100.0) if eq_a > 0 else 0.0
                eq_a = max(eq_a + net_pnl, 1.0)
                trades_a.append(pnl_pct_on_eq)
                raw_trade_outcomes[current_entry_idx_a] = pnl_pct_on_eq
                in_pos_a = False
        elif entry_sig[i] and i < n - 1:
            atr_val = max(atr_arr[i], c * 0.001)
            stop_dist = 2.0 * atr_val
            stop_dist_pct = stop_dist / c
            risk_budget = eq_a * (risk_per_trade_pct / 100.0)
            notional_a = min(risk_budget / stop_dist_pct, eq_a)
            entry_px_a = c
            stop_a = entry_px_a - stop_dist
            tp_a = entry_px_a + 3.0 * stop_dist
            current_entry_idx_a = i
            in_pos_a = True

        if in_pos_a:
            unrealized_a = notional_a * ((c - entry_px_a) / entry_px_a - fee_rate)
            mark_eq_a = max(eq_a + unrealized_a, 1.0)
        else:
            mark_eq_a = eq_a
        curve_a.append(round(mark_eq_a, 2))

        # Update Mode B
        if in_pos_b:
            exit_now_b = False
            exit_px_b = c
            if lo <= stop_b:
                exit_now_b = True
                exit_px_b = stop_b
            elif h >= tp_b:
                exit_now_b = True
                exit_px_b = tp_b
            elif exit_sig[i]:
                exit_now_b = True
                exit_px_b = c

            if exit_now_b:
                gross_pnl = notional_b * ((exit_px_b - entry_px_b) / entry_px_b)
                fees = notional_b * 2.0 * fee_rate
                net_pnl = gross_pnl - fees
                pnl_pct_on_eq = (net_pnl / eq_b * 100.0) if eq_b > 0 else 0.0
                eq_b = max(eq_b + net_pnl, 1.0)
                trades_b.append(pnl_pct_on_eq)
                in_pos_b = False
        elif entry_sig[i] and i < n - 1:
            allowed, mult = effective_guardrail(df, i, strategy)
            if allowed and mult > 0:
                atr_val = max(atr_arr[i], c * 0.001)
                stop_dist = 2.0 * atr_val
                stop_dist_pct = stop_dist / c
                risk_budget = eq_b * (risk_per_trade_pct / 100.0) * mult
                notional_b = min(risk_budget / stop_dist_pct, eq_b * mult)
                entry_px_b = c
                stop_b = entry_px_b - stop_dist
                tp_b = entry_px_b + 3.0 * stop_dist
                in_pos_b = True
            else:
                blocked_signals += 1
                blocked_entry_indices.append(i)

        if in_pos_b:
            unrealized = notional_b * ((c - entry_px_b) / entry_px_b - fee_rate)
            mark_eq_b = max(eq_b + unrealized, 1.0)
        else:
            mark_eq_b = eq_b
        curve_b.append(round(mark_eq_b, 2))

    # Close any open position on final bar identically for both modes
    if in_pos_a:
        gross_pnl = notional_a * ((close_arr[-1] - entry_px_a) / entry_px_a)
        net_pnl = gross_pnl - notional_a * 2.0 * fee_rate
        pnl_pct_on_eq = (net_pnl / eq_a * 100.0) if eq_a > 0 else 0.0
        eq_a = max(eq_a + net_pnl, 1.0)
        trades_a.append(pnl_pct_on_eq)
        raw_trade_outcomes[current_entry_idx_a] = pnl_pct_on_eq
        curve_a[-1] = round(eq_a, 2)

    if in_pos_b:
        gross_pnl = notional_b * ((close_arr[-1] - entry_px_b) / entry_px_b)
        net_pnl = gross_pnl - notional_b * 2.0 * fee_rate
        pnl_pct_on_eq = (net_pnl / eq_b * 100.0) if eq_b > 0 else 0.0
        eq_b = max(eq_b + net_pnl, 1.0)
        trades_b.append(pnl_pct_on_eq)
        curve_b[-1] = round(eq_b, 2)

    blocked_losing_trades = sum(
        1 for idx in blocked_entry_indices if raw_trade_outcomes.get(idx, 0.0) < 0.0
    )

    metrics_a = _compute_metrics(curve_a, trades_a, capital_usdt, timeframe)
    metrics_b = _compute_metrics(curve_b, trades_b, capital_usdt, timeframe)

    drawdown_saved_pct = round(
        max(0.0, metrics_a["max_drawdown_pct"] - metrics_b["max_drawdown_pct"]), 2
    )

    labels = ts_list[warmup:]
    ts_secs = ts_sec_all[warmup:]
    total_pts = len(labels)
    stride = max(1, total_pts // 120)
    sampled_labels = labels[::stride]
    sampled_ts = ts_secs[::stride]
    sampled_a = curve_a[::stride]
    sampled_b = curve_b[::stride]
    if sampled_labels and sampled_labels[-1] != labels[-1]:
        sampled_labels.append(labels[-1])
        sampled_ts.append(ts_secs[-1])
        sampled_a.append(curve_a[-1])
        sampled_b.append(curve_b[-1])

    return {
        "strategy": strategy,
        "strategy_name": STRATEGY_META[strategy]["name"],
        "strategy_description": STRATEGY_META[strategy]["description"],
        "methodology_note": (
            "Comparação justa: ambos os modos operam com o mesmo risco por trade, "
            "stop 2×ATR e alvo 3R; a única variável testada é o filtro de regime."
        ),
        "bars_evaluated": n - warmup,
        "fee_bps_per_side": fee_bps,
        "mode_a_raw": metrics_a,
        "mode_b_guarded": {
            **metrics_b,
            "blocked_bad_trades": blocked_signals,
            "blocked_signals": blocked_signals,
            "blocked_losing_trades": blocked_losing_trades,
            "drawdown_saved_pct": drawdown_saved_pct,
        },
        "chart": {
            "labels": sampled_labels,
            "timestamps": sampled_ts,
            "equity_curve_raw": sampled_a,
            "equity_curve_guarded": sampled_b,
        },
    }
