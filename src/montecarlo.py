"""몬테카를로 시뮬레이션 - 백테스트 강건성 검증.

거래 순서 재배열, block bootstrap, 승률/손익비 기반 파산 확률을 제공한다.
모든 주요 함수는 고정 seed를 지원한다.
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class MCResult:
    """몬테카를로 결과 요약."""

    n_sims: int
    mdd_median: float
    mdd_p95: float
    mdd_p99: float
    final_return_median: float
    final_return_p5: float
    prob_of_ruin: float
    ruin_threshold: float


def _result_from_paths(
    mdd_list: list,
    final_list: list,
    ruin_count: int,
    n_sims: int,
    ruin_threshold: float,
) -> MCResult:
    """시뮬레이션 경로 목록에서 MCResult를 생성한다."""
    if not mdd_list:
        return MCResult(0, 0, 0, 0, 0, 0, 0, ruin_threshold)
    mdds = np.asarray(mdd_list, dtype=float)
    finals = np.asarray(final_list, dtype=float)
    return MCResult(
        n_sims=n_sims,
        mdd_median=float(np.median(mdds)),
        mdd_p95=float(np.percentile(mdds, 5)),
        mdd_p99=float(np.percentile(mdds, 1)),
        final_return_median=float(np.median(finals)),
        final_return_p5=float(np.percentile(finals, 5)),
        prob_of_ruin=ruin_count / n_sims if n_sims else 0,
        ruin_threshold=ruin_threshold,
    )


def reshuffle_mc(
    trade_pnl: list,
    n_sims: int = 1000,
    ruin_threshold: float = 0.30,
    seed: Optional[int] = None,
) -> MCResult:
    """거래 순서를 무작위 재배열해 MDD 분포를 산출한다."""
    rng = np.random.default_rng(seed)
    trades = np.asarray(trade_pnl, dtype=float)
    if len(trades) == 0 or n_sims <= 0:
        return MCResult(0, 0, 0, 0, 0, 0, 0, ruin_threshold)

    initial = abs(trades).sum() * 2 or 1.0
    mdd_list, final_list = [], []
    ruin_count = 0

    for _ in range(n_sims):
        shuffled = rng.permutation(trades)
        equity = np.concatenate([[initial], initial + np.cumsum(shuffled)])
        running_max = np.maximum.accumulate(equity)
        drawdown = (equity - running_max) / running_max
        mdd = float(drawdown.min())
        mdd_list.append(mdd)
        final_list.append(float(equity[-1] / equity[0] - 1))
        if mdd <= -ruin_threshold:
            ruin_count += 1

    return _result_from_paths(mdd_list, final_list, ruin_count, n_sims, ruin_threshold)


def block_bootstrap_mc(
    returns: list,
    n_sims: int = 1000,
    block_size: int = 10,
    ruin_threshold: float = 0.30,
    seed: Optional[int] = None,
) -> MCResult:
    """Block bootstrap으로 자기상관을 일부 보존한 MC 시뮬레이션을 수행한다."""
    rng = np.random.default_rng(seed)
    returns = np.asarray(returns, dtype=float)
    length = len(returns)
    if length == 0 or n_sims <= 0:
        return MCResult(0, 0, 0, 0, 0, 0, 0, ruin_threshold)

    block_size = max(1, int(block_size))
    mdd_list, final_list = [], []
    ruin_count = 0

    for _ in range(n_sims):
        sampled = []
        while len(sampled) < length:
            start = rng.integers(0, max(1, length - block_size + 1))
            sampled.extend(returns[start:start + block_size].tolist())
        sampled = np.asarray(sampled[:length], dtype=float)
        equity = np.concatenate([[1.0], np.cumprod(1 + sampled)])
        running_max = np.maximum.accumulate(equity)
        drawdown = (equity - running_max) / running_max
        mdd = float(drawdown.min())
        mdd_list.append(mdd)
        final_list.append(float(equity[-1] - 1))
        if mdd <= -ruin_threshold:
            ruin_count += 1

    return _result_from_paths(mdd_list, final_list, ruin_count, n_sims, ruin_threshold)


def prob_of_ruin(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    risk_per_trade: float = 0.01,
    n_trades: int = 200,
    ruin_threshold: float = 0.30,
    n_sims: int = 5000,
    seed: Optional[int] = None,
) -> float:
    """승률과 평균 손익비를 기반으로 파산 확률을 시뮬레이션한다."""
    rng = np.random.default_rng(seed)
    if n_sims <= 0 or n_trades <= 0:
        return 0.0
    ruin_count = 0
    floor = 1.0 - ruin_threshold
    loss_pct = avg_loss_pct or 1

    for _ in range(n_sims):
        equity = 1.0
        ruined = False
        for _ in range(n_trades):
            if rng.random() < win_rate:
                equity *= 1 + risk_per_trade * (avg_win_pct / loss_pct)
            else:
                equity *= 1 - risk_per_trade
            if equity <= floor:
                ruined = True
                break
        ruin_count += int(ruined)

    return ruin_count / n_sims


def mc_report(result: MCResult, backtest_mdd: float = None) -> dict:
    """MCResult를 표시와 go/no-go 판단에 쓰기 쉬운 dict로 반환한다."""
    report = {
        "n_sims": result.n_sims,
        "mdd_median": round(result.mdd_median, 4),
        "mdd_95pct_ci": round(result.mdd_p95, 4),
        "mdd_99pct_ci": round(result.mdd_p99, 4),
        "final_return_median": round(result.final_return_median, 4),
        "final_return_p5": round(result.final_return_p5, 4),
        "prob_of_ruin": round(result.prob_of_ruin, 4),
        "ruin_threshold": result.ruin_threshold,
    }
    if backtest_mdd is not None:
        report["backtest_mdd_worse_than_median"] = result.mdd_median < backtest_mdd
        report["mc_warning"] = abs(result.mdd_median) > abs(backtest_mdd) * 1.5
    return report
