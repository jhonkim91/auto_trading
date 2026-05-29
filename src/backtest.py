"""간단한 이벤트 드리븐 백테스터.

FinanceDataReader 로 과거 일봉을 받아 CompositeStrategy 로 매매 시뮬레이션.
핵심 평가지표: 총수익률, 승률, 손익비, MDD (보고서 권고).
수수료+슬리피지를 반영해 과최적화/낙관 편향을 줄인다.
"""
import pandas as pd

from .strategy import CompositeStrategy
from .logger import get_logger

log = get_logger("backtest")


def load_history(code: str, start: str = "2023-01-01", market: str = "domestic") -> list:
    """FinanceDataReader 로 일봉 로드. code: 국내 6자리 또는 미국 심볼."""
    import FinanceDataReader as fdr

    df = fdr.DataReader(code, start)
    if df.empty:
        return []
    df = df.reset_index()
    out = []
    for _, r in df.iterrows():
        out.append(
            {
                "date": str(r["Date"])[:10],
                "open": float(r["Open"]),
                "high": float(r["High"]),
                "low": float(r["Low"]),
                "close": float(r["Close"]),
                "volume": float(r.get("Volume", 0) or 0),
            }
        )
    return out


def run_backtest(candles: list, strategy_cfg: dict, initial_cash: float = 10_000_000,
                 fee_pct: float = 0.015, slippage_pct: float = 0.1) -> dict:
    """단일 종목 롱온리 백테스트."""
    strat = CompositeStrategy(strategy_cfg)
    cash = initial_cash
    shares = 0
    avg_price = 0.0
    equity_curve = []
    trades = []  # 청산된 거래의 수익률

    fee = fee_pct / 100.0
    slip = slippage_pct / 100.0

    # 30봉 워밍업 후부터 시뮬레이션
    for i in range(30, len(candles)):
        window = candles[: i + 1]
        price = window[-1]["close"]
        sig = strat.evaluate(window, current_price=price)

        if sig.action == "buy" and shares == 0:
            buy_price = price * (1 + slip)
            qty = int(cash // (buy_price * (1 + fee)))
            if qty > 0:
                cost = qty * buy_price * (1 + fee)
                cash -= cost
                shares = qty
                avg_price = buy_price
        elif sig.action == "sell" and shares > 0:
            sell_price = price * (1 - slip)
            proceeds = shares * sell_price * (1 - fee)
            cash += proceeds
            ret = (sell_price - avg_price) / avg_price * 100
            trades.append(ret)
            shares = 0
            avg_price = 0.0

        equity = cash + shares * price
        equity_curve.append(equity)

    # 마지막 청산
    if shares > 0:
        price = candles[-1]["close"]
        cash += shares * price * (1 - fee)
        trades.append((price - avg_price) / avg_price * 100)
        shares = 0

    final = cash
    total_return = (final - initial_cash) / initial_cash * 100

    # MDD 계산
    mdd = 0.0
    peak = equity_curve[0] if equity_curve else initial_cash
    for e in equity_curve:
        peak = max(peak, e)
        dd = (e - peak) / peak * 100
        mdd = min(mdd, dd)

    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    return {
        "initial": initial_cash,
        "final": final,
        "total_return_pct": round(total_return, 2),
        "num_trades": len(trades),
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "mdd_pct": round(mdd, 2),
    }
