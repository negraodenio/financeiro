"""CLI demo runner for Scudex (CCXT + Deterministic Committee + Fair A/B Backtest)."""

from __future__ import annotations

import argparse
import json
import sys

from agents import run_committee
from backtester import run_ab_backtest
from market_data import MarketDataUnavailableError, fetch_market_snapshot


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Scudex — CLI Demo")
    parser.add_argument("--symbol", default="BTC/USDT", help="Par de negociação (ex: BTC/USDT)")
    parser.add_argument("--timeframe", default="1h", help="Timeframe (15m, 1h, 4h, 1d)")
    parser.add_argument("--exchange", default="binance", help="Corretora CCXT (binance, bybit, okx, kraken)")
    parser.add_argument("--strategy", default="trend_ema_crossover", help="Estratégia de backtest")
    parser.add_argument("--capital", type=float, default=10000.0, help="Capital em USDT")
    parser.add_argument("--risk", type=float, default=1.0, help="Risco por trade (%%)")
    args = parser.parse_args()

    print("=" * 72)
    print("  SCUDEX — MOTOR DETERMINÍSTICO & BACKTEST A/B (NON-CUSTODIAL)")
    print("=" * 72)

    try:
        snapshot = fetch_market_snapshot(
            symbol=args.symbol,
            timeframe=args.timeframe,
            exchange_id=args.exchange,
        )
    except MarketDataUnavailableError as exc:
        print(f"ERRO: {exc}")
        print("Dica: defina ALLOW_SYNTHETIC_FALLBACK=true apenas para testes locais offline.")
        sys.exit(2)

    committee = run_committee(
        snapshot=snapshot,
        capital_usdt=args.capital,
        risk_per_trade_pct=args.risk,
    )
    df = snapshot.pop("df")
    backtest = run_ab_backtest(
        df=df,
        strategy=args.strategy,
        capital_usdt=args.capital,
        risk_per_trade_pct=args.risk,
        timeframe=args.timeframe,
    )

    mode = "LIVE CCXT" if snapshot["is_live"] else "SIMULACAO OFFLINE"
    print(f"Fonte de Dados : {mode} ({snapshot['served_exchange']})")
    if snapshot["fallback_reason"]:
        print(f"Aviso          : {snapshot['fallback_reason']}")
    print(
        f"Mercado        : {snapshot['symbol']} | TF: {snapshot['timeframe']} | "
        f"Preco: ${snapshot['last_price']:,.2f} ({snapshot['change_24h_pct']:+.2f}%)"
    )
    ind = snapshot["latest_indicators"]
    micro = snapshot["microstructure"]
    print(
        f"Indicadores    : RSI={ind['RSI_14']} | ATR%={ind['ATR_PCT']}% | "
        f"KER={ind['KER_14']} | Spread={micro['bid_ask_spread_bps']} bps | "
        f"Imbalance={micro['orderbook_imbalance']*100:+.1f}%"
    )
    print("-" * 72)
    print("LEITURA DO COMITE DETERMINÍSTICO:")
    for ag in committee["agents"]:
        print(f"  [{ag['signal']}] {ag['agent']} (Score: {ag['score']:+.1f}, Conf: {ag['confidence']}%)")
        for r in ag["reasons"]:
            print(f"      - {r}")
    print("-" * 72)
    print(f"VEREDITO CRO   : {committee['verdict']} | Regime: {committee['regime']} | Score: {committee['combined_score']:+.1f}")
    print(f"Motor          : {committee['engine_name']}")
    print(f"PARECER        : {committee['executive_summary']}")
    plan = committee["execution_plan"]
    print(
        f"REFERÊNCIA     : Entrada=${plan['entry_price']} | Stop=${plan['stop_loss']} | "
        f"TP1=${plan['take_profit_1']} | TP2=${plan['take_profit_2']} | "
        f"Lote=${plan['position_size_usdt']:,.2f} | Risco Max=${plan['max_loss_usdt']:,.2f}"
    )
    print("-" * 72)
    a = backtest["mode_a_raw"]
    b = backtest["mode_b_guarded"]
    print(f"BACKTEST A/B ({backtest['strategy_name']} | {backtest['bars_evaluated']} candles):")
    print(f"  Nota           : {backtest['methodology_note']}")
    print(
        f"  Modo A (Sem Filtro)     -> Retorno: {a['total_return_pct']:+.2f}% | "
        f"MaxDD: -{a['max_drawdown_pct']:.2f}% | Sharpe: {a['sharpe_ratio']} | "
        f"WinRate: {a['win_rate_pct']}% | Trades: {a['total_trades']}"
    )
    print(
        f"  Modo B (Com Disjuntor)  -> Retorno: {b['total_return_pct']:+.2f}% | "
        f"MaxDD: -{b['max_drawdown_pct']:.2f}% | Sharpe: {b['sharpe_ratio']} | "
        f"WinRate: {b['win_rate_pct']}% | Trades: {b['total_trades']}"
    )
    print(
        f"  Comparativo             -> Sinais Bloqueados: {b['blocked_bad_trades']} "
        f"({b['blocked_losing_trades']} perdedores evitados) | Drawdown Evitado: {b['drawdown_saved_pct']:.2f}%"
    )
    print("-" * 72)
    print("PAYLOAD EDUCACIONAL NON-CUSTODIAL:")
    print(json.dumps(committee["ccxt_order_payload"], indent=2, ensure_ascii=False))
    print("=" * 72)


if __name__ == "__main__":
    main()
