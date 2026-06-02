"""종합 시그널 전략 엔진.

여러 지표가 각각 매수/매도 점수를 내고 가중 합산.
점수 합이 임계치(buy_threshold/sell_threshold) 이상이면 신호 발생.
단일 지표 의존을 피하고 '다중 확인' 하는 보고서 권고를 구현.
"""
from dataclasses import dataclass

import pandas as pd

from . import indicators as ind


@dataclass
class Signal:
    action: str          # "buy" | "sell" | "hold"
    score_buy: float
    score_sell: float
    reasons: list        # 설명 문자열 목록
    price: float         # 최신 종가
    vol_target: float    # 변동성 돌파 목표가(참고)
    adx: float = 0.0     # 추세 강도(0~100)
    trend_ok: bool = True  # 장기추세(레짐) 통과 여부


class CompositeStrategy:
    def __init__(self, strategy_cfg: dict):
        self.cfg = strategy_cfg or {}
        self.ind_cfg = self.cfg.get("indicators", {})
        self.buy_th = self.cfg.get("buy_threshold", 2)
        self.sell_th = self.cfg.get("sell_threshold", 2)

    def evaluate(self, candles: list, current_price: float = None, current_open: float = None) -> Signal:
        """캔들/현재가 기준으로 매수·매도·보유 신호를 산출한다."""
        df = ind.to_df(candles)
        reasons = []
        buy = 0.0
        sell = 0.0
        adx_val = float("nan")
        trend_ok = True

        if df.empty or len(df) < 30:
            return Signal("hold", 0, 0, ["데이터 부족(30봉 미만)"], current_price or 0, float("nan"))

        close = df["close"]
        price = float(current_price if current_price else close.iloc[-1])

        # 1) 이동평균 골든/데드크로스
        c = self.ind_cfg.get("ma_cross", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            short = ind.sma(close, c.get("short", 5))
            long_ = ind.sma(close, c.get("long", 20))
            if short.iloc[-2] <= long_.iloc[-2] and short.iloc[-1] > long_.iloc[-1]:
                buy += w
                reasons.append(f"골든크로스(MA{c.get('short')}>MA{c.get('long')}) +{w}")
            elif short.iloc[-2] >= long_.iloc[-2] and short.iloc[-1] < long_.iloc[-1]:
                sell += w
                reasons.append(f"데드크로스(MA{c.get('short')}<MA{c.get('long')}) +{w}")

        # 2) RSI 과매수/과매도
        c = self.ind_cfg.get("rsi", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            r = ind.rsi(close, c.get("period", 14)).iloc[-1]
            if pd.notna(r):
                if r < c.get("low", 30):
                    buy += w
                    reasons.append(f"RSI 과매도({r:.0f}<{c.get('low')}) +{w}")
                elif r > c.get("high", 70):
                    sell += w
                    reasons.append(f"RSI 과매수({r:.0f}>{c.get('high')}) +{w}")

        # 3) MACD 골든/데드크로스
        c = self.ind_cfg.get("macd", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            macd_line, signal_line, _ = ind.macd(
                close, c.get("fast", 12), c.get("slow", 26), c.get("signal", 9)
            )
            if macd_line.iloc[-2] <= signal_line.iloc[-2] and macd_line.iloc[-1] > signal_line.iloc[-1]:
                buy += w
                reasons.append(f"MACD 골든크로스 +{w}")
            elif macd_line.iloc[-2] >= signal_line.iloc[-2] and macd_line.iloc[-1] < signal_line.iloc[-1]:
                sell += w
                reasons.append(f"MACD 데드크로스 +{w}")

        # 4) 볼린저밴드 하단/상단 터치
        c = self.ind_cfg.get("bollinger", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            upper, mid, lower = ind.bollinger(close, c.get("period", 20), c.get("num_std", 2.0))
            if price < lower.iloc[-1]:
                buy += w
                reasons.append(f"볼린저 하단 이탈(과매도) +{w}")
            elif price > upper.iloc[-1]:
                sell += w
                reasons.append(f"볼린저 상단 돌파(과매수) +{w}")

        # 5) 변동성 돌파 (래리 윌리엄스)
        vol_target = float("nan")
        c = self.ind_cfg.get("vol_breakout", {})
        if c.get("enabled"):
            w = c.get("weight", 2)
            vol_target = ind.volatility_breakout_target(df, c.get("k", 0.5), current_open=current_open)
            if pd.notna(vol_target) and price >= vol_target > 0:
                buy += w
                reasons.append(f"변동성돌파 목표가({vol_target:.2f}) 돌파 +{w}")

        # 6) N일 신고가 돌파
        c = self.ind_cfg.get("new_high", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            period = c.get("period", 60)
            high = ind.highest_high(df, period, exclude_last=True)
            if pd.notna(high) and price >= high > 0:
                buy += w
                reasons.append(f"{period}일 신고가 돌파 +{w}")

        # 7) ADX 추세 강도: 약한 추세에서는 매수 점수 차감
        c = self.ind_cfg.get("adx", {})
        if c.get("enabled"):
            period = c.get("period", 14)
            threshold = c.get("min", 20)
            penalty = c.get("penalty", 1)
            adx_val = ind.adx(df, period).iloc[-1]
            if pd.notna(adx_val) and adx_val < threshold:
                buy = max(0.0, buy - penalty)
                reasons.append(f"추세약함(ADX {adx_val:.0f}<{threshold}) -{penalty}")

        # 최종 판정
        if buy >= self.buy_th and buy > sell:
            action = "buy"
        elif sell >= self.sell_th and sell > buy:
            action = "sell"
        else:
            action = "hold"

        # 8) 레짐 필터: 장기 이평 아래에서는 신규 매수 보류
        c = self.ind_cfg.get("regime", {})
        if c.get("enabled"):
            ma_period = c.get("ma", 60)
            long_ma = ind.sma(close, ma_period).iloc[-1] if len(close) >= ma_period else float("nan")
            if pd.notna(long_ma) and price < long_ma:
                trend_ok = False
                if action == "buy":
                    action = "hold"
                    reasons.append(f"레짐 필터: 가격<{ma_period}일선 매수보류")

        adx_out = float(adx_val) if pd.notna(adx_val) else 0.0
        return Signal(action, buy, sell, reasons, price, vol_target, adx_out, trend_ok)
