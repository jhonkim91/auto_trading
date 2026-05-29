"""기술적 지표 계산 (pandas 기반, 외부 TA-Lib 불필요).

리서치 보고서 2항 기준: RSI, MACD, 볼린저밴드, 이동평균, ATR.
지표는 '매매 버튼'이 아니라 '상태 표시등'으로 다중 확인에 사용.
"""
import numpy as np
import pandas as pd


def to_df(candles: list) -> pd.DataFrame:
    """[{date,open,high,low,close,volume}] -> DataFrame (시간순)."""
    df = pd.DataFrame(candles)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.reset_index(drop=True)


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = sma(series, period)
    std = series.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def volatility_breakout_target(df: pd.DataFrame, k: float = 0.5) -> float:
    """래리 윌리엄스 변동성 돌파 당일 목표가.
    목표가 = 당일 시가 + (전일 고가 - 전일 저가) * K
    """
    if len(df) < 2:
        return float("nan")
    today_open = df["open"].iloc[-1]
    prev_range = df["high"].iloc[-2] - df["low"].iloc[-2]
    return float(today_open + prev_range * k)
