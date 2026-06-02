"""슬리피지 추정 모듈.

Corwin-Schultz 일봉 스프레드 추정과 변동성 연동 모델을 제공한다.
백테스트와 검증 모듈에서만 사용한다.
"""
import math

import numpy as np
import pandas as pd


def _as_dataframe(candles) -> pd.DataFrame:
    """캔들 입력을 DataFrame으로 정규화한다."""
    return candles.copy() if isinstance(candles, pd.DataFrame) else pd.DataFrame(candles)


def corwin_schultz_spread(df) -> pd.Series:
    """Corwin-Schultz 일봉 bid-ask 스프레드를 소수점 비율로 추정한다."""
    df = _as_dataframe(df)
    if df.empty or "high" not in df.columns or "low" not in df.columns:
        return pd.Series(dtype=float)

    high = df["high"].astype(float)
    low = df["low"].astype(float)

    if "close" in df.columns and "open" in df.columns:
        prev_close = df["close"].shift(1).astype(float)
        gap = prev_close / df["open"].astype(float).replace(0, np.nan)
        gap = gap.where(gap.notna(), 1.0).clip(0.9, 1.1)
        high = high * gap
        low = low * gap

    prev_high = high.shift(1)
    prev_low = low.shift(1)
    beta = np.log(prev_high / prev_low) ** 2 + np.log(high / low) ** 2
    high_2d = np.maximum(prev_high, high)
    low_2d = np.minimum(prev_low, low)
    gamma = np.log(high_2d / low_2d) ** 2

    coefficient = 3 - 2 * math.sqrt(2)
    alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / coefficient - np.sqrt(gamma / coefficient)
    spread = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    return pd.Series(spread, index=df.index).where(spread >= 0, 0.0)


def one_way_slippage_pct(df, min_bps: float = 5.0) -> pd.Series:
    """편도 슬리피지를 소수점 비율로 추정한다."""
    spread = corwin_schultz_spread(df)
    min_pct = min_bps / 10000
    if spread.empty:
        return pd.Series(dtype=float)
    return pd.Series(np.maximum(spread / 2, min_pct), index=spread.index)


class VolatilitySlippage:
    """변동성과 주문 크기 충격을 반영한 편도 슬리피지 모델."""

    def __init__(self, min_bps: float = 5.0, vol_factor: float = 0.15, vol_window: int = 20):
        self.min_bps = min_bps
        self.vol_factor = vol_factor
        self.vol_window = vol_window

    def estimate(self, df, order_qty: int = 1, adv_qty: int = 100_000) -> pd.Series:
        """봉별 편도 슬리피지를 소수점 비율로 반환한다."""
        df = _as_dataframe(df)
        if df.empty or "close" not in df.columns:
            return pd.Series(dtype=float)
        close = df["close"].astype(float)
        returns = close.pct_change()
        volatility = returns.rolling(self.vol_window).std()

        base = self.min_bps / 10000
        vol_impact = volatility * self.vol_factor
        size_impact = (order_qty / adv_qty) * 0.005 if adv_qty > 0 else 0.0
        return (base + vol_impact + size_impact).fillna(base)


def estimate_from_candles(candles: list, min_bps: float = 5.0) -> float:
    """캔들 리스트에서 평균 편도 슬리피지를 단일 float으로 반환한다."""
    df = _as_dataframe(candles)
    if df.empty or "high" not in df.columns:
        return min_bps / 10000
    estimated = one_way_slippage_pct(df, min_bps)
    clean = estimated.dropna()
    return float(clean.mean()) if not clean.empty else min_bps / 10000
