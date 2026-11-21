"""Offline production hardening tests for Scudex."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import agents
import app as app_module
import backtester
import crisis_advisor
import db
import europe_bridge
import leak_xray
import market_data
import telegram_bot


def _sample_user_ctx(plan_tier: str = "TRIAL", uses_remaining: int = 3) -> dict:
    return {
        "user": {"id": "user-uuid-1", "email": "teste@exemplo.com", "full_name": "Teste"},
        "profile": {
            "id": "user-uuid-1",
            "email": "teste@exemplo.com",
            "full_name": "Teste",
            "plan_tier": plan_tier,
            "trial_uses_remaining": uses_remaining,
            "telegram_chat_id": "999888777",
        },
        "license_info": {
            "license_key": "teste@exemplo.com",
            "owner": "Teste",
            "tier": plan_tier,
            "plan_name": plan_tier,
            "uses_remaining": -1 if plan_tier != "TRIAL" else uses_remaining,
            "unlimited": plan_tier != "TRIAL",
            "active": True,
            "auth_source": "supabase",
        },
    }


def _sample_snapshot(symbol: str = "USDT/BRL", is_live: bool = True) -> dict:
    df_raw, micro = market_data._generate_synthetic_snapshot(
        symbol=symbol,
        timeframe="4h",
        limit=250,
        fallback_reason="" if is_live else "synthetic_test",
    )
    df_ind = market_data.compute_indicators(df_raw)
    last_row = df_ind.iloc[-1]
    last_close = float(last_row["close"])
    return {
        "symbol": symbol,
        "quote_currency": symbol.split("/")[1],
        "currency_symbol": "R$" if symbol.endswith("/BRL") else "$",
        "timeframe": "4h",
        "requested_exchange": "binance",
        "served_exchange": "binance" if is_live else "synthetic_simulator",
        "is_live": is_live,
        "data_source": "ccxt:binance" if is_live else "synthetic_fallback",
        "fallback_reason": "",
        "fetched_at": "2026-11-20T12:00:00+00:00",
        "candles_count": len(df_ind),
        "last_price": round(last_close, 6),
        "change_24h_pct": 0.42,
        "latest_indicators": {
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
        },
        "microstructure": micro,
        "df": df_ind,
    }


class ProductionHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        app_module.clear_rate_limits()
        market_data.clear_market_cache()
        europe_bridge.clear_europe_bridge_cache()
        self.client = TestClient(app_module.app)

    def test_01_analyze_requires_valid_bearer(self) -> None:
        r_no_auth = self.client.post("/api/analyze", json={"symbol": "USDT/BRL"})
        self.assertEqual(r_no_auth.status_code, 401)

        with patch("app.supabase_get_user_from_jwt", return_value=None):
            r_bad_auth = self.client.post(
                "/api/analyze",
                headers={"Authorization": "Bearer invalid-token"},
                json={"symbol": "USDT/BRL"},
            )
            self.assertEqual(r_bad_auth.status_code, 401)

    def test_02_telegram_send_requires_paid_account(self) -> None:
        r_no_auth = self.client.post("/api/telegram/send", json={"text": "Teste"})
        self.assertEqual(r_no_auth.status_code, 401)

        with patch("app.supabase_get_user_from_jwt", return_value=_sample_user_ctx(plan_tier="TRIAL")):
            r_trial = self.client.post(
                "/api/telegram/send",
                headers={"Authorization": "Bearer valid-trial"},
                json={"text": "Teste"},
            )
            self.assertEqual(r_trial.status_code, 403)

    def test_03_removed_backdoor_routes_return_404(self) -> None:
        for path in ("/api/admin/licenses", "/api/auth/verify", "/api/rescue/log"):
            res = self.client.post(path, json={})
            self.assertEqual(res.status_code, 404, msg=f"Expected 404 for {path}")

        res_get = self.client.get("/api/rescue/summary")
        self.assertEqual(res_get.status_code, 404)

    def test_04_market_unavailable_raises_503_never_fake_trap(self) -> None:
        class BrokenExchange:
            def fetch_ohlcv(self, *_args, **_kwargs):
                raise RuntimeError("Exchange offline")

        with patch.dict(os.environ, {"ALLOW_SYNTHETIC_FALLBACK": "false"}):
            with patch("market_data._build_exchange", return_value=BrokenExchange()):
                with self.assertRaises(market_data.MarketDataUnavailableError):
                    market_data.fetch_market_snapshot("BTC/BRL", "1h", "binance")

                res = self.client.post(
                    "/api/public/trap-check",
                    json={"symbol": "BTC/BRL", "claimed_side": "BUY", "timeframe": "1h"},
                )
                self.assertEqual(res.status_code, 503)
                self.assertNotIn("ALERTA_CILADA", res.text)

    def test_05_ab_backtest_uses_identical_sizing_when_filter_allows_all(self) -> None:
        snap = _sample_snapshot("BTC/USDT", is_live=True)
        df = snap["df"]
        bt = backtester.run_ab_backtest(
            df=df,
            strategy="trend_ema_crossover",
            capital_usdt=10000.0,
            risk_per_trade_pct=1.0,
            timeframe="4h",
            guardrail_fn=lambda _df, _idx, _st: (True, 1.0),
        )
        self.assertEqual(bt["chart"]["equity_curve_raw"], bt["chart"]["equity_curve_guarded"])
        for metric_key in (
            "final_equity_usdt",
            "total_return_pct",
            "max_drawdown_pct",
            "sharpe_ratio",
            "win_rate_pct",
            "profit_factor",
            "total_trades",
        ):
            self.assertEqual(
                bt["mode_a_raw"][metric_key],
                bt["mode_b_guarded"][metric_key],
                msg=f"Mismatch on {metric_key}",
            )
        self.assertEqual(bt["mode_b_guarded"]["drawdown_saved_pct"], 0.0)
        self.assertIn("methodology_note", bt)

    def test_06_fx_treasury_has_no_invented_timing_gain(self) -> None:
        snap = _sample_snapshot("USDT/BRL", is_live=True)
        fx = crisis_advisor.build_fx_treasury_signal(snap, monthly_usd_volume=2500.0, profile="EARN_USD")
        for forbidden_key in ("timing_edge_brl", "timing_pct", "total_monthly_gain_brl", "annual_impact_brl"):
            self.assertNotIn(forbidden_key, fx)
        self.assertIn("spread_saved_monthly_brl", fx)
        self.assertIn("spread_saved_annual_brl", fx)
        self.assertTrue(fx["is_live"])

    def test_07_europe_bridge_candles_honest_routes_and_no_cvm_tags(self) -> None:
        eur_snap = _sample_snapshot("EUR/USDT", is_live=True)
        usd_snap = _sample_snapshot("USDT/BRL", is_live=True)

        def fake_fetch(symbol: str, **_kwargs):
            return _sample_snapshot(symbol, is_live=True)

        class FakeResp:
            def __init__(self, status_code: int, payload: object):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        def fake_httpx_get(url: str, **_kwargs):
            if "bcb.gov.br" in url:
                return FakeResp(200, [{"valor": "11.75"}])
            # Simulate Yahoo unreachable for B3 assets to verify honest unavailability
            return FakeResp(503, {})

        with patch("europe_bridge.fetch_market_snapshot", side_effect=fake_fetch):
            with patch("europe_bridge.httpx.get", side_effect=fake_httpx_get):
                bridge = europe_bridge.build_europe_brazil_bridge(amount_eur=1500.0, investment_type="SELIC")

        candles = bridge["fx"]["candles"]
        self.assertGreater(len(candles), 10)
        times = [c["time"] for c in candles]
        self.assertEqual(times, sorted(set(times)))

        routes = bridge["routes"]
        self.assertEqual(routes["remitter_fee_pct"], 1.1)
        self.assertEqual(routes["bank_fee_pct"], 3.8)
        self.assertGreater(routes["brl_saved_vs_bank"], 0.0)
        self.assertNotIn("USDT→PIX", bridge["whatsapp_share_text"])
        self.assertIn("Remessadora regulada", bridge["whatsapp_share_text"])

        for asset in bridge["brazil_assets_in_eur"]:
            self.assertNotIn("Desconto Duplo", asset["tag"])
            self.assertFalse(asset["is_live"])
            self.assertIsNone(asset["price_brl"])
            self.assertEqual(asset["tag"], "Cotação indisponível")

    def test_08_smart_dca_compares_honestly_against_fixed_dca(self) -> None:
        snap = _sample_snapshot("USDT/BRL", is_live=True)
        dca = crisis_advisor.simulate_smart_dca_brl(snap["df"], monthly_contribution_brl=500.0, timeframe="4h")
        for forbidden_key in ("avg_price_fomo", "extra_units_vs_fomo_pct", "savings_vs_fomo_brl"):
            self.assertNotIn(forbidden_key, dca)
        for required_key in (
            "avg_price_dca",
            "avg_price_smart_dca",
            "final_value_dca_brl",
            "final_value_smart_dca_brl",
            "cash_reserve_left_brl",
            "final_value_diff_pct",
        ):
            self.assertIn(required_key, dca)

    def test_09_committee_is_strictly_deterministic_zero_http(self) -> None:
        snap = _sample_snapshot("BTC/USDT", is_live=True)
        with patch("httpx.post", side_effect=AssertionError("Unexpected HTTP POST in committee")):
            with patch("httpx.get", side_effect=AssertionError("Unexpected HTTP GET in committee")):
                res = agents.run_committee(snap, capital_usdt=10000.0, risk_per_trade_pct=1.0)
        self.assertEqual(res["engine_name"], "Scudex Quant v2 (Motor Determinístico)")

    def test_10_rate_limiter_triggers_429(self) -> None:
        app_module.clear_rate_limits()
        with patch("app.get_recent_audit_log", return_value=[]):
            for _ in range(20):
                ok_res = self.client.get("/api/public/audit-log")
                self.assertEqual(ok_res.status_code, 200)
            blocked = self.client.get("/api/public/audit-log")
            self.assertEqual(blocked.status_code, 429)

    def test_11_security_headers_present_on_all_pages(self) -> None:
        for path in ("/", "/app", "/termos", "/privacidade", "/api/health"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, msg=f"Unexpected status for {path}")
            self.assertEqual(res.headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(res.headers.get("x-frame-options"), "DENY")
            self.assertEqual(res.headers.get("referrer-policy"), "strict-origin-when-cross-origin")
            self.assertIn("geolocation=()", res.headers.get("permissions-policy", ""))

    def test_12_no_forbidden_llm_or_demo_strings_exposed(self) -> None:
        forbidden = (
            "openrouter",
            "openai",
            "gpt-4",
            "gemini",
            "chatgpt",
            "llm",
            "admin-quant-2026",
            "qg-pro-demo-2026",
            "qg-escudo-br-2026",
            "qg-prop-vip-2026",
            "qg-free-trial",
        )
        surfaces: list[tuple[str, str]] = [
            ("landing.html", (ROOT_DIR / "static" / "landing.html").read_text(encoding="utf-8")),
            ("index.html", (ROOT_DIR / "static" / "index.html").read_text(encoding="utf-8")),
        ]
        for route in ("/", "/app", "/termos", "/privacidade", "/api/health"):
            surfaces.append((route, self.client.get(route).text))

        for name, body in surfaces:
            low = body.lower()
            for term in forbidden:
                self.assertNotIn(term, low, msg=f"Forbidden string '{term}' found in {name}")

    def test_13_snapshot_cache_clones_df_and_quota_not_consumed_on_503(self) -> None:
        class LiveFakeExchange:
            calls = 0

            def fetch_ohlcv(self, symbol, timeframe="1h", limit=400):
                LiveFakeExchange.calls += 1
                df_syn, _ = market_data._generate_synthetic_snapshot(symbol, timeframe, limit, "")
                return df_syn[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()

            def fetch_order_book(self, symbol, limit=20):
                return {"bids": [[5.80, 1000.0]], "asks": [[5.81, 900.0]]}

        market_data.clear_market_cache()
        with patch.dict(os.environ, {"MARKET_CACHE_TTL_SEC": "45"}):
            with patch("market_data._build_exchange", return_value=LiveFakeExchange()):
                s1 = market_data.fetch_market_snapshot("USDT/BRL", "4h", "binance", 100)
                df1 = s1.pop("df")
                self.assertEqual(len(df1), 100)
                s2 = market_data.fetch_market_snapshot("USDT/BRL", "4h", "binance", 100)
                self.assertIn("df", s2)
                self.assertEqual(LiveFakeExchange.calls, 1)

        with patch("app.supabase_get_user_from_jwt", return_value=_sample_user_ctx("TRIAL", 3)):
            with patch("app.fetch_market_snapshot", side_effect=market_data.MarketDataUnavailableError("offline")):
                with patch("app.decrement_user_quota_atomic") as dec_mock:
                    res = self.client.post(
                        "/api/analyze",
                        headers={"Authorization": "Bearer valid"},
                        json={"symbol": "USDT/BRL"},
                    )
                    self.assertEqual(res.status_code, 503)
                    dec_mock.assert_not_called()

    def test_14_alerts_require_auth_and_crud_works_with_mock_db(self) -> None:
        self.assertEqual(self.client.get("/api/alerts").status_code, 401)
        self.assertEqual(
            self.client.post("/api/alerts", json={"symbol": "EUR/BRL", "target_rate": 6.4}).status_code,
            401,
        )

        with patch("app.supabase_get_user_from_jwt", return_value=_sample_user_ctx("TRIAL", 3)):
            with patch(
                "app.create_user_alert",
                return_value={"id": 42, "symbol": "EUR/BRL", "direction": "ABOVE", "target_rate": 6.45, "channel": "telegram"},
            ):
                r_create = self.client.post(
                    "/api/alerts",
                    headers={"Authorization": "Bearer valid"},
                    json={"symbol": "EUR/BRL", "direction": "ABOVE", "target_rate": 6.45, "channel": "telegram"},
                )
                self.assertEqual(r_create.status_code, 200)
                self.assertEqual(r_create.json()["alert"]["id"], 42)

    def test_15_favicon_and_new_lp_assets_served(self) -> None:
        for asset_route in (
            "/favicon.ico",
            "/static/assets/favicon.svg",
            "/static/assets/scudex-logo.png",
            "/static/assets/scudex-wordmark.png",
            "/static/assets/hero-bridge.png",
            "/static/assets/alerts-radar.png",
            "/static/assets/brazil-eu-skyline.png",
        ):
            res = self.client.get(asset_route)
            self.assertEqual(res.status_code, 200, msg=f"Expected 200 for {asset_route}")
            self.assertGreater(len(res.content), 100)

        lp_html = (ROOT_DIR / "static" / "landing.html").read_text(encoding="utf-8")
        idx_html = (ROOT_DIR / "static" / "index.html").read_text(encoding="utf-8")
        for doc_name, doc in (("landing.html", lp_html), ("index.html", idx_html)):
            self.assertIn("favicon.svg", doc, msg=f"{doc_name} missing favicon.svg")
            for legacy_asset in (
                "hero-editorial.png",
                "europe-bridge-editorial.png",
                "trap-check-editorial.png",
            ):
                self.assertNotIn(legacy_asset, doc, msg=f"Legacy image {legacy_asset} still in {doc_name}")

        # Verify complete showcase, 3 transparent tiers, fixed currency labels, Scudex rebrand, and zero Cofre Anti-Bet
        self.assertIn('id="recursos"', lp_html)
        self.assertIn("Scudex Pro", lp_html)
        self.assertIn("Pro Desk Quant", lp_html)
        self.assertIn("https://scudex.app/", lp_html)
        self.assertIn("€ Euro (EUR)", lp_html)
        self.assertIn("R$ Real (BRL)", lp_html)
        self.assertNotIn("Escudo Real", lp_html)
        self.assertNotIn("escudoreal.com", lp_html)
        self.assertNotIn("Mora na Europa e envia dinheiro", lp_html)
        self.assertNotIn("Ponte Europa Pro", lp_html)
        self.assertNotIn("alertas ilimitados", lp_html.lower())
        self.assertNotIn("cofre anti-bet", lp_html.lower())
        self.assertNotIn("rescue_deposits", lp_html.lower())

    def test_16_telegram_bot_inline_menu_callbacks_and_alerts(self) -> None:
        telegram_bot.clear_command_cooldowns()
        sent_calls: list[dict] = []

        def fake_send(text: str, bot_token=None, chat_id=None, reply_markup=None):
            sent_calls.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
            return {"sent": True, "mode": "mock", "message_id": len(sent_calls)}

        with patch.dict(os.environ, {"TELEGRAM_CTA_URL": "http://localhost:8000"}):
            with patch("telegram_bot.send_telegram_message", side_effect=fake_send):
                with patch("telegram_bot.answer_callback_query", return_value=True) as ack_mock:
                    # 1. /start e texto livre "oi" enviam teclado inline principal SEM URL localhost invalida
                    telegram_bot.handle_update(
                        {"message": {"chat": {"id": 111}, "text": "/start"}},
                        bot_token="fake-token",
                    )
                    telegram_bot.handle_update(
                        {"message": {"chat": {"id": 111}, "text": "oi"}},
                        bot_token="fake-token",
                    )
                    self.assertEqual(len(sent_calls), 2)
                    for call in sent_calls:
                        kb = call["reply_markup"]["inline_keyboard"]
                        cb_values = [btn.get("callback_data") for row in kb for btn in row]
                        urls = [btn.get("url") for row in kb for btn in row if "url" in btn]
                        self.assertIn("cb:menu_euro", cb_values)
                        self.assertIn("cb:menu_alerts", cb_values)
                        self.assertIn("cb:menu_trap", cb_values)
                        self.assertEqual(urls, [], msg="Localhost URL buttons must not be emitted")

                    # 2. callback_query cb:euro:1500:SELIC
                    sent_calls.clear()
                    telegram_bot.clear_command_cooldowns()
                    fake_bridge = {
                        "whatsapp_share_text": "🇪🇺➡️🇧🇷 *SIMULACAO*\nEUR/BRL: *R$ 6.5000*",
                    }
                    with patch("telegram_bot.build_europe_brazil_bridge", return_value=fake_bridge) as br_mock:
                        telegram_bot.handle_update(
                            {
                                "callback_query": {
                                    "id": "cbq-1",
                                    "data": "cb:euro:1500:SELIC",
                                    "message": {"chat": {"id": 111}},
                                }
                            },
                            bot_token="fake-token",
                        )
                        ack_mock.assert_called_with("cbq-1", "fake-token")
                        br_mock.assert_called_once_with(amount_eur=1500.0, investment_type="SELIC")
                        self.assertIn("cb:menu_alerts", str(sent_calls[-1]["reply_markup"]))

                    # 3. callback_query cb:menu_alerts sem conta vinculada
                    sent_calls.clear()
                    with patch("telegram_bot.get_profile_by_telegram_chat_id", return_value=None):
                        telegram_bot.handle_update(
                            {
                                "callback_query": {
                                    "id": "cbq-2",
                                    "data": "cb:menu_alerts",
                                    "message": {"chat": {"id": 222}},
                                }
                            },
                            bot_token="fake-token",
                        )
                        self.assertIn("222", sent_calls[-1]["text"])
                        self.assertIn("/app#alerts", sent_calls[-1]["text"])

                    # 4. callback_query cb:alert_quick:EUR/BRL:ABOVE:1 com conta vinculada
                    sent_calls.clear()
                    telegram_bot.clear_command_cooldowns()
                    linked_trial = {"id": "user-uuid-1", "email": "u@test.com", "plan_tier": "TRIAL", "trial_uses_remaining": 3}
                    with patch("telegram_bot.get_profile_by_telegram_chat_id", return_value=linked_trial):
                        with patch("telegram_bot.fetch_eur_brl_snapshot", return_value={"is_live": True, "eur_brl_rate": 6.5000}):
                            with patch(
                                "telegram_bot.create_user_alert",
                                return_value={"id": 77, "symbol": "EUR/BRL", "direction": "ABOVE", "target_rate": 6.565},
                            ) as ca_mock:
                                telegram_bot.handle_update(
                                    {
                                        "callback_query": {
                                            "id": "cbq-3",
                                            "data": "cb:alert_quick:EUR/BRL:ABOVE:1",
                                            "message": {"chat": {"id": 333}},
                                        }
                                    },
                                    bot_token="fake-token",
                                )
                                ca_mock.assert_called_once_with(
                                    user_id="user-uuid-1",
                                    plan_tier="TRIAL",
                                    symbol="EUR/BRL",
                                    direction="ABOVE",
                                    target_rate=6.565,
                                    channel="telegram",
                                )
                                self.assertIn("#77", sent_calls[-1]["text"])

                    # 5. Comando /alerta EUR/BRL acima 6.55 com conta vinculada
                    sent_calls.clear()
                    with patch("telegram_bot.get_profile_by_telegram_chat_id", return_value=linked_trial):
                        with patch(
                            "telegram_bot.create_user_alert",
                            return_value={"id": 88, "symbol": "EUR/BRL", "direction": "ABOVE", "target_rate": 6.55},
                        ) as ca_cmd_mock:
                            telegram_bot.handle_update(
                                {"message": {"chat": {"id": 333}, "text": "/alerta EUR/BRL acima 6.55"}},
                                bot_token="fake-token",
                            )
                            ca_cmd_mock.assert_called_once_with(
                                user_id="user-uuid-1",
                                plan_tier="TRIAL",
                                symbol="EUR/BRL",
                                direction="ABOVE",
                                target_rate=6.55,
                                channel="telegram",
                            )
                            self.assertIn("#88", sent_calls[-1]["text"])

                    # 6. callback_query cb:alert_del:42
                    sent_calls.clear()
                    with patch("telegram_bot.get_profile_by_telegram_chat_id", return_value=linked_trial):
                        with patch("telegram_bot.delete_user_alert", return_value=True) as del_mock:
                            with patch("telegram_bot.list_user_alerts", return_value=[]):
                                telegram_bot.handle_update(
                                    {
                                        "callback_query": {
                                            "id": "cbq-4",
                                            "data": "cb:alert_del:42",
                                            "message": {"chat": {"id": 333}},
                                        }
                                    },
                                    bot_token="fake-token",
                                )
                                del_mock.assert_called_once_with("user-uuid-1", 42)
                                self.assertIn("#42 removido", sent_calls[0]["text"])

                    # 7. /analisar e cb:quant:* bloqueados para TRIAL; cb:menu_quant exibe menu
                    sent_calls.clear()
                    telegram_bot.clear_command_cooldowns()
                    with patch("telegram_bot.get_profile_by_telegram_chat_id", return_value=linked_trial):
                        telegram_bot.handle_update(
                            {"message": {"chat": {"id": 333}, "text": "/analisar BTC/USDT"}},
                            bot_token="fake-token",
                        )
                        self.assertIn("exclusivo para assinantes", sent_calls[-1]["text"])

                        telegram_bot.handle_update(
                            {
                                "callback_query": {
                                    "id": "cbq-5",
                                    "data": "cb:menu_quant",
                                    "message": {"chat": {"id": 333}},
                                }
                            },
                            bot_token="fake-token",
                        )
                        self.assertIn("cb:quant:BTC/USDT:1h", str(sent_calls[-1]["reply_markup"]))

                        telegram_bot.handle_update(
                            {
                                "callback_query": {
                                    "id": "cbq-6",
                                    "data": "cb:quant:BTC/USDT:1h",
                                    "message": {"chat": {"id": 333}},
                                }
                            },
                            bot_token="fake-token",
                        )
                        self.assertIn("exclusivo para assinantes", sent_calls[-1]["text"])

    def test_17_financial_integrity_ibov_points_selic_no_hardcode_and_breakeven_assumptions(self) -> None:
        class FakeResp:
            def __init__(self, status_code: int, payload: object):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        def fake_fetch(symbol: str, **_kwargs):
            return _sample_snapshot(symbol, is_live=True)

        def fake_httpx_selic_down(url: str, **_kwargs):
            if "bcb.gov.br" in url:
                return FakeResp(503, {})
            if "finance.yahoo.com" in url and ("%5EBVSP" in url or "^BVSP" in url):
                return FakeResp(
                    200,
                    {
                        "chart": {
                            "result": [
                                {
                                    "meta": {"regularMarketTime": 1795190000},
                                    "indicators": {"quote": [{"close": [125000.0, 130250.4]}]},
                                }
                            ]
                        }
                    },
                )
            if "finance.yahoo.com" in url and "BOVA11.SA" in url:
                return FakeResp(
                    200,
                    {
                        "chart": {
                            "result": [
                                {
                                    "meta": {"regularMarketTime": 1795190000},
                                    "indicators": {"quote": [{"close": [120.0, 126.4]}]},
                                }
                            ]
                        }
                    },
                )
            return FakeResp(503, {})

        europe_bridge.clear_europe_bridge_cache()
        try:
            with patch("europe_bridge.fetch_market_snapshot", side_effect=fake_fetch):
                with patch("europe_bridge.httpx.get", side_effect=fake_httpx_selic_down):
                    bridge = europe_bridge.build_europe_brazil_bridge(amount_eur=1500.0, investment_type="SELIC")

            by_ticker = {a["ticker"]: a for a in bridge["brazil_assets_in_eur"]}
            bvsp = by_ticker["^BVSP"]
            self.assertEqual(bvsp["unit"], "points")
            self.assertEqual(bvsp["price_points"], 130250)
            self.assertIsNone(bvsp["price_brl"])
            self.assertIsNone(bvsp["price_eur"])
            self.assertTrue(bvsp["is_live"])

            bova = by_ticker["BOVA11"]
            self.assertEqual(bova["unit"], "BRL")
            self.assertEqual(bova["price_brl"], 126.4)
            self.assertIsNotNone(bova["price_eur"])
            self.assertGreater(bova["price_eur"], 0)

            ct = bridge["carry_trade"]
            self.assertIsNone(ct["selic_pct"])
            self.assertFalse(ct["available"])
            self.assertIsNone(ct["break_even_eur_brl_12m"])

            fx = bridge["fx"]
            self.assertEqual(fx["eur_brl_rate"], round(fx["eur_usd_rate"] * fx["usd_brl_rate"], 4))
            self.assertTrue(bool(fx.get("observed_at")))
            self.assertTrue(bool(fx.get("fetched_at")))
            self.assertTrue(bool(fx.get("quote_type")))

            def fake_httpx_selic_up(url: str, **_kwargs):
                if "bcb.gov.br" in url:
                    return FakeResp(200, [{"data": "20/11/2026", "valor": "14.25"}])
                return FakeResp(503, {})

            europe_bridge.clear_europe_bridge_cache()
            with patch("europe_bridge.fetch_market_snapshot", side_effect=fake_fetch):
                with patch("europe_bridge.httpx.get", side_effect=fake_httpx_selic_up):
                    bridge_live = europe_bridge.build_europe_brazil_bridge(amount_eur=1500.0, investment_type="SELIC")

            ct_live = bridge_live["carry_trade"]
            self.assertEqual(ct_live["selic_pct"], 14.25)
            self.assertEqual(ct_live["selic_observed_date"], "20/11/2026")
            self.assertTrue(ct_live["available"])
            self.assertLess(ct_live["break_even_round_trip_12m"], ct_live["break_even_eur_brl_12m"])
            for key in ("formula_pure", "formula_round_trip", "ir_tax_pct", "iof_outbound_pct", "fx_risk_warning"):
                self.assertIn(key, ct_live["assumptions"])
        finally:
            europe_bridge.clear_europe_bridge_cache()

    def test_18_audit_log_dedup_ui_integrity_and_telegram_support(self) -> None:
        class FakeResp:
            def __init__(self, status_code: int, payload: object):
                self.status_code = status_code
                self._payload = payload

            def json(self):
                return self._payload

        recent_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        db._LAST_AUDIT_TS.clear()
        try:
            with patch("db._cfg", return_value=("https://mock.supabase.co", "anon-key", "srv-key")):
                with patch("db.httpx.get", return_value=FakeResp(200, [{"created_at": recent_iso}])):
                    with patch("db.httpx.post") as post_mock:
                        saved = db.record_audit_snapshot(
                            symbol="EUR/BRL",
                            rate=6.20,
                            rsi_14=50.0,
                            signal_label="Euro Próximo da Média Recente",
                            source="binance (EUR/USDT × USDT/BRL)",
                            is_live=True,
                        )
                        self.assertFalse(saved)
                        post_mock.assert_not_called()

                sample_row = [
                    {
                        "id": 1,
                        "symbol": "EUR/BRL",
                        "rate": 6.2,
                        "rsi_14": 50.0,
                        "signal_label": "Euro Próximo da Média Recente",
                        "source": "binance (EUR/USDT × USDT/BRL)",
                        "is_live": True,
                        "created_at": recent_iso,
                    }
                ]
                with patch("db.httpx.get", return_value=FakeResp(200, sample_row)):
                    rows = db.get_recent_audit_log(limit=5)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["engine_version"], db.AUDIT_ENGINE_VERSION)
                    self.assertEqual(rows[0]["methodology"], db.AUDIT_METHODOLOGY)
        finally:
            db._LAST_AUDIT_TS.clear()

        lp_html = (ROOT_DIR / "static" / "landing.html").read_text(encoding="utf-8")
        self.assertIn("Compare. Proteja. Decida.", lp_html)
        self.assertIn("Fonte &amp; Motor", lp_html)
        self.assertIn("euBreakEvenAssumptions", lp_html)
        self.assertIn("BOVA11", lp_html)
        self.assertNotIn("EUR/BRL 24/7", lp_html)
        self.assertNotIn("Remessas 24/7", lp_html)

        sent_calls: list[dict] = []

        def fake_send(text: str, bot_token=None, chat_id=None, reply_markup=None):
            sent_calls.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
            return {"sent": True, "mode": "mock", "message_id": 1}

        with patch.dict(os.environ, {"TELEGRAM_CTA_URL": "http://localhost:8000"}):
            with patch("telegram_bot.send_telegram_message", side_effect=fake_send):
                with patch("telegram_bot.answer_callback_query", return_value=True):
                    telegram_bot.handle_update(
                        {
                            "callback_query": {
                                "id": "cbq-support",
                                "data": "cb:menu_support",
                                "message": {"chat": {"id": 444}},
                            }
                        },
                        bot_token="fake-token",
                    )
                    self.assertEqual(len(sent_calls), 1)
                    self.assertIn("Scudex Quant v2", sent_calls[0]["text"])
                    self.assertIn("/termos", sent_calls[0]["text"])

    def test_19_seo_meta_jsonld_robots_and_sitemap(self) -> None:
        r_root = self.client.get("/")
        self.assertEqual(r_root.status_code, 200)
        body = r_root.text
        self.assertIn("https://scudex.app/", body)
        for required_tag in (
            'name="description"',
            'rel="canonical"',
            'hreflang="pt-BR"',
            'hreflang="pt-PT"',
            'property="og:title"',
            'property="og:image"',
            'name="twitter:card"',
            'application/ld+json',
            '"SoftwareApplication"',
            '"FAQPage"',
            '<main>',
            '</main>',
        ):
            self.assertIn(required_tag, body, msg=f"Missing SEO tag/element {required_tag} in /")

        with patch.dict(os.environ, {"SITE_URL": "https://meuescudo.app"}):
            r_override = self.client.get("/")
            self.assertEqual(r_override.status_code, 200)
            self.assertIn("https://meuescudo.app/", r_override.text)

            r_robots = self.client.get("/robots.txt")
            self.assertEqual(r_robots.status_code, 200)
            self.assertIn("text/plain", r_robots.headers.get("content-type", ""))
            self.assertIn("Disallow: /app", r_robots.text)
            self.assertIn("Disallow: /api/", r_robots.text)
            self.assertIn("Sitemap: https://meuescudo.app/sitemap.xml", r_robots.text)

            r_sitemap = self.client.get("/sitemap.xml")
            self.assertEqual(r_sitemap.status_code, 200)
            self.assertIn("application/xml", r_sitemap.headers.get("content-type", ""))
            self.assertIn("<urlset", r_sitemap.text)
            self.assertIn("https://meuescudo.app/", r_sitemap.text)
            self.assertIn("https://meuescudo.app/termos", r_sitemap.text)
            self.assertIn("https://meuescudo.app/privacidade", r_sitemap.text)

        r_app = self.client.get("/app")
        self.assertEqual(r_app.status_code, 200)
        self.assertIn("noindex, nofollow", r_app.text)

    def test_20_leak_xray_parsers_wise_revolut_binance_and_zero_invented_rates(self) -> None:
        leak_xray.clear_leak_xray_cache()
        wise_csv = "\n".join(
            [
                "TransferWise ID,Created on,Source amount (after fees),Source currency,Target amount (after fees),Target currency,Exchange Rate,Total fees,Source fee currency,IOF,IOF currency",
                "TW-1,2026-05-12 10:00:00,1000.00,EUR,5950.00,BRL,5.95,12.00,EUR,40.00,BRL",
            ]
        )
        with patch("leak_xray.fetch_historical_fx_rates", return_value={"2026-05-12": {"BRL": 6.20, "USD": 1.09}}):
            res_wise = leak_xray.analyze_leak_xray(wise_csv, provider="auto", base_currency="EUR", allow_network=False)

        self.assertEqual(res_wise["detected_provider"], "wise")
        self.assertEqual(res_wise["verified_spread_count"], 1)
        self.assertEqual(res_wise["unverified_spread_count"], 0)
        self.assertGreater(res_wise["categories"]["inevitable"], 6.0)
        self.assertGreater(res_wise["total_cost"], 50.0)
        self.assertLessEqual(res_wise["alternative_estimated_cost"], res_wise["total_cost"])

        # Missing ECB historical rate must NEVER fabricate spread
        with patch("leak_xray.fetch_historical_fx_rates", return_value={}):
            res_unverified = leak_xray.analyze_leak_xray(wise_csv, provider="wise", base_currency="EUR", allow_network=False)
        self.assertEqual(res_unverified["verified_spread_count"], 0)
        self.assertEqual(res_unverified["unverified_spread_count"], 1)
        self.assertAlmostEqual(res_unverified["categories"]["reducible"], 12.0, places=2)

        revolut_csv = "\n".join(
            [
                "Type,Started Date,Completed Date,Description,Amount,Fee,Currency,State",
                "EXCHANGE,2026-05-12 11:00:00,2026-05-12 11:00:00,Exchanged to BRL,-500.00,4.00,EUR,COMPLETED",
                "EXCHANGE,2026-05-12 11:00:00,2026-05-12 11:00:00,Exchanged from EUR,2950.00,0.00,BRL,COMPLETED",
                "CARD_PAYMENT,2026-05-13 09:00:00,2026-05-13 09:00:00,Reversed test,-100.00,5.00,EUR,REVERTED",
            ]
        )
        with patch("leak_xray.fetch_historical_fx_rates", return_value={}):
            res_rev = leak_xray.analyze_leak_xray(revolut_csv, provider="auto", base_currency="EUR", allow_network=False)
        self.assertEqual(res_rev["detected_provider"], "revolut")
        self.assertEqual(res_rev["operations_analyzed"], 1)
        self.assertGreaterEqual(res_rev["categories"]["reducible"], 4.0)

        binance_csv = "\n".join(
            [
                "Date(UTC),Pair,Operation,Executed,Total,Fee,Fee Coin",
                "2026-05-10 08:00:00,BTCUSDT,BUY,0.02BTC,1800USDT,1.80USDT,USDT",
                "2026-05-11 08:00:00,,Withdraw,500USDT,500USDT,8.00USDT,USDT",
                "2026-05-12 08:00:00,,Withdraw,300USDT,300USDT,8.00USDT,USDT",
                "2026-05-13 08:00:00,,Withdraw,250USDT,250USDT,8.00USDT,USDT",
            ]
        )
        with patch("leak_xray.fetch_historical_fx_rates", return_value={}):
            res_bin = leak_xray.analyze_leak_xray(binance_csv, provider="auto", base_currency="EUR", allow_network=False)
        self.assertEqual(res_bin["detected_provider"], "binance")
        self.assertGreater(res_bin["categories"]["avoidable"], 0.0)
        self.assertGreater(res_bin["categories"]["reducible"], 0.0)

        app_module.clear_rate_limits()
        with patch("leak_xray.fetch_historical_fx_rates", return_value={}):
            r_sample = self.client.post("/api/public/leak-xray", json={"use_sample": True, "base_currency": "EUR"})
        self.assertEqual(r_sample.status_code, 200)
        sample_data = r_sample.json()
        self.assertTrue(sample_data["is_sample"])
        self.assertGreaterEqual(sample_data["operations_analyzed"], 5)
        self.assertIn("RAM", sample_data["privacy_notice"])

    def test_21_impulse_brake_historical_stats_and_expensive_moment(self) -> None:
        snap = _sample_snapshot("BTC/BRL", is_live=True)
        comm = agents.run_committee(snap, capital_usdt=5000.0, risk_per_trade_pct=1.0)
        df = snap.pop("df")
        brake = crisis_advisor.evaluate_impulse_brake(
            snap,
            comm,
            df=df,
            planned_amount=1500.0,
            claimed_side="BUY",
            source_origin="grupo_whatsapp",
        )
        self.assertIn(brake["brake_level"], ("PAUSA_OBRIGATORIA_10M", "CAUTELA_FRACIONAR", "SEGUIR_COM_STOP"))
        self.assertEqual(brake["pause_seconds"], 600)
        self.assertGreater(brake["estimated_swing_risk_amount"], 0.0)
        self.assertEqual(len(brake["reflection_questions"]), 3)

        app_module.clear_rate_limits()
        with patch("app.fetch_market_snapshot", return_value=_sample_snapshot("BTC/BRL", is_live=True)):
            with patch("app.save_trap_check_db", return_value=True):
                r_brake = self.client.post(
                    "/api/public/impulse-brake",
                    json={"symbol": "BTC/BRL", "planned_amount": 1500.0, "source_origin": "grupo_whatsapp"},
                )
        self.assertEqual(r_brake.status_code, 200)
        self.assertEqual(r_brake.json()["pause_seconds"], 600)

        caro = europe_bridge.evaluate_expensive_moment("EUR/BRL", 6.12, 6.00, 66.0, 0.85)
        self.assertEqual(caro["status"], "MOMENTO_CARO")
        self.assertEqual(caro["suggested_expensive_rate"], round(6.00 * 1.015, 4))

        fav = europe_bridge.evaluate_expensive_moment("EUR/BRL", 5.88, 6.00, 35.0, 0.15)
        self.assertEqual(fav["status"], "MOMENTO_FAVORAVEL")

        neutro = europe_bridge.evaluate_expensive_moment("EUR/BRL", 6.00, 6.00, 50.0, 0.50)
        self.assertEqual(neutro["status"], "MOMENTO_NEUTRO")

    def test_22_telegram_freio_raiox_momentocaro_and_lp_ui(self) -> None:
        telegram_bot.clear_command_cooldowns()
        sent_calls: list[dict] = []

        def fake_send(text: str, bot_token=None, chat_id=None, reply_markup=None):
            sent_calls.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
            return {"sent": True, "mode": "mock", "message_id": len(sent_calls)}

        linked_trial = {"id": "user-uuid-1", "email": "u@test.com", "plan_tier": "TRIAL", "trial_uses_remaining": 3}
        with patch.dict(os.environ, {"TELEGRAM_CTA_URL": "https://scudex.app"}):
            with patch("telegram_bot.send_telegram_message", side_effect=fake_send):
                with patch("telegram_bot.answer_callback_query", return_value=True):
                    telegram_bot.handle_update(
                        {
                            "callback_query": {
                                "id": "cbq-xray",
                                "data": "cb:menu_xray",
                                "message": {"chat": {"id": 555}},
                            }
                        },
                        bot_token="fake-token",
                    )
                    self.assertIn("RAM", sent_calls[-1]["text"])
                    self.assertIn("criptografia", sent_calls[-1]["text"])

                    with patch("telegram_bot.fetch_market_snapshot", return_value=_sample_snapshot("BTC/BRL", is_live=True)):
                        telegram_bot.handle_update(
                            {"message": {"chat": {"id": 555}, "text": "/freio BTC/BRL 1500"}},
                            bot_token="fake-token",
                        )
                    self.assertIn("FREIO DE IMPULSO", sent_calls[-1]["text"])
                    self.assertIn("10 minutos", sent_calls[-1]["text"])

                    fake_fx = {
                        "is_live": True,
                        "eur_brl_rate": 6.12,
                        "sma_20": 6.10,
                        "rsi_14": 54.0,
                        "bb_upper": 6.20,
                        "bb_lower": 6.00,
                    }
                    telegram_bot.clear_command_cooldowns()
                    with patch("telegram_bot.get_profile_by_telegram_chat_id", return_value=linked_trial):
                        with patch("telegram_bot.fetch_eur_brl_snapshot", return_value=fake_fx):
                            with patch(
                                "telegram_bot.create_user_alert",
                                return_value={"id": 91, "symbol": "EUR/BRL", "direction": "ABOVE", "target_rate": 6.1915},
                            ) as ca_mock:
                                telegram_bot.handle_update(
                                    {
                                        "callback_query": {
                                            "id": "cbq-moment",
                                            "data": "cb:alert_moment:EUR/BRL",
                                            "message": {"chat": {"id": 555}},
                                        }
                                    },
                                    bot_token="fake-token",
                                )
                                ca_mock.assert_called_once_with(
                                    user_id="user-uuid-1",
                                    plan_tier="TRIAL",
                                    symbol="EUR/BRL",
                                    direction="ABOVE",
                                    target_rate=round(6.10 * 1.015, 4),
                                    channel="telegram",
                                )
                                self.assertIn("Momento Caro", sent_calls[-1]["text"])

        lp_html = (ROOT_DIR / "static" / "landing.html").read_text(encoding="utf-8")
        for dom_id in ("studioTabXray", "panelXray", "brakeRunBtn", "brakeDiarySummary", "euMomentBar", "fxMomentBar"):
            self.assertIn(dom_id, lp_html)

    def test_23_vercel_deploy_config_and_serverless_endpoints(self) -> None:
        import json

        cfg = json.loads((ROOT_DIR / "vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("regions"), ["gru1"])
        self.assertIn("app.py", cfg.get("functions", {}))

        self.assertEqual((ROOT_DIR / ".python-version").read_text(encoding="utf-8").strip(), "3.14")
        gitignore = (ROOT_DIR / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", gitignore)
        self.assertIn(".venv/", gitignore)

        vercelignore = (ROOT_DIR / ".vercelignore").read_text(encoding="utf-8")
        self.assertIn(".env", vercelignore)
        self.assertIn(".venv", vercelignore)

        self.assertTrue((ROOT_DIR / ".env.example").is_file())
        self.assertIn("leak_xray.py", (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8"))

        payload = {"message": {"chat": {"id": 777}, "text": "/start"}}
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "", "TELEGRAM_BOT_TOKEN": ""}):
            res_unconf = self.client.post("/api/telegram/webhook", json=payload)
            self.assertEqual(res_unconf.status_code, 503)

        with patch.dict(
            os.environ,
            {"TELEGRAM_WEBHOOK_SECRET": "wh-secret-123", "TELEGRAM_BOT_TOKEN": "bot-tok-1"},
        ):
            res_missing = self.client.post("/api/telegram/webhook", json=payload)
            self.assertEqual(res_missing.status_code, 403)

            res_wrong = self.client.post(
                "/api/telegram/webhook",
                json=payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            )
            self.assertEqual(res_wrong.status_code, 403)

            with patch("app.handle_update") as hu_mock:
                res_ok = self.client.post(
                    "/api/telegram/webhook",
                    json=payload,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wh-secret-123"},
                )
                self.assertEqual(res_ok.status_code, 200)
                self.assertEqual(res_ok.json(), {"ok": True})
                hu_mock.assert_called_once_with(payload, bot_token="bot-tok-1")

        with patch.dict(os.environ, {"CRON_SECRET": ""}):
            res_cron_unconf = self.client.get("/api/cron/check-alerts")
            self.assertEqual(res_cron_unconf.status_code, 503)

        with patch.dict(os.environ, {"CRON_SECRET": "cron-sec-999"}):
            res_cron_missing = self.client.get("/api/cron/check-alerts")
            self.assertEqual(res_cron_missing.status_code, 401)

            res_cron_wrong = self.client.get(
                "/api/cron/check-alerts",
                headers={"Authorization": "Bearer wrong-token"},
            )
            self.assertEqual(res_cron_wrong.status_code, 401)

            expected_summary = {"checked": 2, "triggered": 1, "errors": 0}
            with patch("app.check_and_dispatch_price_alerts", return_value=expected_summary) as cr_mock:
                res_cron_ok = self.client.get(
                    "/api/cron/check-alerts",
                    headers={"Authorization": "Bearer cron-sec-999"},
                )
                self.assertEqual(res_cron_ok.status_code, 200)
                self.assertEqual(res_cron_ok.json(), expected_summary)
                cr_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
