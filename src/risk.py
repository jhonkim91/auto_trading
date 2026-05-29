"""리스크 관리 - 포지션 사이징, 손절/익절, 일손실·MDD 한도.

보고서 3항: 리스크 관리가 전략 자체보다 생존에 더 결정적.
- ATR 기반 또는 계좌대비 고정위험비율 포지션 사이징
- 진입과 동시에 손절 라인 설정
- 일일 손실 한도 / 최대낙폭(MDD) 한도 모니터링
"""
from . import indicators as ind


class RiskManager:
    def __init__(self, risk_cfg: dict):
        self.cfg = risk_cfg or {}
        self.day_start_equity = None     # 당일 시작 자산
        self.peak_equity = None          # 최고 자산(MDD 계산용)
        self.halted = False              # 당일 매매 중단 여부

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
    def should_exit(self, avg_price: float, cur_price: float) -> str:
        """보유 포지션의 청산 사유 반환: 'stop_loss'|'take_profit'|''"""
        if avg_price <= 0:
            return ""
        pnl_pct = (cur_price - avg_price) / avg_price * 100
        if pnl_pct <= -self.cfg.get("stop_loss_pct", 5.0):
            return "stop_loss"
        if pnl_pct >= self.cfg.get("take_profit_pct", 10.0):
            return "take_profit"
        return ""

    def can_open_new(self, current_positions: int) -> bool:
        return current_positions < self.cfg.get("max_positions", 5)

    # ---- 일손실 / MDD 한도 ---- #
    def update_equity(self, equity: float):
        if self.day_start_equity is None:
            self.day_start_equity = equity
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

    def reset_day(self, equity: float):
        self.day_start_equity = equity
        self.halted = False

    def check_limits(self, equity: float) -> str:
        """한도 위반 시 사유 반환: 'daily_loss'|'max_drawdown'|''"""
        self.update_equity(equity)
        if self.day_start_equity and self.day_start_equity > 0:
            day_pnl = (equity - self.day_start_equity) / self.day_start_equity * 100
            if day_pnl <= -self.cfg.get("daily_loss_limit_pct", 3.0):
                self.halted = True
                return "daily_loss"
        if self.peak_equity and self.peak_equity > 0:
            dd = (equity - self.peak_equity) / self.peak_equity * 100
            if dd <= -self.cfg.get("max_drawdown_pct", 15.0):
                self.halted = True
                return "max_drawdown"
        return ""
