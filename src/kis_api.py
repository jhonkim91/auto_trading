"""한국투자증권 KIS Developers REST API 클라이언트.

- OAuth2 토큰 발급/캐싱(token.dat, 24시간 유효·6시간 주기 갱신)
- 유량 제어(실전 초당 20건 / 모의 초당 5건, 안전하게 보수적으로 적용)
- 국내/해외 시세 조회, 주문(모의=V접두 / 실전=T접두), 잔고 조회
- 모든 TR_ID 는 폴더 내 리서치 보고서 6항 기준
"""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import requests

from .config import Settings
from .logger import get_logger

log = get_logger("kis")

_TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "token.dat")
_TOKEN_REFRESH_SEC = 6 * 3600  # 6시간마다 갱신 권장


class RateLimiter:
    """슬라이딩 윈도우 초당 호출 제한."""

    def __init__(self, max_per_sec: int):
        self.max = max_per_sec
        self.calls = deque()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.time()
            while self.calls and now - self.calls[0] > 1.0:
                self.calls.popleft()
            if len(self.calls) >= self.max:
                sleep_for = 1.0 - (now - self.calls[0]) + 0.02
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.time()
                while self.calls and now - self.calls[0] > 1.0:
                    self.calls.popleft()
            self.calls.append(time.time())


