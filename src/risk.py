"""리스크 관리 - 포지션 사이징, 손절/익절, 일손실·MDD 한도.

보고서 3항: 리스크 관리가 전략 자체보다 생존에 더 결정적.
- ATR 기반 또는 계좌대비 고정위험비율 포지션 사이징
- 진입과 동시에 손절 라인 설정
- 일일 손실 한도 / 최대낙폭(MDD) 한도 모니터링
"""
import json
import os
from datetime import datetime

from . import indicators as ind


class RiskManager:
    def __init__(self, risk_cfg: dict, state_path: str = None, state_store=None, mode: str = None):
        self.cfg = risk_cfg or {}
        self.state_path = state_path
        self.state_store = state_store
        self.mode = mode
        self.state_date = None
        self.day_start_equity = None     # 당일 시작 자산
        self.peak_equity = None          # 최고 자산(MDD 계산용)
        self.halted = False              # 당일 매매 중단 여부
        self._load_state()

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _load_state(self):
        data = None
        if self.state_store and self.mode:
            try:
                data = self.state_store.load_equity_state(self.mode)
            except Exception:  # noqa
                data = None
        if not data and self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:  # noqa
                data = None
        if not data:
            return
        try:
            self.state_date = data.get("date")
            self.day_start_equity = data.get("day_start_equity")
            self.peak_equity = data.get("peak_equity")
            self.halted = bool(data.get("halted", False))
        except Exception:  # noqa
            self.state_date = None
            self.day_start_equity = None
            self.peak_equity = None
            self.halted = False

    def _save_state(self):
        data = {
            "date": self.state_date,
            "day_start_equity": self.day_start_equity,
            "peak_equity": self.peak_equity,
            "halted": self.halted,
        }
        if self.state_store and self.mode and self.state_date:
            self.state_store.save_equity_state(
                self.state_date,
                self.mode,
                self.day_start_equity,
                self.peak_equity,
                self.halted,
            )
        if self.state_path:
            state_dir = os.path.dirname(self.state_path)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- 포지션 사이징 ---- #
    def position_size(self, equity: float, price: float, candles: list = None) -> int:
        """거래당 위험비율 기반 매수 수량 계산.
        위험금액 = equity * risk_per_trade_pct%
        손절폭 = ATR(있으면) 또는 stop_loss_pct
        수량 = 위험금액 / 손절폭
        """
        if price <= 0 or equity <= 0:
            return 0
        risk_pct = self.cfg.get("risk_per_trade_pct", 1.0) / 100.0
        risk_amount = equity * risk_pct

        stop_dist = price * (self.cfg.get("stop_loss_pct", 5.0) / 100.0)
        if candles and len(candles) > self.cfg.get("atr_period", 14):
            df = ind.to_df(candles)
            atr_val = ind.atr(df, self.cfg.get("atr_period", 14)).iloc[-1]
            if atr_val and atr_val > 0:
                stop_dist = float(atr_val)

        if stop_dist <= 0:
            return 0
        qty = int(risk_amount / stop_dist)
        # 최소 1주, 단 1주 가격이 위험금액보다 크면 0
        if qty < 1:
            qty = 1 if price <= risk_amount else 0
        return qty

    # ---- 손절/익절 판단 ---- #
    def should_exit(self, avg_price: float, cur_price: float, peak_price: float = None) -> str:
        """보유 포지션의 청산 사유 반환: 'stop_loss'|'trailing_stop'|'take_profit'|''"""
        if avg_price <= 0:
            return ""
        pnl_pct = (cur_price - avg_price) / avg_price * 100
        if pnl_pct <= -self.cfg.get("stop_loss_pct", 5.0):
            return "stop_loss"
        trailing_stop_pct = self.cfg.get("trailing_stop_pct", 0.0) or 0.0
        if trailing_stop_pct > 0 and peak_price and peak_price > avg_price and cur_price > avg_price:
            pullback_pct = (cur_price - peak_price) / peak_price * 100
            if pullback_pct <= -trailing_stop_pct:
                return "trailing_stop"
        if pnl_pct >= self.cfg.get("take_profit_pct", 10.0):
            return "take_profit"
        return ""

    def can_open_new(self, current_positions: int) -> bool:
        return current_positions < self.cfg.get("max_positions", 5)

    # ---- 일손실 / MDD 한도 ---- #
    def update_equity(self, equity: float):
        today = self._today()
        if self.state_date != today:
            self.state_date = today
            self.day_start_equity = equity
            self.halted = False
        elif self.day_start_equity is None:
            self.day_start_equity = equity
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
        self._save_state()

    def reset_day(self, equity: float):
        self.state_date = self._today()
        self.day_start_equity = equity
        self.halted = False
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
        self._save_state()

    def check_limits(self, equity: float) -> str:
        """한도 위반 시 사유 반환: 'daily_loss'|'max_drawdown'|''"""
        self.update_equity(equity)
        if self.day_start_equity and self.day_start_equity > 0:
            day_pnl = (equity - self.day_start_equity) / self.day_start_equity * 100
            if day_pnl <= -self.cfg.get("daily_loss_limit_pct", 3.0):
                self.halted = True
                self._save_state()
                return "daily_loss"
        if self.peak_equity and self.peak_equity > 0:
            dd = (equity - self.peak_equity) / self.peak_equity * 100
            if dd <= -self.cfg.get("max_drawdown_pct", 15.0):
                self.halted = True
                self._save_state()
                return "max_drawdown"
        return ""
