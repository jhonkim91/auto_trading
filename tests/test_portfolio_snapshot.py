import gc
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from autotrader import Settings as GuiSettings
from autotrader import KISApi as GuiKISApi
from autotrader import RiskManager as GuiRiskManager
from autotrader import Trader as GuiTrader
from autotrader import TradeJournal
from autotrader import DEFAULT_CONFIG
from autotrader import build_portfolio_snapshot as build_gui_snapshot
from autotrader import _format_balance_lines
from autotrader import _deep_merge
from src.config import Settings as ModuleSettings
from src.kis_api import KISApi
from src.kis_api import KIS_INTERVAL_PAPER, KIS_INTERVAL_REAL, KIS_RATE_PAPER, KIS_RATE_REAL
from src.risk import RiskManager
from src.trader import Trader
from src.trader import TradeJournal as ModuleTradeJournal
from src.trader import build_portfolio_snapshot as build_module_snapshot


DOMESTIC_BALANCE = {
    "cash": 1_000_000,
    "total_eval": 1_250_000,
    "positions": [
        {
            "code": "005930",
            "name": "삼성전자",
            "qty": 10,
            "avg_price": 70_000,
            "cur_price": 75_000,
            "pnl_amt": 50_000,
            "pnl_rate": 7.14,
        }
    ],
}

OVERSEAS_BALANCE = {
    "cash": 25.5,
    "positions": [
        {
            "code": "QQQ",
            "name": "QQQ",
            "qty": 2,
            "avg_price": 500.0,
            "cur_price": 510.0,
            "pnl_amt": 20.0,
            "pnl_rate": 2.0,
        }
    ],
}


