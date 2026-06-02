import gc
import os
import tempfile
import unittest
from datetime import datetime

from autotrader import Settings as GuiSettings
from autotrader import TradeJournal
from autotrader import build_portfolio_snapshot as build_gui_snapshot
from autotrader import _format_balance_lines
from src.config import Settings as ModuleSettings
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


if __name__ == "__main__":
    unittest.main()
