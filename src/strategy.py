"""보수적 게이트 기반 종합 시그널 전략 엔진.

매수는 레짐, ADX, 1차 트리거, 보조 확인을 순서대로 통과해야 한다.
매도 점수는 기존 지표 기반 판정을 유지해 청산 신호를 별도로 제공한다.
"""
from dataclasses import dataclass

import pandas as pd

from . import indicators as ind


@dataclass
class Signal:
    action: str
    score_buy: float
    score_sell: float
    reasons: list
    price: float
    vol_target: float
    adx: float = 0.0
    trend_ok: bool = True
    gate_passed: bool = True
    gate_reason: str = ""
    primary_trigger: str = ""
    confirm_score: float = 0.0


class CompositeStrategy:
    def __init__(self, strategy_cfg: dict):
        self.cfg = strategy_cfg or {}
        self.ind_cfg = self.cfg.get("indicators", {})
        self.buy_th = self.cfg.get("buy_threshold", 3)
        self.sell_th = self.cfg.get("sell_threshold", 2)
        self.confirm_th = self.cfg.get("confirm_threshold", 1)

    def evaluate(self, candles: list, current_price: float = None, current_open: float = None) -> Signal:
        """캔들/현재가 기준으로 매수·매도·보유 신호를 산출한다."""
        df = ind.to_df(candles)
        price = float(current_price or (df["close"].iloc[-1] if not df.empty else 0))
        if df.empty or len(df) < 60:
            return Signal(
                "hold",
                0,
                0,
                ["데이터 부족(60봉 미만)"],
                price,
                float("nan"),
                gate_passed=False,
                gate_reason="data_insufficient",
            )

        close = df["close"]
        reasons = []
        confirm_score = 0.0
        sell_score = 0.0
        adx_val = float("nan")
        vol_target = float("nan")
        primary_trigger = ""

        # G2. 레짐 필터
        regime_cfg = self.ind_cfg.get("regime", {})
        if regime_cfg.get("enabled", True):
            ma_period = regime_cfg.get("ma", 60)
            long_ma = ind.sma(close, ma_period).iloc[-1]
            if pd.notna(long_ma) and price < long_ma:
                return Signal(
                    "hold",
                    0,
                    0,
                    [f"레짐 필터: 가격<{ma_period}일선"],
                    price,
                    float("nan"),
                    trend_ok=False,
                    gate_passed=False,
                    gate_reason="regime_filter",
                )

        # G3. ADX 최솟값
        adx_cfg = self.ind_cfg.get("adx", {})
        if adx_cfg.get("enabled", True):
            adx_val = ind.adx(df, adx_cfg.get("period", 14)).iloc[-1]
            if pd.notna(adx_val) and adx_val < adx_cfg.get("min", 20):
                return Signal(
                    "hold",
                    0,
                    0,
                    [f"ADX {adx_val:.0f} < {adx_cfg.get('min', 20)}"],
                    price,
                    float("nan"),
                    adx=float(adx_val),
                    gate_passed=False,
                    gate_reason="adx_weak",
                )

        # G4. 1차 트리거
        vol_cfg = self.ind_cfg.get("vol_breakout", {})
        if vol_cfg.get("enabled", True):
            vol_target = ind.volatility_breakout_target(
                df,
                vol_cfg.get("k", 0.5),
                current_open=current_open,
            )
            if pd.notna(vol_target) and price >= vol_target > 0:
                primary_trigger = "vol_breakout"
                reasons.append(f"변동성돌파({vol_target:.0f}) +트리거")

        high_cfg = self.ind_cfg.get("new_high", {})
        if high_cfg.get("enabled", True) and not primary_trigger:
            period = high_cfg.get("period", 60)
            highest = ind.highest_high(df, period, exclude_last=True)
            if pd.notna(highest) and price >= highest > 0:
                primary_trigger = "new_high"
                reasons.append(f"{period}일 신고가 돌파 +트리거")

        confirm_score, sell_score, confirm_reasons = self._confirmation_scores(df, price)
        reasons.extend(confirm_reasons)
        adx_out = float(adx_val) if pd.notna(adx_val) else 0.0

        if sell_score >= self.sell_th and sell_score > confirm_score:
            return Signal(
                "sell",
                confirm_score,
                sell_score,
                reasons,
                price,
                vol_target,
                adx_out,
                True,
                True,
                "",
                primary_trigger,
                confirm_score,
            )

        if not primary_trigger:
            return Signal(
                "hold",
                confirm_score,
                sell_score,
                ["1차 트리거 없음(변동성돌파/신고가 미충족)"],
                price,
                vol_target,
                adx_out,
                True,
                False,
                "no_primary_trigger",
                "",
                confirm_score,
            )

        if confirm_score >= self.confirm_th:
            return Signal(
                "buy",
                confirm_score,
                sell_score,
                reasons,
                price,
                vol_target,
                adx_out,
                True,
                True,
                "",
                primary_trigger,
                confirm_score,
            )

        return Signal(
            "hold",
            confirm_score,
            sell_score,
            reasons + [f"보조 확인 부족({confirm_score:.0f}<{self.confirm_th})"],
            price,
            vol_target,
            adx_out,
            True,
            False,
            "confirm_insufficient",
            primary_trigger,
            confirm_score,
        )

    def _confirmation_scores(self, df: pd.DataFrame, price: float) -> tuple[float, float, list]:
        """보조 확인 지표와 매도 점수를 계산한다."""
        close = df["close"]
        confirm_score = 0.0
        sell_score = 0.0
        reasons = []

        ma_cfg = self.ind_cfg.get("ma_cross", {})
        if ma_cfg.get("enabled", True):
            weight = ma_cfg.get("weight", 1)
            short = ind.sma(close, ma_cfg.get("short", 5))
            long = ind.sma(close, ma_cfg.get("long", 20))
            if short.iloc[-2] <= long.iloc[-2] and short.iloc[-1] > long.iloc[-1]:
                confirm_score += weight
                reasons.append(f"MA 골든크로스 +{weight}")
            elif short.iloc[-2] >= long.iloc[-2] and short.iloc[-1] < long.iloc[-1]:
                sell_score += weight
                reasons.append(f"MA 데드크로스 +{weight}")

        rsi_cfg = self.ind_cfg.get("rsi", {})
        if rsi_cfg.get("enabled", True):
            weight = rsi_cfg.get("weight", 1)
            rsi = ind.rsi(close, rsi_cfg.get("period", 14)).iloc[-1]
            if pd.notna(rsi):
                if rsi < rsi_cfg.get("low", 30):
                    confirm_score += weight
                    reasons.append(f"RSI 과매도({rsi:.0f}) +{weight}")
                elif rsi > rsi_cfg.get("high", 70):
                    sell_score += weight
                    reasons.append(f"RSI 과매수({rsi:.0f}) +{weight}")

        macd_cfg = self.ind_cfg.get("macd", {})
        if macd_cfg.get("enabled", True):
            weight = macd_cfg.get("weight", 1)
            macd_line, signal_line, _ = ind.macd(
                close,
                macd_cfg.get("fast", 12),
                macd_cfg.get("slow", 26),
                macd_cfg.get("signal", 9),
            )
            if macd_line.iloc[-2] <= signal_line.iloc[-2] and macd_line.iloc[-1] > signal_line.iloc[-1]:
                confirm_score += weight
                reasons.append(f"MACD 골든 +{weight}")
            elif macd_line.iloc[-2] >= signal_line.iloc[-2] and macd_line.iloc[-1] < signal_line.iloc[-1]:
                sell_score += weight
                reasons.append(f"MACD 데드 +{weight}")

        bollinger_cfg = self.ind_cfg.get("bollinger", {})
        if bollinger_cfg.get("enabled", True):
            weight = bollinger_cfg.get("weight", 1)
            upper, _, lower = ind.bollinger(
                close,
                bollinger_cfg.get("period", 20),
                bollinger_cfg.get("num_std", 2.0),
            )
            if price < lower.iloc[-1]:
                confirm_score += weight
                reasons.append(f"볼린저 하단 +{weight}")
            elif price > upper.iloc[-1]:
                sell_score += weight
                reasons.append(f"볼린저 상단 +{weight}")

        return confirm_score, sell_score, reasons
