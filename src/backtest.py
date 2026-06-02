"""이벤트 드리븐 백테스터.

신호는 현재 봉 종가 기준으로 판단하고 체결은 다음 봉 시가로 처리해
미래참조 위험을 줄인다. 비용과 슬리피지는 백테스트 결과에만 반영한다.
"""
import pandas as pd

from . import costs, slippage
from .logger import get_logger
from .strategy import CompositeStrategy

log = get_logger("backtest")


def load_history(code: str, start: str = "2023-01-01", market: str = "domestic") -> list:
    """FinanceDataReader로 일봉을 로드한다."""
    del market
    import FinanceDataReader as fdr

    frame = fdr.DataReader(code, start)
    if frame.empty:
        return []
    frame = frame.reset_index()
    out = []
    for _, row in frame.iterrows():
        out.append(
            {
                "date": str(row["Date"])[:10],
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row.get("Volume", 0) or 0),
            }
        )
    return out


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _trade_slippage_pct(candles: list, costs_cfg: dict, use_cs_slippage: bool) -> float:
    """비용 계산 함수에 넘길 슬리피지를 퍼센트 단위로 반환한다."""
    min_bps = (costs_cfg or {}).get("min_slippage_bps", 5.0)
    if not use_cs_slippage:
        return min_bps / 100
    return slippage.estimate_from_candles(candles, min_bps=min_bps) * 100


def _daily_return(equity_curve: list, equity: float) -> float:
    """직전 자산 대비 일별 수익률을 계산한다."""
    previous = equity_curve[-1] if equity_curve else equity
    return (equity / previous - 1) if previous else 0.0


