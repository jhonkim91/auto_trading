"""백테스트 성과 지표.

Sharpe, Sortino, Calmar는 일봉 기준 연율화 계수 252를 기본으로 사용한다.
PSR/DSR은 과최적화 가능성을 낮추기 위한 통계 검증 보조 지표다.
"""
import math
from statistics import NormalDist

import numpy as np

EULER = 0.5772156649
_NORMAL = NormalDist()


def _clean_array(values) -> np.ndarray:
    """NaN을 제거한 float 배열을 반환한다."""
    array = np.asarray(values, dtype=float)
    return array[~np.isnan(array)]


def _skew(values: np.ndarray) -> float:
    """왜도를 계산한다."""
    if len(values) < 2:
        return float("nan")
    std = values.std(ddof=0)
    if std <= 0:
        return float("nan")
    centered = values - values.mean()
    return float(np.mean((centered / std) ** 3))


def _kurtosis(values: np.ndarray) -> float:
    """Fisher 보정 없는 첨도를 계산한다."""
    if len(values) < 2:
        return float("nan")
    std = values.std(ddof=0)
    if std <= 0:
        return float("nan")
    centered = values - values.mean()
    return float(np.mean((centered / std) ** 4))


def sharpe(returns, N: int = 252, rf: float = 0.025) -> float:
    """연율화 Sharpe Ratio를 반환한다."""
    r = _clean_array(returns)
    if len(r) < 2:
        return float("nan")
    excess = r.mean() * N - rf
    volatility = r.std(ddof=1) * math.sqrt(N)
    return excess / volatility if volatility > 0 else float("nan")


def sortino(returns, N: int = 252, rf: float = 0.025) -> float:
    """연율화 Sortino Ratio를 반환한다."""
    r = _clean_array(returns)
    if len(r) < 2:
        return float("nan")
    downside = r[r < 0]
    if len(downside) == 0:
        return float("inf")
    downside_std = downside.std(ddof=1) * math.sqrt(N)
    return (r.mean() * N - rf) / downside_std if downside_std > 0 else float("nan")


def calmar(equity_curve, N: int = 252) -> float:
    """Calmar Ratio를 계산한다."""
    equity = _clean_array(equity_curve)
    if len(equity) < 2 or equity[0] <= 0:
        return float("nan")
    periods = len(equity)
    cagr = (equity[-1] / equity[0]) ** (N / periods) - 1
    mdd = max_drawdown(equity)
    return cagr / abs(mdd) if mdd < 0 else float("nan")


def max_drawdown(equity_curve) -> float:
    """최대낙폭을 음수 비율로 반환한다."""
    equity = _clean_array(equity_curve)
    if len(equity) < 2:
        return 0.0
    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    return float(drawdown.min())


def omega(returns, threshold: float = 0.0) -> float:
    """Omega Ratio를 계산한다."""
    r = _clean_array(returns)
    gains = (r[r > threshold] - threshold).sum()
    losses = (threshold - r[r < threshold]).sum()
    return float(gains / losses) if losses > 0 else float("inf")


def sharpe_tstat(returns) -> float:
    """Sharpe의 단순 t-통계량을 계산한다."""
    r = _clean_array(returns)
    if len(r) < 2:
        return float("nan")
    std = r.std(ddof=1)
    if std <= 0:
        return float("nan")
    return float((r.mean() / std) * math.sqrt(len(r)))


def psr(returns, sr_benchmark: float = 0.0) -> float:
    """Probabilistic Sharpe Ratio를 반환한다."""
    r = _clean_array(returns)
    observations = len(r)
    if observations < 4:
        return float("nan")
    std = r.std(ddof=1)
    if std <= 0:
        return float("nan")
    sr_hat = r.mean() / std
    g3 = _skew(r)
    g4 = _kurtosis(r)
    denom = 1 - g3 * sr_hat + (g4 - 1) / 4 * sr_hat ** 2
    if denom <= 0 or math.isnan(denom):
        return float("nan")
    sigma = math.sqrt(denom / (observations - 1))
    return float(_NORMAL.cdf((sr_hat - sr_benchmark) / sigma))


def deflated_sharpe(returns, sr_trials) -> float:
    """다중 전략 시도 수를 보정한 Deflated Sharpe Ratio를 반환한다."""
    r = _clean_array(returns)
    trials = _clean_array(sr_trials)
    count = len(trials)
    if count < 2 or len(r) < 4:
        return float("nan")
    var_sr = float(np.var(trials, ddof=1))
    if var_sr <= 0:
        return float("nan")
    expected_max_sr = math.sqrt(var_sr) * (
        (1 - EULER) * _NORMAL.inv_cdf(1 - 1.0 / count)
        + EULER * _NORMAL.inv_cdf(1 - 1.0 / (count * math.e))
    )
    return psr(r, sr_benchmark=expected_max_sr)


def summary(returns, equity_curve=None, N: int = 252, rf: float = 0.025) -> dict:
    """주요 성과 지표를 dict로 반환한다."""
    r = _clean_array(returns)
    result = {
        "sharpe": round(sharpe(r, N, rf), 4),
        "sortino": round(sortino(r, N, rf), 4),
        "omega": round(omega(r), 4),
        "psr": round(psr(r), 4),
        "t_stat": round(sharpe_tstat(r), 4),
        "n_returns": len(r),
    }
    if equity_curve is not None:
        result["calmar"] = round(calmar(equity_curve, N), 4)
        result["mdd"] = round(max_drawdown(equity_curve), 4)
    return result
