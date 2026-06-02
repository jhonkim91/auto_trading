"""Rolling Walk-Forward Analysis (WFA).

IS(in-sample)에서 최적화한 파라미터를 OOS(out-of-sample)에 검증하는
슬라이딩 윈도우 분석을 제공한다.
"""
from dataclasses import dataclass
from typing import Callable, List, Tuple

import numpy as np


@dataclass
class WFAResult:
    """Walk-forward 분석 결과."""

    n_windows: int
    oos_returns: list
    oos_trades: list
    is_metrics: list
    oos_metrics: list
    best_params_history: list
    wfe: float
    n_oos_trades: int
    calculated: bool = True
    reason: str = ""


def walk_forward_splits(
    n: int,
    train_frac: float = 0.80,
    n_windows: int = 6,
    anchored: bool = False,
) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Rolling 또는 anchored WFA 윈도우 인덱스를 생성한다."""
    if n <= 1 or n_windows <= 0 or not 0 < train_frac < 1:
        return []
    total_test = int(n * (1 - train_frac))
    test_size = max(1, total_test // n_windows)
    train_size = int(test_size * train_frac / (1 - train_frac))

    splits = []
    for index in range(n_windows):
        test_start = n - total_test + index * test_size
        test_end = min(test_start + test_size, n)
        if test_start >= n:
            break
        if anchored:
            train_idx = np.arange(0, test_start)
        else:
            train_idx = np.arange(max(0, test_start - train_size), test_start)
        test_idx = np.arange(test_start, test_end)
        if len(train_idx) > 0 and len(test_idx) > 0:
            splits.append((train_idx, test_idx))
    return splits


def wfe(is_annual_return: float, oos_annual_return: float) -> float:
    """Walk-Forward Efficiency를 계산한다."""
    if is_annual_return <= 0:
        return float("nan")
    return oos_annual_return / is_annual_return


def run_wfa(
    candles: list,
    param_grid: list,
    backtest_fn: Callable,
    n_windows: int = 6,
    train_frac: float = 0.80,
    anchored: bool = False,
    min_is_trades: int = 10,
    min_oos_trades: int = 5,
    rf: float = 0.025,
    N: int = 252,
) -> WFAResult:
    """Rolling WFA를 실행한다."""
    del rf
    n = len(candles)
    splits = walk_forward_splits(n, train_frac, n_windows, anchored)
    if not splits:
        return WFAResult(
            0,
            [],
            [],
            [],
            [],
            [],
            float("nan"),
            0,
            calculated=False,
            reason="데이터 부족 - 윈도우 생성 불가",
        )

    all_oos_returns, all_oos_trades = [], []
    is_metrics_list, oos_metrics_list, params_history = [], [], []

    for window_index, (train_idx, test_idx) in enumerate(splits):
        is_candles = [candles[i] for i in train_idx]
        oos_candles = [candles[i] for i in test_idx]

        best_params, best_sharpe = None, -np.inf
        for params in param_grid:
            try:
                result = backtest_fn(is_candles, params)
            except Exception:
                continue
            sharpe = result.get("sharpe", -np.inf)
            if sharpe > best_sharpe and len(result.get("trades", [])) >= min_is_trades:
                best_sharpe = sharpe
                best_params = params

        if best_params is None:
            continue

        try:
            oos_result = backtest_fn(oos_candles, best_params)
        except Exception:
            continue

        oos_returns = oos_result.get("returns", [])
        oos_trades = oos_result.get("trades", [])
        all_oos_returns.extend(oos_returns)
        all_oos_trades.extend(oos_trades)
        is_metrics_list.append({"window": window_index, "sharpe": best_sharpe, "params": best_params})
        oos_metrics_list.append(
            {"window": window_index, "sharpe": oos_result.get("sharpe"), "trades": len(oos_trades)}
        )
        params_history.append(best_params)

    n_oos_trades = len(all_oos_trades)
    if n_oos_trades < min_oos_trades:
        return WFAResult(
            len(splits),
            all_oos_returns,
            all_oos_trades,
            is_metrics_list,
            oos_metrics_list,
            params_history,
            float("nan"),
            n_oos_trades,
            calculated=False,
            reason=f"OOS 거래수 부족 ({n_oos_trades} < {min_oos_trades})",
        )

    is_values = [metric["sharpe"] for metric in is_metrics_list if metric.get("sharpe")]
    is_ann = np.mean(is_values) * np.sqrt(N) if is_values else float("nan")
    oos_array = np.asarray(all_oos_returns, dtype=float)
    oos_ann = oos_array.mean() * N if len(oos_array) > 0 else 0.0
    wfe_value = wfe(is_ann, oos_ann)

    return WFAResult(
        n_windows=len(params_history),
        oos_returns=all_oos_returns,
        oos_trades=all_oos_trades,
        is_metrics=is_metrics_list,
        oos_metrics=oos_metrics_list,
        best_params_history=params_history,
        wfe=round(wfe_value, 4),
        n_oos_trades=n_oos_trades,
    )


def param_stability_score(params_history: list) -> dict:
    """윈도우별 최적 파라미터의 변동성을 요약한다."""
    if not params_history or len(params_history) < 2:
        return {}
    all_keys = set().union(*[params.keys() for params in params_history])
    result = {}
    for key in all_keys:
        values = [
            params[key]
            for params in params_history
            if key in params and isinstance(params[key], (int, float))
        ]
        if len(values) >= 2:
            mean = np.mean(values)
            cv = np.std(values) / (mean or 1)
            result[key] = {"mean": round(mean, 4), "cv": round(cv, 4), "stable": cv < 0.20}
    return result
