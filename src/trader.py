"""자동매매 엔진 - 데이터→시그널→리스크→주문 파이프라인 조정.

텔레그램 봇과 분리(notify 콜백으로 알림). 국내/해외 종목 모두 처리.
auto_trade_enabled=False 면 주문 없이 시그널 알림만(반자동).
"""
import threading
import time
from datetime import datetime

from .kis_api import KISApi
from .strategy import CompositeStrategy
from .risk import RiskManager
from .logger import get_logger

log = get_logger("trader")


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_position(position: dict, market: str, currency: str) -> dict:
    item = dict(position or {})
    item["market"] = market
    item["market_label"] = "국내" if market == "domestic" else "미국"
    item["currency"] = currency
    item["position_value"] = _to_float(item.get("cur_price")) * _to_float(item.get("qty"))
    return item


def build_portfolio_snapshot(domestic: dict, overseas: dict) -> dict:
    """국내 원화 잔고와 미국 달러 잔고를 통화별로 분리해 표시용 스냅샷으로 합친다."""
    domestic = domestic or {}
    overseas = overseas or {}
    domestic_positions = [
        _normalize_position(p, "domestic", "KRW")
        for p in domestic.get("positions", [])
    ]
    overseas_positions = [
        _normalize_position(p, "overseas", "USD")
        for p in overseas.get("positions", [])
    ]
    cash_krw = _to_float(domestic.get("cash"))
    total_eval_krw = _to_float(domestic.get("total_eval"))
    cash_usd = _to_float(overseas.get("cash"))
    overseas_value = sum(_to_float(p.get("position_value")) for p in overseas_positions)
    total_eval_usd = _to_float(overseas.get("total_eval"))
    if not total_eval_usd and (cash_usd or overseas_value):
        total_eval_usd = cash_usd + overseas_value
    return {
        "cash": cash_krw,
        "cash_krw": cash_krw,
        "cash_usd": cash_usd,
        "total_eval": total_eval_krw,
        "total_eval_krw": total_eval_krw,
        "total_eval_usd": total_eval_usd,
        "pnl_krw": sum(_to_float(p.get("pnl_amt")) for p in domestic_positions),
        "pnl_usd": sum(_to_float(p.get("pnl_amt")) for p in overseas_positions),
        "positions": domestic_positions + overseas_positions,
        "domestic": domestic,
        "overseas": overseas,
    }


def _format_money(value, currency: str = "KRW", signed: bool = False) -> str:
    value = _to_float(value)
    if currency == "USD":
        return f"{value:+,.2f} USD" if signed else f"{value:,.2f} USD"
    return f"{value:+,.0f}원" if signed else f"{value:,.0f}원"


def _format_balance_lines(krw_value, usd_value=0, include_usd: bool = False, signed: bool = False) -> str:
    lines = [_format_money(krw_value, "KRW", signed=signed)]
    if include_usd or _to_float(usd_value):
        lines.append(_format_money(usd_value, "USD", signed=signed))
    return "\n".join(lines)