class PortfolioSnapshotTests(unittest.TestCase):
    def _assert_combined_snapshot(self, snapshot):
        self.assertEqual(snapshot["cash_krw"], 1_000_000)
        self.assertEqual(snapshot["cash_usd"], 25.5)
        self.assertEqual(snapshot["total_eval_krw"], 1_250_000)
        self.assertEqual(snapshot["total_eval_usd"], 1_045.5)
        self.assertEqual(snapshot["pnl_krw"], 50_000)
        self.assertEqual(snapshot["pnl_usd"], 20.0)
        self.assertEqual(len(snapshot["positions"]), 2)
        self.assertEqual(snapshot["positions"][0]["market_label"], "국내")
        self.assertEqual(snapshot["positions"][0]["currency"], "KRW")
        self.assertEqual(snapshot["positions"][1]["market_label"], "미국")
        self.assertEqual(snapshot["positions"][1]["currency"], "USD")

    def test_gui_snapshot_keeps_krw_and_usd_separate(self):
        snapshot = build_gui_snapshot(DOMESTIC_BALANCE, OVERSEAS_BALANCE)
        self._assert_combined_snapshot(snapshot)

    def test_module_snapshot_keeps_krw_and_usd_separate(self):
        snapshot = build_module_snapshot(DOMESTIC_BALANCE, OVERSEAS_BALANCE)
        self._assert_combined_snapshot(snapshot)

    def test_balance_formatter_adds_usd_only_when_needed(self):
        self.assertEqual(_format_balance_lines(1000, 0, include_usd=False), "1,000원")
        self.assertEqual(_format_balance_lines(1000, 2.5, include_usd=True), "1,000원\n2.50 USD")

    def test_settings_expose_active_account_key_by_mode(self):
        gui_paper = GuiSettings(
            mode="paper", app_key="k", app_secret="s", account_no="1", account_prod="01",
            telegram_token="t", allowed_chat_ids=["1"], paper_account="11111111-01",
            real_account="22222222-01",
        )
        module_real = ModuleSettings(
            mode="real", app_key="k", app_secret="s", account_no="2", account_prod="01",
            telegram_token="t", allowed_chat_ids=["1"], paper_account="11111111-01",
            real_account="22222222-01",
        )
        self.assertEqual(gui_paper.active_account_key, "KIS_PAPER_ACCOUNT")
        self.assertEqual(module_real.active_account_key, "KIS_REAL_ACCOUNT")

    def test_gui_config_deep_merge_preserves_nested_defaults(self):
        merged = _deep_merge(
            {"strategy": {"buy_threshold": 3, "indicators": {"rsi": {"enabled": True}}}},
            {"strategy": {"buy_threshold": 4}},
        )

        self.assertEqual(merged["strategy"]["buy_threshold"], 4)
        self.assertEqual(merged["strategy"]["indicators"]["rsi"]["enabled"], True)

    def test_autotrader_deep_merge_preserves_indicators(self):
        """config.yaml에 buy_threshold만 있어도 indicators 기본값을 유지한다."""
        import copy

        base = copy.deepcopy(DEFAULT_CONFIG)
        override = {"strategy": {"buy_threshold": 4}}
        merged = _deep_merge(base, override)

        self.assertEqual(merged["strategy"]["buy_threshold"], 4)
        self.assertIn("ma_cross", merged["strategy"]["indicators"])
        self.assertIn("adx", merged["strategy"]["indicators"])

    def test_autotrader_shallow_update_would_lose_indicators(self):
        """얕은 update가 indicators를 잃는 문제를 문서화한다."""
        import copy

        base = copy.deepcopy(DEFAULT_CONFIG)
        base.update({"strategy": {"buy_threshold": 4}})

        self.assertNotIn("indicators", base["strategy"])

    def test_trade_journal_filters_records_by_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(os.path.join(tmp, "trades.db"))
            today = datetime.now().strftime("%Y-%m-%d")
            journal.log("paper", "domestic", "005930", "삼성전자", "buy", 1, 70000, ok=True)
            journal.log("paper", "domestic", "005930", "삼성전자", "sell", 1, 71000, pnl=1000, ok=True)
            journal.log("real", "domestic", "000660", "SK하이닉스", "sell", 1, 120000, pnl=-500, ok=True)
            journal.log("paper", "domestic", "035720", "카카오", "buy", 1, 50000, ok=False)
            journal.log("real", "domestic", "035420", "NAVER", "buy", 1, 200000, ok=False)

            self.assertEqual(len(journal.recent(mode="paper")), 3)
            self.assertEqual(len(journal.recent(mode="real")), 2)
            self.assertEqual(journal.summary(today, today, mode="paper")["realized_pnl"], 1000)
            self.assertEqual(journal.summary(today, today, mode="real")["realized_pnl"], -500)
            self.assertEqual(journal.daily_pnl(days=1, mode="paper")[0][1], 1000)

            self.assertEqual(journal.clear_failed(mode="paper"), 1)
            self.assertEqual(len([r for r in journal.recent(mode="paper") if not r["ok"]]), 0)
            self.assertEqual(len([r for r in journal.recent(mode="real") if not r["ok"]]), 1)
            del journal
            gc.collect()

    def test_kis_rate_policy_uses_conservative_shared_limits(self):
        self.assertEqual(KIS_RATE_PAPER, 3)
        self.assertEqual(KIS_RATE_REAL, 15)
        self.assertEqual(KIS_INTERVAL_PAPER, 0.4)
        self.assertEqual(KIS_INTERVAL_REAL, 0.07)

    def test_risk_manager_applies_trailing_stop_in_profit_only(self):
        risk = RiskManager(
            {
                "stop_loss_pct": 5.0,
                "take_profit_pct": 10.0,
                "trailing_stop_pct": 4.0,
            }
        )
        self.assertEqual(risk.should_exit(100, 94, peak_price=110), "stop_loss")
        self.assertEqual(risk.should_exit(100, 104, peak_price=110), "trailing_stop")
        self.assertEqual(risk.should_exit(100, 111, peak_price=111), "take_profit")
        self.assertEqual(risk.should_exit(100, 98, peak_price=110), "")

    def test_module_risk_state_persists_daily_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "risk_state_paper.json")
            cfg = {"daily_loss_limit_pct": 3.0, "max_drawdown_pct": 15.0}
            risk = RiskManager(cfg, state_path=path)

            self.assertEqual(risk.check_limits(1_000_000), "")
            self.assertEqual(risk.check_limits(960_000), "daily_loss")

            loaded = RiskManager(cfg, state_path=path)
            self.assertEqual(loaded.day_start_equity, 1_000_000)
            self.assertEqual(loaded.peak_equity, 1_000_000)
            self.assertTrue(loaded.halted)

    def test_gui_risk_state_persists_daily_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "risk_state_real.json")
            cfg = {"daily_loss_limit_pct": 3.0, "max_drawdown_pct": 15.0}
            risk = GuiRiskManager(cfg, state_path=path)

            self.assertEqual(risk.check_limits(1_000_000), "")
            self.assertEqual(risk.check_limits(960_000), "일일손실한도")

            loaded = GuiRiskManager(cfg, state_path=path)
            self.assertEqual(loaded.day_start, 1_000_000)
            self.assertEqual(loaded.peak, 1_000_000)
            self.assertTrue(loaded.halted)

    def test_module_risk_state_persists_to_equity_state_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = ModuleTradeJournal(os.path.join(tmp, "trades.db"))
            cfg = {"daily_loss_limit_pct": 3.0, "max_drawdown_pct": 15.0}
            risk = RiskManager(cfg, state_store=journal, mode="paper")

            self.assertEqual(risk.check_limits(1_000_000), "")
            self.assertEqual(risk.check_limits(960_000), "daily_loss")

            loaded = RiskManager(cfg, state_store=journal, mode="paper")
            self.assertEqual(loaded.day_start_equity, 1_000_000)
            self.assertEqual(loaded.peak_equity, 1_000_000)
            self.assertTrue(loaded.halted)

    def test_gui_risk_state_persists_to_equity_state_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = TradeJournal(os.path.join(tmp, "trades.db"))
            cfg = {"daily_loss_limit_pct": 3.0, "max_drawdown_pct": 15.0}
            risk = GuiRiskManager(cfg, state_store=journal, mode="real")

            self.assertEqual(risk.check_limits(1_000_000), "")
            self.assertEqual(risk.check_limits(960_000), "일일손실한도")

            loaded = GuiRiskManager(cfg, state_store=journal, mode="real")
            self.assertEqual(loaded.day_start, 1_000_000)
            self.assertEqual(loaded.peak, 1_000_000)
            self.assertTrue(loaded.halted)

    def test_safe_overseas_balance_merges_configured_exchanges_without_cash_duplication(self):
        class FakeApi:
            def overseas_balance(self, exchange):
                balances = {
                    "NAS": {
                        "cash": 100,
                        "positions": [{"code": "QQQ", "qty": 1, "cur_price": 510, "exchange": "NAS"}],
                    },
                    "NYS": {
                        "cash": 100,
                        "positions": [{"code": "BABA", "qty": 2, "cur_price": 80, "exchange": "NYS"}],
                    },
                    "AMS": {"cash": 100, "positions": []},
                }
                return balances[exchange]

        trader = Trader.__new__(Trader)
        trader.s = SimpleNamespace(
            universe={"overseas": [{"symbol": "QQQ", "exchange": "NAS"}, {"symbol": "BABA", "exchange": "NYS"}]}
        )
        trader.api = FakeApi()

        balance = trader.safe_overseas_balance()

        self.assertEqual(balance["cash"], 100)
        self.assertEqual(balance["total_eval"], 770)
        self.assertEqual([(p["exchange"], p["code"]) for p in balance["positions"]], [("NAS", "QQQ"), ("NYS", "BABA")])

    def test_gui_safe_overseas_balance_merges_all_default_exchanges(self):
        class FakeApi:
            def overseas_balance(self, exchange):
                balances = {
                    "NAS": {
                        "cash": 100,
                        "positions": [{"code": "QQQ", "qty": 1, "cur_price": 510}],
                    },
                    "NYS": {
                        "cash": 120,
                        "positions": [{"code": "BABA", "qty": 2, "cur_price": 80}],
                    },
                    "AMS": {"cash": 110, "positions": []},
                }
                return balances[exchange]

        trader = GuiTrader.__new__(GuiTrader)
        trader.api = FakeApi()

        balance = trader.safe_overseas_balance()

        self.assertEqual(balance["cash"], 120)
        self.assertEqual(balance["total_eval"], 790)
        self.assertEqual(
            [(position["exchange"], position["code"]) for position in balance["positions"]],
            [("NAS", "QQQ"), ("NYS", "BABA")],
        )

    def test_liquidate_all_sells_domestic_and_overseas_positions(self):
        class FakeApi:
            def __init__(self):
                self.calls = []

            def domestic_balance(self):
                return {"positions": [{"code": "005930", "qty": 3}]}

            def overseas_balance(self, exchange):
                if exchange == "NAS":
                    return {"cash": 0, "positions": [{"code": "QQQ", "qty": 1, "cur_price": 510, "exchange": "NAS"}]}
                if exchange == "NYS":
                    return {"cash": 0, "positions": [{"code": "BABA", "qty": 2, "cur_price": 80, "exchange": "NYS"}]}
                return {"cash": 0, "positions": []}

            def domestic_order(self, code, qty, side, price=0):
                self.calls.append(("domestic", code, qty, side, price))
                return {"ok": True}

            def overseas_order(self, code, qty, side, price=0, exchange="NAS"):
                self.calls.append(("overseas", exchange, code, qty, side, price))
                return {"ok": True}

        api = FakeApi()
        trader = Trader.__new__(Trader)
        trader.s = SimpleNamespace(
            universe={"overseas": [{"symbol": "QQQ", "exchange": "NAS"}, {"symbol": "BABA", "exchange": "NYS"}]}
        )
        trader.api = api

        results = trader.liquidate_all()

        self.assertEqual(results, [("005930", True), ("NAS:QQQ", True), ("NYS:BABA", True)])
        self.assertEqual(
            api.calls,
            [
                ("domestic", "005930", 3, "sell", 0),
                ("overseas", "NAS", "QQQ", 1, "sell", 510),
                ("overseas", "NYS", "BABA", 2, "sell", 80),
            ],
        )

    def test_kis_overseas_buyable_parses_amount_and_quantity(self):
        calls = []
        api = KISApi.__new__(KISApi)
        api.s = SimpleNamespace(is_paper=True, account_no="12345678", account_prod="01")

        def fake_get(path, tr_id, params):
            calls.append((path, tr_id, params))
            return {"output": {"ord_psbl_frcr_amt": "1200.50", "ovrs_ord_psbl_qty": "3"}}

        api._get = fake_get

        result = api.overseas_buyable("QQQ", 510, "NAS")

        self.assertEqual(result, {"amount": 1200.50, "qty": 3})
        self.assertEqual(calls[0][0], "/uapi/overseas-stock/v1/trading/inquire-psamount")
        self.assertEqual(calls[0][1], "VTTS3007R")
        self.assertEqual(calls[0][2]["ITEM_CD"], "QQQ")

    def test_gui_overseas_balance_uses_extended_cash_fallback(self):
        api = GuiKISApi.__new__(GuiKISApi)
        api.s = SimpleNamespace(is_paper=True, account_no="12345678", account_prod="01")
        api._get = lambda *args, **kwargs: {"output1": [], "output2": {"ovrs_tot_dncl_amt": "123.45"}}

        result = api.overseas_balance("NAS")

        self.assertEqual(result["cash"], 123.45)

    def test_kis_domestic_volume_rank_parses_candidates(self):
        calls = []
        api = KISApi.__new__(KISApi)

        def fake_get(path, tr_id, params):
            calls.append((path, tr_id, params))
            return {
                "output": [
                    {
                        "mksc_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "75000",
                        "prdy_ctrt": "1.5",
                        "acml_vol": "1000000",
                    }
                ]
            }

        api._get = fake_get

        result = api.domestic_volume_rank("kospi", 10)

        self.assertEqual(result[0]["code"], "005930")
        self.assertEqual(result[0]["price"], 75000)
        self.assertEqual(calls[0][0], "/uapi/domestic-stock/v1/quotations/volume-rank")
        self.assertEqual(calls[0][1], "FHPST01710000")
        self.assertEqual(calls[0][2]["FID_INPUT_ISCD"], "0001")

    def test_kis_overseas_search_parses_candidates(self):
        calls = []
        api = KISApi.__new__(KISApi)

        def fake_get(path, tr_id, params):
            calls.append((path, tr_id, params))
            return {"output2": [{"symb": "AAPL", "name": "Apple", "last": "190.5", "rate": "2.1"}]}

        api._get = fake_get

        result = api.overseas_search("NAS", 5, 1000, 30)

        self.assertEqual(result[0]["code"], "AAPL")
        self.assertEqual(result[0]["price"], 190.5)
        self.assertEqual(calls[0][0], "/uapi/overseas-price/v1/quotations/inquire-search")
        self.assertEqual(calls[0][1], "HHDFS76410000")
        self.assertEqual(calls[0][2]["CO_YN_PRICECUR"], "1")

    def test_trader_screener_filters_and_sorts_domestic_candidates(self):
        class FakeApi:
            def domestic_volume_rank(self, market, count):
                return [
                    {"code": "LOW", "name": "low", "price": 1000, "change_rate": 100},
                    {"code": "A", "name": "A", "price": 10000, "change_rate": 1},
                    {"code": "B", "name": "B", "price": 12000, "change_rate": 5},
                ]

        trader = Trader.__new__(Trader)
        trader.s = SimpleNamespace(
            universe={"domestic": ["005930"], "overseas": []},
            screener={
                "enabled": True,
                "market": "all",
                "pool_size": 30,
                "top_k": 2,
                "min_price": 2000,
                "max_price": 50000,
                "momentum_rank": True,
            },
        )
        trader.api = FakeApi()

        result = trader.screen_candidates(cash=20000)

        self.assertEqual([item["code"] for item in result], ["B", "A"])

    def test_trader_screener_falls_back_to_overseas_pool(self):
        class FakeApi:
            def overseas_search(self, exchange, min_price, max_price, count):
                raise RuntimeError("network disabled")

        trader = Trader.__new__(Trader)
        trader.s = SimpleNamespace(
            universe={"domestic": [], "overseas": []},
            screener={
                "enabled": True,
                "overseas_market": "NAS",
                "overseas_pool": ["AAPL", "MSFT", "NVDA"],
                "top_k": 2,
            },
        )
        trader.api = FakeApi()

        with patch("src.trader.log.warning"):
            result = trader.screen_overseas()

        self.assertEqual(result, [{"symbol": "AAPL", "exchange": "NAS"}, {"symbol": "MSFT", "exchange": "NAS"}])

    def test_scan_once_uses_screener_candidates(self):
        processed = []
        trader = Trader.__new__(Trader)
        trader.s = SimpleNamespace(universe={"domestic": [], "overseas": []}, screener={"enabled": True})
        trader.risk = SimpleNamespace(check_limits=lambda equity: "", halted=False)
        trader.safe_domestic_balance = lambda: {"cash": 100000, "total_eval": 100000, "positions": []}
        trader.screen_candidates = lambda cash: [{"code": "A"}, {"code": "B"}]
        trader._process_domestic = lambda code, balance: processed.append(code)
        trader._overseas_items = lambda: []
        trader.safe_overseas_balance = lambda: {"cash": 0, "total_eval": 0, "positions": []}
        trader.screen_overseas = lambda cash=0: []

        trader.scan_once()

        self.assertEqual(processed, ["A", "B"])

    def test_cooldown_blocks_reentry_for_configured_minutes(self):
        trader = Trader.__new__(Trader)
        trader.s = SimpleNamespace(risk={"cooldown_min": 30})
        trader._cooldown = {"domestic:005930": datetime.now().timestamp()}

        self.assertTrue(trader._in_cooldown("domestic:005930"))

    def test_domestic_buy_respects_cooldown_key(self):
        class FakeApi:
            def __init__(self):
                self.orders = []

            def domestic_daily(self, code, count=120):
                return [{"open": 100, "high": 110, "low": 90, "close": 105, "volume": 1000}] * 40

            def domestic_price(self, code):
                return {"price": 100, "open": 95}

            def domestic_order(self, code, qty, side, price=0):
                self.orders.append((code, qty, side, price))
                return {"ok": True}

        fake_api = FakeApi()
        trader = Trader.__new__(Trader)
        trader.s = SimpleNamespace(risk={"cooldown_min": 30})
        trader.api = fake_api
        trader.strategy_domestic = SimpleNamespace(evaluate=lambda *args, **kwargs: SimpleNamespace(action="buy", reasons=["test"]))
        trader.risk = SimpleNamespace(can_open_new=lambda current_positions: True,
                                      position_size=lambda cash, price, candles: 1)
        trader.auto_enabled = True
        trader.notify = lambda message: None
        trader.last_signals = {}
        trader._peak = {}
        trader._cooldown = {"domestic:005930": datetime.now().timestamp()}

        trader._process_domestic("005930", {"cash": 100000, "total_eval": 100000, "positions": []})

        self.assertEqual(fake_api.orders, [])

    def test_overseas_buy_uses_buyable_quantity_cap(self):
        class FakeApi:
            def __init__(self):
                self.orders = []

            def overseas_daily(self, symbol, exchange, count=120):
                return [{"open": 100, "high": 110, "low": 90, "close": 105, "volume": 1000}] * 40

            def overseas_price(self, symbol, exchange):
                return {"price": 100, "open": 95}

            def overseas_buyable(self, symbol, price, exchange):
                return {"amount": 5000, "qty": 3}

            def overseas_order(self, symbol, qty, side, price=0, exchange="NAS"):
                self.orders.append((symbol, qty, side, price, exchange))
                return {"ok": True}

        class FakeRisk:
            def __init__(self):
                self.cash_seen = None

            def can_open_new(self, current_positions):
                return True

            def position_size(self, cash, price, candles=None):
                self.cash_seen = cash
                return 10

            def should_exit(self, avg_price, cur_price, peak_price=None):
                return ""

        api = FakeApi()
        risk = FakeRisk()
        trader = Trader.__new__(Trader)
        trader.s = SimpleNamespace(screener={"overseas_use_buyable": True})
        trader.api = api
        trader.risk = risk
        trader.strategy_overseas = SimpleNamespace(
            evaluate=lambda candles, current_price=None, current_open=None: SimpleNamespace(
                action="buy",
                reasons=["test"],
            )
        )
        trader.journal = SimpleNamespace(log=lambda *args, **kwargs: None)
        trader.auto_enabled = True
        trader.last_signals = {}
        trader._peak = {}
        trader.notify = lambda msg: None

        with patch("src.trader.log.info"):
            trader._process_overseas({"symbol": "QQQ", "exchange": "NAS"}, {"cash": 100, "positions": []})

        self.assertEqual(risk.cash_seen, 5000)
        self.assertEqual(api.orders, [("QQQ", 3, "buy", 100, "NAS")])

    def test_module_daily_report_uses_mode_filtered_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = ModuleTradeJournal(os.path.join(tmp, "trades.db"))
            journal.log("paper", "domestic", "005930", "삼성전자", "sell", 1, 71000, "test", 1000, True)
            journal.log("real", "domestic", "000660", "SK하이닉스", "sell", 1, 120000, "test", -500, True)
            trader = Trader.__new__(Trader)
            trader.s = SimpleNamespace(mode="paper")
            trader.journal = journal

            report = trader.daily_report()

            self.assertIn("오늘 리포트 (paper)", report)
            self.assertIn("실현손익 +1,000", report)

    def test_market_specific_strategy_settings_are_exposed(self):
        module_settings = ModuleSettings(
            mode="paper",
            app_key="k",
            app_secret="s",
            account_no="1",
            account_prod="01",
            telegram_token="t",
            allowed_chat_ids=["1"],
            strategy={"buy_threshold": 3},
            strategy_domestic={"buy_threshold": 2},
            strategy_overseas={"buy_threshold": 4},
        )

        self.assertEqual(module_settings.strategy_domestic["buy_threshold"], 2)
        self.assertEqual(module_settings.strategy_overseas["buy_threshold"], 4)


if __name__ == "__main__":
    unittest.main()