def run_backtest(
    candles: list,
    strategy_cfg: dict,
    initial_cash: float = 10_000_000,
    market: str = "kosdaq",
    costs_cfg: dict = None,
    use_cs_slippage: bool = True,
    rf: float = 0.025,
) -> dict:
    """단일 종목 롱온리 백테스트를 실행한다.

    기존 요약 반환 키를 유지하고, 거래 상세/비용/수익률/검증 결과를 추가한다.
    """
    if not candles:
        return _empty_result(initial_cash)

    strat = CompositeStrategy(strategy_cfg)
    cash = float(initial_cash)
    shares = 0
    entry_cost = 0.0
    entry_price = 0.0
    entry_date = ""
    equity_curve = [float(initial_cash)]
    returns = []
    trades = []
    costs_total = 0.0
    cost_kwargs = costs.from_settings(costs_cfg or {})

    for index in range(30, len(candles) - 1):
        window = candles[: index + 1]
        signal_bar = window[-1]
        exec_bar = candles[index + 1]
        signal_price = _to_float(signal_bar.get("close"))
        exec_price = _to_float(exec_bar.get("open"))
        mark_price = _to_float(exec_bar.get("close"), exec_price)
        if exec_price <= 0:
            continue

        signal = strat.evaluate(
            window,
            current_price=signal_price,
            current_open=signal_bar.get("open"),
        )
        slip_pct = _trade_slippage_pct(window, costs_cfg or {}, use_cs_slippage)

        if signal.action == "buy" and shares == 0:
            unit_cost = costs.compute_trade_cost(
                exec_price,
                1,
                "buy",
                market,
                slip_pct,
                **cost_kwargs,
            )
            qty = int(cash // (exec_price * (1 + unit_cost.total_pct / 100)))
            if qty > 0:
                cost_result = costs.compute_trade_cost(
                    exec_price,
                    qty,
                    "buy",
                    market,
                    slip_pct,
                    **cost_kwargs,
                )
                gross = qty * exec_price
                total = gross + cost_result.total
                if total <= cash:
                    cash -= total
                    shares = qty
                    entry_cost = total
                    entry_price = exec_price
                    entry_date = exec_bar.get("date", "")
                    costs_total += cost_result.total

        elif signal.action == "sell" and shares > 0:
            cost_result = costs.compute_trade_cost(
                exec_price,
                shares,
                "sell",
                market,
                slip_pct,
                **cost_kwargs,
            )
            gross = shares * exec_price
            proceeds = gross - cost_result.total
            cash += proceeds
            costs_total += cost_result.total
            pnl = proceeds - entry_cost
            trades.append(
                {
                    "entry_date": entry_date,
                    "exit_date": exec_bar.get("date", ""),
                    "entry_price": entry_price,
                    "exit_price": exec_price,
                    "exec_price": exec_price,
                    "qty": shares,
                    "pnl": pnl,
                    "return_pct": pnl / entry_cost * 100 if entry_cost else 0.0,
                    "cost": cost_result.total,
                    "side": "sell",
                    "reason": "; ".join(signal.reasons),
                }
            )
            shares = 0
            entry_cost = 0.0
            entry_price = 0.0
            entry_date = ""

        equity = cash + shares * mark_price
        returns.append(_daily_return(equity_curve, equity))
        equity_curve.append(equity)

    if shares > 0:
        final_bar = candles[-1]
        final_price = _to_float(final_bar.get("close"))
        slip_pct = _trade_slippage_pct(candles, costs_cfg or {}, use_cs_slippage)
        cost_result = costs.compute_trade_cost(
            final_price,
            shares,
            "sell",
            market,
            slip_pct,
            **cost_kwargs,
        )
        proceeds = shares * final_price - cost_result.total
        cash += proceeds
        costs_total += cost_result.total
        pnl = proceeds - entry_cost
        trades.append(
            {
                "entry_date": entry_date,
                "exit_date": final_bar.get("date", ""),
                "entry_price": entry_price,
                "exit_price": final_price,
                "exec_price": final_price,
                "qty": shares,
                "pnl": pnl,
                "return_pct": pnl / entry_cost * 100 if entry_cost else 0.0,
                "cost": cost_result.total,
                "side": "sell",
                "reason": "final_liquidation",
            }
        )
        shares = 0
        if equity_curve:
            equity = cash
            returns.append(_daily_return(equity_curve, equity))
            equity_curve.append(equity)

    return _summary_result(
        initial_cash=initial_cash,
        final_cash=cash,
        trades=trades,
        equity_curve=equity_curve,
        returns=returns,
        costs_total=costs_total,
        rf=rf,
    )


def _empty_result(initial_cash: float) -> dict:
    """데이터가 없을 때의 하위 호환 결과를 반환한다."""
    return _summary_result(
        initial_cash=initial_cash,
        final_cash=initial_cash,
        trades=[],
        equity_curve=[float(initial_cash)],
        returns=[],
        costs_total=0.0,
        rf=0.025,
    )


def _summary_result(
    initial_cash: float,
    final_cash: float,
    trades: list,
    equity_curve: list,
    returns: list,
    costs_total: float,
    rf: float,
) -> dict:
    """기존 요약 키와 신규 상세 키를 함께 구성한다."""
    del rf
    total_return = (final_cash - initial_cash) / initial_cash * 100 if initial_cash else 0.0
    mdd_pct = _max_drawdown_pct(equity_curve)
    returns_pct = [trade.get("return_pct", 0.0) for trade in trades]
    wins = [value for value in returns_pct if value > 0]
    losses = [value for value in returns_pct if value <= 0]
    win_rate = len(wins) / len(returns_pct) * 100 if returns_pct else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    result = {
        "initial": initial_cash,
        "final": final_cash,
        "total_return_pct": round(total_return, 2),
        "num_trades": len(trades),
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "mdd_pct": round(mdd_pct, 2),
        "costs_total": round(costs_total, 4),
        "trades": trades,
        "equity_curve": equity_curve,
        "returns": returns,
    }
    result["validation"] = validate_backtest(result)
    return result


def _max_drawdown_pct(equity_curve: list) -> float:
    """최대낙폭을 퍼센트 단위로 계산한다."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    mdd = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak:
            mdd = min(mdd, (equity - peak) / peak * 100)
    return mdd


def validate_backtest(
    result: dict,
    validation_cfg: dict = None,
    n_mc_sims: int = 1000,
    seed: int = 42,
) -> dict:
    """PSR, MC 파산확률, 최소 거래수 기준으로 go/no-go를 계산한다."""
    from src.metrics import psr as calc_psr
    from src.montecarlo import reshuffle_mc

    validation_cfg = validation_cfg or {}
    min_trades = validation_cfg.get("min_oos_trades", 30)
    psr_min = validation_cfg.get("psr_min", 0.95)
    ror_max = validation_cfg.get("ror_max", 0.05)
    trades = result.get("trades", [])
    returns = result.get("returns", [])
    n_trades = len(trades)

    if n_trades < min_trades:
        return {
            "calculated": False,
            "reason": f"거래수 부족 ({n_trades} < {min_trades})",
            "go_no_go": False,
            "gates": {},
        }

    psr_value = calc_psr(returns)
    trade_pnl = [trade.get("pnl", 0) for trade in trades]
    mc_result = reshuffle_mc(
        trade_pnl,
        n_sims=n_mc_sims,
        seed=seed,
        ruin_threshold=validation_cfg.get("mc_ruin_threshold", 0.30),
    )
    gates = {
        "psr": {"value": round(psr_value, 4), "threshold": psr_min, "pass": psr_value >= psr_min},
        "prob_of_ruin": {
            "value": round(mc_result.prob_of_ruin, 4),
            "threshold": ror_max,
            "pass": mc_result.prob_of_ruin <= ror_max,
        },
        "min_trades": {"value": n_trades, "threshold": min_trades, "pass": True},
    }
    go_no_go = all(gate["pass"] for gate in gates.values())
    return {
        "calculated": True,
        "psr": psr_value,
        "mdd_mc_p95": mc_result.mdd_p95,
        "prob_of_ruin": mc_result.prob_of_ruin,
        "go_no_go": go_no_go,
        "gates": gates,
        "reason": "" if go_no_go else "게이트 미통과: " + ", ".join(
            key for key, value in gates.items() if not value["pass"]
        ),
    }
