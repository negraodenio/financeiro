"""Market data and microstructure ingestion via CCXT with multi-exchange fallback and TTL cache."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import threading
import time
from typing import Any

import ccxt
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SUPPORTED_EXCHANGES: tuple[str, ...] = ("binance", "bybit", "okx", "kraken")

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

FX_SNAPSHOT_LIMIT: int = 250

SYNTHETIC_ANCHOR_PRICES: dict[str, float] = {
    "BTC/USDT": 92500.0,
    "ETH/USDT": 3180.0,
    "SOL/USDT": 215.0,
    "BNB/USDT": 625.0,
    "EUR/USDT": 1.08,
    "USDT/BRL": 5.85,
    "BTC/BRL": 372000.0,
    "ETH/BRL": 18600.0,
    "SOL/BRL": 1240.0,
}

_CACHE_LOCK = threading.Lock()
_SNAPSHOT_CACHE: dict[tuple[str, str, str, int, bool], tuple[float, dict[str, Any]]] = {}


class MarketDataUnavailableError(RuntimeError):
    """Raised when live exchange data cannot be retrieved and synthetic fallback is disabled."""


def clear_market_cache() -> None:
    """Clear the in-memory OHLCV + orderbook snapshot cache."""
    with _CACHE_LOCK:
        _SNAPSHOT_CACHE.clear()


def _clone_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated copy so callers calling snapshot.pop('df') never mutate cached entries."""
    cloned = dict(snapshot)
    if "df" in snapshot and snapshot["df"] is not None:
        cloned["df"] = snapshot["df"].copy()
    if isinstance(snapshot.get("latest_indicators"), dict):
        cloned["latest_indicators"] = dict(snapshot["latest_indicators"])
    if isinstance(snapshot.get("microstructure"), dict):
        cloned["microstructure"] = dict(snapshot["microstructure"])
    return cloned


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute trend, momentum, volatility, volume, and efficiency indicators."""
    out = df.copy()
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    volume = out["volume"].astype(float)

    for span in (9, 21, 50, 200):
        out[f"EMA_{span}"] = close.ewm(span=span, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss > 0, np.where(avg_gain > 0, 100.0, 50.0))
    out["RSI_14"] = rsi.fillna(50.0)

    ema_12 = close.ewm(span=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, adjust=False).mean()
    out["MACD"] = ema_12 - ema_26
    out["MACD_SIGNAL"] = out["MACD"].ewm(span=9, adjust=False).mean()
    out["MACD_HIST"] = out["MACD"] - out["MACD_SIGNAL"]

    bb_mid = close.rolling(window=20, min_periods=1).mean()
    bb_std = close.rolling(window=20, min_periods=1).std(ddof=0).fillna(0.0)
    out["BB_MID"] = bb_mid
    out["BB_UPPER"] = bb_mid + 2.0 * bb_std
    out["BB_LOWER"] = bb_mid - 2.0 * bb_std
    out["BB_WIDTH"] = ((out["BB_UPPER"] - out["BB_LOWER"]) / bb_mid.replace(0.0, np.nan)).fillna(0.0)

    prev_close = close.shift(1).fillna(close)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["ATR_14"] = tr.ewm(alpha=1.0 / 14.0, min_periods=1, adjust=False).mean()
    out["ATR_PCT"] = ((out["ATR_14"] / close.replace(0.0, np.nan)) * 100.0).fillna(0.0)

    vol_sma = volume.rolling(window=20, min_periods=1).mean()
    out["VOL_RATIO"] = (volume / vol_sma.replace(0.0, np.nan)).fillna(1.0)

    net_change = (close - close.shift(14)).abs()
    volatility_sum = close.diff().abs().rolling(window=14, min_periods=1).sum()
    ker = (net_change / volatility_sum.replace(0.0, np.nan)).fillna(0.0).clip(0.0, 1.0)
    out["KER_14"] = ker

    return out


def _build_exchange(exchange_id: str) -> ccxt.Exchange:
    """Instantiate a public CCXT exchange client with conservative timeouts."""
    exchange_cls = getattr(ccxt, exchange_id)
    return exchange_cls(
        {
            "enableRateLimit": True,
            "timeout": 8000,
        }
    )


def _extract_orderbook_metrics(orderbook: dict[str, Any], fallback_price: float) -> dict[str, Any]:
    """Compute top-20 orderbook spread (bps) and bid/ask volume imbalance."""
    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []

    if not bids or not asks:
        return {
            "best_bid": fallback_price * 0.9998,
            "best_ask": fallback_price * 1.0002,
            "mid_price": fallback_price,
            "bid_ask_spread_bps": 4.0,
            "orderbook_imbalance": 0.0,
            "bid_ratio": 0.5,
            "bid_volume_top20": 0.0,
            "ask_volume_top20": 0.0,
        }

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid_price = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / mid_price * 10000.0) if mid_price > 0 else 0.0

    bid_vol = float(sum(float(level[1]) for level in bids[:20]))
    ask_vol = float(sum(float(level[1]) for level in asks[:20]))
    total_vol = bid_vol + ask_vol

    imbalance = ((bid_vol - ask_vol) / total_vol) if total_vol > 0 else 0.0
    bid_ratio = (bid_vol / total_vol) if total_vol > 0 else 0.5

    return {
        "best_bid": round(best_bid, 6),
        "best_ask": round(best_ask, 6),
        "mid_price": round(mid_price, 6),
        "bid_ask_spread_bps": round(spread_bps, 3),
        "orderbook_imbalance": round(imbalance, 4),
        "bid_ratio": round(bid_ratio, 4),
        "bid_volume_top20": round(bid_vol, 4),
        "ask_volume_top20": round(ask_vol, 4),
    }


def _generate_synthetic_snapshot(
    symbol: str,
    timeframe: str,
    limit: int,
    fallback_reason: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Generate synthetic OHLCV + orderbook data when explicit dev fallback is enabled."""
    seed = abs(hash(f"{symbol}:{timeframe}")) % (2**32)
    rng = np.random.default_rng(seed)

    base_price = SYNTHETIC_ANCHOR_PRICES.get(symbol.upper(), 100.0)
    is_fx_stable = symbol.upper().startswith(("USDT/", "USDC/", "EUR/"))
    step_sec = TIMEFRAME_SECONDS.get(timeframe, 3600)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    start_ts = now_ts - limit * step_sec

    closes = [base_price]
    vols = []
    vol_scale = 0.22 if is_fx_stable else 1.0
    wick_sigma = 0.0008 if is_fx_stable else 0.0035
    for i in range(limit):
        phase = i / max(limit, 1)
        if phase < 0.30:
            drift, sigma = 0.0012 * vol_scale, 0.0055 * vol_scale
        elif phase < 0.55:
            drift, sigma = -0.0002 * vol_scale, 0.0110 * vol_scale
        elif phase < 0.85:
            drift, sigma = 0.0014 * vol_scale, 0.0060 * vol_scale
        else:
            drift, sigma = 0.0005 * vol_scale, 0.0075 * vol_scale

        ret = drift + rng.normal(0.0, sigma)
        next_close = max(closes[-1] * (1.0 + ret), base_price * 0.2)
        closes.append(next_close)
        vols.append(float(rng.lognormal(mean=5.5, sigma=0.45)))

    rows = []
    for i in range(limit):
        ts_ms = (start_ts + (i + 1) * step_sec) * 1000
        open_px = closes[i]
        close_px = closes[i + 1]
        wick_up = abs(rng.normal(0.0, wick_sigma)) * max(open_px, close_px)
        wick_dn = abs(rng.normal(0.0, wick_sigma)) * max(open_px, close_px)
        high_px = max(open_px, close_px) + wick_up
        low_px = max(min(open_px, close_px) - wick_dn, 0.01)
        rows.append([ts_ms, open_px, high_px, low_px, close_px, vols[i]])

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

    last_close = float(df["close"].iloc[-1])
    spread = last_close * 0.00025
    synthetic_ob = {
        "bids": [[last_close - spread * (0.5 + 0.1 * k), 1.8 + float(rng.uniform(0.2, 2.5))] for k in range(20)],
        "asks": [[last_close + spread * (0.5 + 0.1 * k), 1.4 + float(rng.uniform(0.2, 2.2))] for k in range(20)],
    }
    micro = _extract_orderbook_metrics(synthetic_ob, last_close)
    micro["fallback_reason"] = fallback_reason
    return df, micro


