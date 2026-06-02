"""거래 비용 계산 - 위탁수수료, 유관기관제비용, 거래세.

KIS 주문 API는 이 모듈을 직접 사용하지 않는다.
백테스트와 검증 모듈에서만 비용을 추정한다.
"""
from dataclasses import dataclass


@dataclass
class CostResult:
    """단일 주문 비용 상세 결과."""

    commission: float
    exchange_fee: float
    tax: float
    slippage: float
    total: float
    total_pct: float


def compute_trade_cost(
    price: float,
    qty: int,
    side: str,
    market: str = "kosdaq",
    slippage_pct: float = 0.0,
    commission_pct: float = 0.015,
    exchange_fee_pct: float = 0.0036396,
    tax_kospi_pct: float = 0.20,
    tax_kosdaq_pct: float = 0.20,
    overseas_commission_pct: float = 0.025,
) -> CostResult:
    """단일 주문의 비용을 계산한다.

    거래세는 국내 매도에만 적용한다. 모든 비율 입력은 퍼센트 단위다.
    """
    if price <= 0 or qty <= 0:
        return CostResult(0, 0, 0, 0, 0, 0)

    amount = price * qty
    side = (side or "").lower()
    market = (market or "kosdaq").lower()

    if market == "overseas":
        commission = amount * (overseas_commission_pct / 100)
        exchange_fee = 0.0
        tax = 0.0
    else:
        commission = amount * (commission_pct / 100)
        exchange_fee = amount * (exchange_fee_pct / 100)
        if side == "sell":
            tax_rate = tax_kospi_pct if market == "kospi" else tax_kosdaq_pct
            tax = amount * (tax_rate / 100)
        else:
            tax = 0.0

    slippage_cost = amount * (slippage_pct / 100)
    total = commission + exchange_fee + tax + slippage_cost
    total_pct = total / amount * 100 if amount > 0 else 0.0
    return CostResult(commission, exchange_fee, tax, slippage_cost, total, total_pct)


def roundtrip_cost_pct(
    price: float,
    market: str = "kosdaq",
    slippage_pct_one_way: float = 0.0,
    **kwargs,
) -> float:
    """매수와 매도 왕복 비용 합계를 가격 대비 퍼센트로 반환한다."""
    buy = compute_trade_cost(price, 1, "buy", market, slippage_pct_one_way, **kwargs)
    sell = compute_trade_cost(price, 1, "sell", market, slippage_pct_one_way, **kwargs)
    return buy.total_pct + sell.total_pct


def from_settings(settings_costs: dict) -> dict:
    """Settings.costs dict에서 비용 계산 함수 인수를 추출한다."""
    settings_costs = settings_costs or {}
    return {
        "commission_pct": settings_costs.get("commission_pct", 0.015),
        "exchange_fee_pct": settings_costs.get("exchange_fee_pct", 0.0036396),
        "tax_kospi_pct": settings_costs.get("tax_kospi_pct", 0.20),
        "tax_kosdaq_pct": settings_costs.get("tax_kosdaq_pct", 0.20),
        "overseas_commission_pct": settings_costs.get("overseas_commission_pct", 0.025),
    }