class Trader:
    def __init__(self, settings, notify=None):
        self.s = settings
        self.api = KISApi(settings)
        self.strategy = CompositeStrategy(settings.strategy)
        self.risk = RiskManager(settings.risk)
        self.notify = notify or (lambda msg: None)  # 알림 콜백
        self.auto_enabled = settings.engine.get("auto_trade_enabled", True)
        self.running = False
        self._thread = None
        self._stop_evt = threading.Event()
        self.last_signals = {}

    # ----------------------------------------------------------------- #
    #  종목 유니버스
    # ----------------------------------------------------------------- #
    def _domestic_codes(self):
        return self.s.universe.get("domestic", []) or []

    def _overseas_items(self):
        return self.s.universe.get("overseas", []) or []

    # ----------------------------------------------------------------- #
    #  단일 종목 평가 + (자동이면) 주문
    # ----------------------------------------------------------------- #
    def _process_domestic(self, code: str, balance: dict):
        try:
            candles = self.api.domestic_daily(code, count=120)
            quote = self.api.domestic_price(code)
            sig = self.strategy.evaluate(candles, current_price=quote["price"])
            self.last_signals[code] = sig

            pos = next((p for p in balance["positions"] if p["code"] == code), None)

            # 보유중이면 손절/익절 먼저 체크
            if pos:
                reason = self.risk.should_exit(pos["avg_price"], quote["price"])
                if reason or sig.action == "sell":
                    why = reason or "전략 매도신호"
                    if self.auto_enabled:
                        res = self.api.domestic_order(code, pos["qty"], "sell", price=0)
                        self._report_order("매도", code, pos["qty"], quote["price"], why, res)
                    else:
                        self.notify(f"📉 [신호] {code} 매도 추천 ({why}) — 자동매매 OFF")
                    return

            # 신규 매수
            if sig.action == "buy" and not pos:
                if not self.risk.can_open_new(len(balance["positions"])):
                    return
                equity = balance.get("total_eval") or balance.get("cash", 0)
                qty = self.risk.position_size(balance.get("cash", 0), quote["price"], candles)
                if qty < 1:
                    return
                reason = ", ".join(sig.reasons)
                if self.auto_enabled:
                    res = self.api.domestic_order(code, qty, "buy", price=0)
                    self._report_order("매수", code, qty, quote["price"], reason, res)
                else:
                    self.notify(f"📈 [신호] {code} 매수 추천 {qty}주 ({reason}) — 자동매매 OFF")
        except Exception as e:  # noqa
            log.exception("국내 처리 오류 %s", code)
            self.notify(f"⚠️ {code} 처리 오류: {e}")

    def _process_overseas(self, item: dict, balance: dict):
        symbol = item.get("symbol")
        exch = item.get("exchange", "NAS")
        try:
            candles = self.api.overseas_daily(symbol, exch, count=120)
            quote = self.api.overseas_price(symbol, exch)
            sig = self.strategy.evaluate(candles, current_price=quote["price"])
            self.last_signals[symbol] = sig

            pos = next((p for p in balance["positions"] if p["code"] == symbol), None)

            if pos:
                reason = self.risk.should_exit(pos["avg_price"], quote["price"])
                if reason or sig.action == "sell":
                    why = reason or "전략 매도신호"
                    if self.auto_enabled:
                        res = self.api.overseas_order(symbol, pos["qty"], "sell",
                                                      price=quote["price"], exchange=exch)
                        self._report_order("매도(美)", symbol, pos["qty"], quote["price"], why, res)
                    else:
                        self.notify(f"📉 [신호] {symbol} 매도 추천 ({why}) — 자동매매 OFF")
                    return

            if sig.action == "buy" and not pos:
                if not self.risk.can_open_new(len(balance["positions"])):
                    return
                # 해외는 USD 기준; 보유현금 정보가 제한적이라 1주 단위 보수적 매수
                qty = 1
                reason = ", ".join(sig.reasons)
                if self.auto_enabled:
                    res = self.api.overseas_order(symbol, qty, "buy",
                                                  price=quote["price"], exchange=exch)
                    self._report_order("매수(美)", symbol, qty, quote["price"], reason, res)
                else:
                    self.notify(f"📈 [신호] {symbol} 매수 추천 {qty}주 ({reason}) — 자동매매 OFF")
        except Exception as e:  # noqa
            log.exception("해외 처리 오류 %s", symbol)
            self.notify(f"⚠️ {symbol} 처리 오류: {e}")

    def _report_order(self, kind, code, qty, price, reason, res):
        status = "✅성공" if res.get("ok") else f"❌실패({res.get('msg')})"
        msg = f"{kind} {code} {qty}주 @{price:,.2f}\n사유: {reason}\n결과: {status}"
        self.notify(msg)
        log.info(msg.replace("\n", " | "))

    # ----------------------------------------------------------------- #
    #  1회 스캔
    # ----------------------------------------------------------------- #
    def scan_once(self):
        balance = self.safe_domestic_balance()

        # 리스크 한도 체크
        equity = balance.get("total_eval") or balance.get("cash", 0)
        limit = self.risk.check_limits(equity)
        if limit == "daily_loss":
            self.notify("🛑 일일 손실 한도 도달 — 당일 매매를 중단합니다.")
            return
        if limit == "max_drawdown":
            self.notify("🛑 최대낙폭(MDD) 한도 도달 — 전체 청산을 검토하세요. /liquidate")
            return
        if self.risk.halted:
            return

        for code in self._domestic_codes():
            self._process_domestic(code, balance)

        for item in self._overseas_items():
            self._process_overseas(item, balance)

    def safe_domestic_balance(self):
        try:
            return self.api.domestic_balance()
        except Exception as e:  # noqa
            log.warning("잔고조회 실패: %s", e)
            return {"cash": 0, "total_eval": 0, "positions": []}

    def safe_overseas_balance(self):
        items = self._overseas_items()
        exchange = items[0].get("exchange", "NAS") if items else "NAS"
        try:
            return self.api.overseas_balance(exchange)
        except Exception as e:  # noqa
            log.warning("미국 잔고조회 실패: %s", e)
            return {"cash": 0, "total_eval": 0, "positions": []}

    def portfolio_balance(self):
        """대시보드/리포트 표시용 국내+미국 잔고 스냅샷을 반환한다."""
        return build_portfolio_snapshot(self.safe_domestic_balance(), self.safe_overseas_balance())

    # ----------------------------------------------------------------- #
    #  자동매매 루프 (별도 스레드)
    # ----------------------------------------------------------------- #
    def start(self):
        if self.running:
            return False
        self.running = True
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.running = False
        self._stop_evt.set()
        return True

    def _loop(self):
        interval = self.s.engine.get("loop_interval_sec", 60)
        self.notify(f"▶️ 자동매매 루프 시작 (모드: {self.s.mode}, 주기 {interval}s)")
        while not self._stop_evt.is_set():
            try:
                if self._in_session():
                    self.scan_once()
            except Exception:  # noqa
                log.exception("루프 오류")
            self._stop_evt.wait(interval)
        self.notify("⏹️ 자동매매 루프 종료")

    def _in_session(self) -> bool:
        """간단한 장중 시간 체크(국내장 기준). 항상 True 로 두려면 config 조정."""
        now = datetime.now().strftime("%H:%M")
        dsession = self.s.engine.get("domestic_session", "09:00-15:20")
        osession = self.s.engine.get("overseas_session", "23:30-06:00")

        def within(rng):
            try:
                a, b = rng.split("-")
                if a <= b:
                    return a <= now <= b
                return now >= a or now <= b  # 자정 넘김
            except Exception:  # noqa
                return True

        return within(dsession) or within(osession)

    # ----------------------------------------------------------------- #
    #  수동 명령 (텔레그램에서 호출)
    # ----------------------------------------------------------------- #
    def manual_buy(self, code, qty, overseas=False, exchange="NAS"):
        if overseas:
            return self.api.overseas_order(code, qty, "buy", price=0, exchange=exchange)
        return self.api.domestic_order(code, qty, "buy", price=0)

    def manual_sell(self, code, qty, overseas=False, exchange="NAS"):
        if overseas:
            return self.api.overseas_order(code, qty, "sell", price=0, exchange=exchange)
        return self.api.domestic_order(code, qty, "sell", price=0)

    def liquidate_all(self):
        """국내 전체 청산."""
        bal = self.safe_domestic_balance()
        results = []
        for p in bal["positions"]:
            res = self.api.domestic_order(p["code"], p["qty"], "sell", price=0)
            results.append((p["code"], res.get("ok")))
        return results

    def portfolio_report(self) -> str:
        bal = self.portfolio_balance()
        has_usd = bool(bal.get("overseas", {}).get("positions")) or bool(bal.get("cash_usd"))
        lines = [
            f"💼 포트폴리오 ({self.s.mode})",
            "예수금: " + _format_balance_lines(bal.get("cash_krw"), bal.get("cash_usd"), has_usd),
            "총평가: " + _format_balance_lines(
                bal.get("total_eval_krw"), bal.get("total_eval_usd"), has_usd
            ),
        ]
        if not bal["positions"]:
            lines.append("보유 종목 없음")
        for p in bal["positions"]:
            currency = p.get("currency", "KRW")
            lines.append(
                f"• [{p.get('market_label', '-')}] {p['name']}({p['code']}) {p['qty']}주 "
                f"평단 {_format_money(p['avg_price'], currency)} "
                f"현재 {_format_money(p['cur_price'], currency)} "
                f"손익 {_format_money(p.get('pnl_amt', 0), currency, signed=True)} "
                f"({p['pnl_rate']:+.2f}%)"
            )
        return "\n".join(lines)