def fetch_market_snapshot(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    exchange_id: str = "binance",
    limit: int = 400,
    allow_synthetic: bool | None = None,
) -> dict[str, Any]:
    """Fetch OHLCV + orderbook via CCXT with TTL cache and strict live-data safety."""
    normalized_symbol = symbol.strip().upper()
    quote_currency = normalized_symbol.split("/")[1] if "/" in normalized_symbol else "USDT"
    currency_symbol = "R$" if quote_currency == "BRL" else "$"
    requested_exchange = exchange_id.strip().lower()
    if requested_exchange not in SUPPORTED_EXCHANGES:
        requested_exchange = "binance"

    if allow_synthetic is None:
        effective_allow_synthetic = os.getenv("ALLOW_SYNTHETIC_FALLBACK", "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    else:
        effective_allow_synthetic = bool(allow_synthetic)

    try:
        cache_ttl = max(0.0, float(os.getenv("MARKET_CACHE_TTL_SEC", "45.0")))
    except ValueError:
        cache_ttl = 45.0

    cache_key = (normalized_symbol, timeframe, requested_exchange, int(limit), effective_allow_synthetic)
    if cache_ttl > 0:
        now_mono = time.monotonic()
        with _CACHE_LOCK:
            cached = _SNAPSHOT_CACHE.get(cache_key)
            if cached and (now_mono - cached[0]) < cache_ttl:
                return _clone_snapshot(cached[1])

    fallback_order = ("binance", "okx", "bybit", "kraken") if quote_currency == "BRL" else SUPPORTED_EXCHANGES
    ordered_exchanges = [requested_exchange] + [
        ex for ex in fallback_order if ex != requested_exchange
    ]

    errors: list[str] = []
    df_raw: pd.DataFrame | None = None
    micro: dict[str, Any] = {}
    served_exchange = requested_exchange
    is_live = False
    fallback_reason = ""

    for ex_id in ordered_exchanges:
        try:
            client = _build_exchange(ex_id)
            ohlcv = client.fetch_ohlcv(normalized_symbol, timeframe=timeframe, limit=limit)
            if not ohlcv or len(ohlcv) < 50:
                raise ValueError(f"Insufficient candles returned ({len(ohlcv) if ohlcv else 0})")

            df_raw = pd.DataFrame(
                ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df_raw["datetime"] = pd.to_datetime(df_raw["timestamp"], unit="ms", utc=True)
            last_close = float(df_raw["close"].iloc[-1])

            try:
                orderbook = client.fetch_order_book(normalized_symbol, limit=20)
            except Exception as ob_exc:
                logger.warning("Orderbook fetch failed on %s: %s", ex_id, ob_exc)
                orderbook = {}

            micro = _extract_orderbook_metrics(orderbook, last_close)
            served_exchange = ex_id
            is_live = True
            if ex_id != requested_exchange:
                fallback_reason = (
                    f"Fallback automático: {requested_exchange} indisponível ({'; '.join(errors)}). "
                    f"Dados ao vivo carregados via {ex_id}."
                )
            break
        except Exception as exc:
            err_msg = f"{ex_id}: {type(exc).__name__}"
            errors.append(err_msg)
            logger.warning("CCXT fetch failed for %s (%s): %s", ex_id, normalized_symbol, exc)

    if df_raw is None:
        if not effective_allow_synthetic:
            raise MarketDataUnavailableError(
                "Dados de mercado ao vivo temporariamente indisponíveis nas corretoras. "
                "Tente novamente em instantes."
            )

        fallback_reason = (
            "Modo offline/simulação ativado (ALLOW_SYNTHETIC_FALLBACK=true; corretoras inacessíveis: "
            + ", ".join(errors)
            + ")."
        )
        df_raw, micro = _generate_synthetic_snapshot(
            normalized_symbol,
            timeframe,
            limit,
            fallback_reason=fallback_reason,
        )
        served_exchange = "synthetic_simulator"
        is_live = False

    df_ind = compute_indicators(df_raw)
    last_row = df_ind.iloc[-1]

    step_sec = TIMEFRAME_SECONDS.get(timeframe, 3600)
    bars_24h = max(1, min(len(df_ind) - 1, int(86400 // step_sec)))
    ref_close = float(df_ind["close"].iloc[-1 - bars_24h])
    last_close = float(last_row["close"])
    change_24h_pct = ((last_close - ref_close) / ref_close * 100.0) if ref_close > 0 else 0.0

    latest_metrics = {
        "close": round(last_close, 6),
        "open": round(float(last_row["open"]), 6),
        "high": round(float(last_row["high"]), 6),
        "low": round(float(last_row["low"]), 6),
        "volume": round(float(last_row["volume"]), 4),
        "EMA_9": round(float(last_row["EMA_9"]), 6),
        "EMA_21": round(float(last_row["EMA_21"]), 6),
        "EMA_50": round(float(last_row["EMA_50"]), 6),
        "EMA_200": round(float(last_row["EMA_200"]), 6),
        "RSI_14": round(float(last_row["RSI_14"]), 2),
        "MACD": round(float(last_row["MACD"]), 6),
        "MACD_SIGNAL": round(float(last_row["MACD_SIGNAL"]), 6),
        "MACD_HIST": round(float(last_row["MACD_HIST"]), 6),
        "BB_UPPER": round(float(last_row["BB_UPPER"]), 6),
        "BB_MID": round(float(last_row["BB_MID"]), 6),
        "BB_LOWER": round(float(last_row["BB_LOWER"]), 6),
        "BB_WIDTH": round(float(last_row["BB_WIDTH"]), 5),
        "ATR_14": round(float(last_row["ATR_14"]), 6),
        "ATR_PCT": round(float(last_row["ATR_PCT"]), 3),
        "VOL_RATIO": round(float(last_row["VOL_RATIO"]), 3),
        "KER_14": round(float(last_row["KER_14"]), 3),
    }

    raw_obs = df_ind["datetime"].iloc[-1]
    observed_at = raw_obs.isoformat() if hasattr(raw_obs, "isoformat") else str(raw_obs)

    snapshot: dict[str, Any] = {
        "symbol": normalized_symbol,
        "quote_currency": quote_currency,
        "currency_symbol": currency_symbol,
        "timeframe": timeframe,
        "requested_exchange": requested_exchange,
        "served_exchange": served_exchange,
        "is_live": is_live,
        "data_source": f"ccxt:{served_exchange}" if is_live else "synthetic_fallback",
        "quote_type": "crypto_spot" if is_live else "synthetic_fallback",
        "validity_status": "LIVE" if is_live else "SIMULATED",
        "cache_ttl_sec": cache_ttl,
        "fallback_reason": fallback_reason,
        "observed_at": observed_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "candles_count": len(df_ind),
        "last_price": round(last_close, 6),
        "change_24h_pct": round(change_24h_pct, 2),
        "latest_indicators": latest_metrics,
        "microstructure": micro,
        "df": df_ind,
    }

    if cache_ttl > 0 and is_live:
        with _CACHE_LOCK:
            _SNAPSHOT_CACHE[cache_key] = (time.monotonic(), _clone_snapshot(snapshot))

    return snapshot