class KISApi:
    def __init__(self, settings: Settings):
        self.s = settings
        self.base = settings.base_url
        # 모의=초당5건, 실전=초당20건 → 보수적으로 약간 낮춰 적용
        self.limiter = RateLimiter(4 if settings.is_paper else 18)
        self._token = None
        self._token_issued = 0.0
        self._load_token()

    # ------------------------------------------------------------------ #
    #  토큰 관리
    # ------------------------------------------------------------------ #
    def _load_token(self):
        """파일에 저장된 토큰 재사용(잦은 발급 방지)."""
        if not os.path.exists(_TOKEN_FILE):
            return
        try:
            with open(_TOKEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("mode") != self.s.mode or data.get("app_key") != self.s.app_key:
                return
            issued = data.get("issued", 0)
            # 24시간 유효, 안전하게 6시간만 재사용
            if time.time() - issued < _TOKEN_REFRESH_SEC:
                self._token = data.get("token")
                self._token_issued = issued
                log.info("저장된 토큰 재사용 (발급 후 %.1f시간)", (time.time() - issued) / 3600)
        except Exception as e:  # noqa
            log.warning("토큰 파일 로드 실패: %s", e)

    def _save_token(self):
        try:
            with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "mode": self.s.mode,
                        "app_key": self.s.app_key,
                        "token": self._token,
                        "issued": self._token_issued,
                    },
                    f,
                )
        except Exception as e:  # noqa
            log.warning("토큰 저장 실패: %s", e)

    def token(self) -> str:
        """유효 토큰 반환, 필요시 재발급(재발급은 1분당 1회 제한)."""
        if self._token and time.time() - self._token_issued < _TOKEN_REFRESH_SEC:
            return self._token

        url = f"{self.base}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.s.app_key,
            "appsecret": self.s.app_secret,
        }
        self.limiter.acquire()
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_issued = time.time()
        self._save_token()
        log.info("신규 토큰 발급 완료")
        return self._token

    def _hashkey(self, body: dict) -> str:
        url = f"{self.base}/uapi/hashkey"
        headers = {
            "content-type": "application/json",
            "appkey": self.s.app_key,
            "appsecret": self.s.app_secret,
        }
        self.limiter.acquire()
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        resp.raise_for_status()
        return resp.json()["HASH"]

    def _headers(self, tr_id: str, hashkey: str = None) -> dict:
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token()}",
            "appkey": self.s.app_key,
            "appsecret": self.s.app_secret,
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }
        if hashkey:
            h["hashkey"] = hashkey
        return h

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        self.limiter.acquire()
        resp = requests.get(
            f"{self.base}{path}", headers=self._headers(tr_id), params=params, timeout=10
        )
        if resp.status_code != 200:
            log.error("GET %s 실패 %s: %s", path, resp.status_code, resp.text[:300])
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        hashkey = self._hashkey(body)
        self.limiter.acquire()
        resp = requests.post(
            f"{self.base}{path}",
            headers=self._headers(tr_id, hashkey),
            data=json.dumps(body),
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("POST %s 실패 %s: %s", path, resp.status_code, resp.text[:300])
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    #  국내주식 시세
    # ------------------------------------------------------------------ #
    def domestic_price(self, code: str) -> dict:
        """현재가 조회. 반환: {price, volume, change_rate, high, low, open}"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        data = self._get(path, "FHKST01010100", params)
        o = data.get("output", {})
        return {
            "price": float(o.get("stck_prpr", 0) or 0),
            "open": float(o.get("stck_oprc", 0) or 0),
            "high": float(o.get("stck_hgpr", 0) or 0),
            "low": float(o.get("stck_lwpr", 0) or 0),
            "volume": int(o.get("acml_vol", 0) or 0),
            "change_rate": float(o.get("prdy_ctrt", 0) or 0),
        }

    def domestic_daily(self, code: str, count: int = 100) -> list:
        """일봉 조회. 반환: [{date, open, high, low, close, volume}, ...] (과거→최신)"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=count * 2 + 10)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        data = self._get(path, "FHKST03010100", params)
        rows = data.get("output2", []) or []
        out = []
        for r in rows:
            if not r.get("stck_bsop_date"):
                continue
            out.append(
                {
                    "date": r["stck_bsop_date"],
                    "open": float(r.get("stck_oprc", 0) or 0),
                    "high": float(r.get("stck_hgpr", 0) or 0),
                    "low": float(r.get("stck_lwpr", 0) or 0),
                    "close": float(r.get("stck_clpr", 0) or 0),
                    "volume": int(r.get("acml_vol", 0) or 0),
                }
            )
        out.reverse()  # 과거→최신
        return out

    # ------------------------------------------------------------------ #
    #  국내주식 주문 / 잔고
    # ------------------------------------------------------------------ #
    def domestic_order(self, code: str, qty: int, side: str, price: int = 0) -> dict:
        """현금 매수/매도. side='buy'|'sell', price=0 이면 시장가."""
        path = "/uapi/domestic-stock/v1/trading/order-cash"
        if side == "buy":
            tr_id = "VTTC0802U" if self.s.is_paper else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self.s.is_paper else "TTTC0801U"
        ord_dvsn = "01" if price == 0 else "00"  # 01 시장가 / 00 지정가
        body = {
            "CANO": self.s.account_no,
            "ACNT_PRDT_CD": self.s.account_prod,
            "PDNO": code,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(int(qty)),
            "ORD_UNPR": str(int(price)),
        }
        data = self._post(path, tr_id, body)
        ok = data.get("rt_cd") == "0"
        log.info("국내주문 %s %s x%d @%s -> %s %s",
                 side, code, qty, price, data.get("rt_cd"), data.get("msg1"))
        return {"ok": ok, "raw": data, "msg": data.get("msg1", "")}

    def domestic_balance(self) -> dict:
        """잔고조회. 반환: {cash, total_eval, positions:[{code,name,qty,avg_price,cur_price,pnl_rate}]}"""
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "VTTC8434R" if self.s.is_paper else "TTTC8434R"
        params = {
            "CANO": self.s.account_no,
            "ACNT_PRDT_CD": self.s.account_prod,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        data = self._get(path, tr_id, params)
        positions = []
        for r in data.get("output1", []) or []:
            qty = int(float(r.get("hldg_qty", 0) or 0))
            if qty <= 0:
                continue
            positions.append(
                {
                    "code": r.get("pdno"),
                    "name": r.get("prdt_name"),
                    "qty": qty,
                    "avg_price": float(r.get("pchs_avg_pric", 0) or 0),
                    "cur_price": float(r.get("prpr", 0) or 0),
                    "pnl_rate": float(r.get("evlu_pfls_rt", 0) or 0),
                    "pnl_amt": float(r.get("evlu_pfls_amt", 0) or 0),
                }
            )
        summary = (data.get("output2") or [{}])[0]
        return {
            "cash": float(summary.get("dnca_tot_amt", 0) or 0),
            "total_eval": float(summary.get("tot_evlu_amt", 0) or 0),
            "positions": positions,
        }

    # ------------------------------------------------------------------ #
    #  해외주식(미국) 시세 / 주문 / 잔고
    # ------------------------------------------------------------------ #
    _EXCD = {"NAS": "NAS", "NYS": "NYS", "AMS": "AMS"}  # 나스닥/뉴욕/아멕스

    def overseas_price(self, symbol: str, exchange: str = "NAS") -> dict:
        path = "/uapi/overseas-price/v1/quotations/price"
        params = {"AUTH": "", "EXCD": self._EXCD.get(exchange, "NAS"), "SYMB": symbol}
        data = self._get(path, "HHDFS00000300", params)
        o = data.get("output", {})
        return {
            "price": float(o.get("last", 0) or 0),
            "open": float(o.get("open", 0) or 0),
            "high": float(o.get("high", 0) or 0),
            "low": float(o.get("low", 0) or 0),
            "volume": int(float(o.get("tvol", 0) or 0)),
            "change_rate": float(o.get("rate", 0) or 0),
        }

    def overseas_daily(self, symbol: str, exchange: str = "NAS", count: int = 100) -> list:
        """해외 일봉. 반환: [{date,open,high,low,close,volume}] (과거→최신)"""
        path = "/uapi/overseas-price/v1/quotations/dailyprice"
        params = {
            "AUTH": "",
            "EXCD": self._EXCD.get(exchange, "NAS"),
            "SYMB": symbol,
            "GUBN": "0",  # 0 일 / 1 주 / 2 월
            "BYMD": "",
            "MODP": "1",
        }
        data = self._get(path, "HHDFS76240000", params)
        rows = data.get("output2", []) or []
        out = []
        for r in rows:
            if not r.get("xymd"):
                continue
            out.append(
                {
                    "date": r["xymd"],
                    "open": float(r.get("open", 0) or 0),
                    "high": float(r.get("high", 0) or 0),
                    "low": float(r.get("low", 0) or 0),
                    "close": float(r.get("clos", 0) or 0),
                    "volume": int(float(r.get("tvol", 0) or 0)),
                }
            )
        out.reverse()
        return out

    def overseas_order(self, symbol: str, qty: int, side: str,
                       price: float = 0, exchange: str = "NAS") -> dict:
        """미국주식 주문. 해외는 지정가 기준이라 price 필수(0이면 현재가로 대체)."""
        path = "/uapi/overseas-stock/v1/trading/order"
        # 미국 매수/매도 TR (NASD 통합)
        if side == "buy":
            tr_id = "VTTT1002U" if self.s.is_paper else "TTTT1002U"
        else:
            tr_id = "VTTT1001U" if self.s.is_paper else "TTTT1006U"
        if price <= 0:
            price = self.overseas_price(symbol, exchange)["price"]
        body = {
            "CANO": self.s.account_no,
            "ACNT_PRDT_CD": self.s.account_prod,
            "OVRS_EXCG_CD": {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}.get(exchange, "NASD"),
            "PDNO": symbol,
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": f"{price:.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",  # 지정가
        }
        data = self._post(path, tr_id, body)
        ok = data.get("rt_cd") == "0"
        log.info("해외주문 %s %s x%d @%.2f -> %s %s",
                 side, symbol, qty, price, data.get("rt_cd"), data.get("msg1"))
        return {"ok": ok, "raw": data, "msg": data.get("msg1", "")}

    def overseas_balance(self, exchange: str = "NAS") -> dict:
        path = "/uapi/overseas-stock/v1/trading/inquire-balance"
        tr_id = "VTTS3012R" if self.s.is_paper else "TTTS3012R"
        params = {
            "CANO": self.s.account_no,
            "ACNT_PRDT_CD": self.s.account_prod,
            "OVRS_EXCG_CD": {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}.get(exchange, "NASD"),
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        data = self._get(path, tr_id, params)
        positions = []
        for r in data.get("output1", []) or []:
            qty = int(float(r.get("ovrs_cblc_qty", 0) or 0))
            if qty <= 0:
                continue
            positions.append(
                {
                    "code": r.get("ovrs_pdno"),
                    "name": r.get("ovrs_item_name"),
                    "qty": qty,
                    "avg_price": float(r.get("pchs_avg_pric", 0) or 0),
                    "cur_price": float(r.get("now_pric2", 0) or 0),
                    "pnl_rate": float(r.get("evlu_pfls_rt", 0) or 0),
                    "pnl_amt": float(r.get("frcr_evlu_pfls_amt", 0) or 0),
                }
            )
        return {"positions": positions, "raw": data.get("output2")}
