# -*- coding: utf-8 -*-
"""
한국투자증권 주식 분석 · 자동매매 — 단일 파일 GUI 프로그램.

이 파일 하나로 모든 기능(설정·KIS API·지표·전략·리스크·매매엔진·텔레그램봇·GUI)이 동작합니다.

실행:
  python autotrader.py            # GUI 실행
  더블클릭: run_app.bat
  단일 exe 빌드: build_exe.bat  ->  dist\\자동매매.exe

필요 파일(이 스크립트/exe 옆에 위치):
  .env          : 앱키/시크릿/계좌/텔레그램 토큰·chat_id
  config.yaml   : (선택) 전략·리스크·감시종목. 없으면 내장 기본값 사용
"""
import os
import sys
import json
import time
import queue
import sqlite3
import logging
import threading
import urllib.request
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import requests

try:
    import yaml
except Exception:  # noqa
    yaml = None
try:
    from dotenv import load_dotenv
except Exception:  # noqa
    load_dotenv = None

import tkinter as tk
from tkinter import ttk, messagebox

# ===================================================================== #
#  경로 (스크립트 또는 exe 기준)
# ===================================================================== #
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(BASE, ".env")
CFG_PATH = os.path.join(BASE, "config.yaml")
TOKEN_FILE = os.path.join(BASE, "token.dat")
LOG_DIR = os.path.join(BASE, "logs")
DB_PATH = os.path.join(BASE, "trades.db")

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
TOKEN_REFRESH_SEC = 6 * 3600

# ===================================================================== #
#  내장 기본 설정 (config.yaml 이 있으면 그 값으로 덮어씀)
# ===================================================================== #
DEFAULT_CONFIG = {
    "universe": {
        "domestic": ["005930", "000660", "035720"],
        "overseas": [
            {"symbol": "AAPL", "exchange": "NAS"},
            {"symbol": "TSLA", "exchange": "NAS"},
        ],
    },
    "engine": {
        "loop_interval_sec": 60,
        "domestic_session": "09:00-15:20",
        "overseas_session": "23:30-06:00",
        "auto_trade_enabled": True,
    },
    "strategy": {
        "buy_threshold": 2,
        "sell_threshold": 2,
        "indicators": {
            "ma_cross": {"enabled": True, "short": 5, "long": 20, "weight": 1},
            "rsi": {"enabled": True, "period": 14, "low": 30, "high": 70, "weight": 1},
            "macd": {"enabled": True, "fast": 12, "slow": 26, "signal": 9, "weight": 1},
            "bollinger": {"enabled": True, "period": 20, "num_std": 2.0, "weight": 1},
            "vol_breakout": {"enabled": True, "k": 0.5, "weight": 2},
        },
    },
    "risk": {
        "max_positions": 5,
        "risk_per_trade_pct": 1.0,
        "stop_loss_pct": 5.0,
        "take_profit_pct": 10.0,
        "daily_loss_limit_pct": 3.0,
        "max_drawdown_pct": 15.0,
        "atr_period": 14,
    },
}

# ===================================================================== #
#  로깅
# ===================================================================== #
def get_logger(name="autotrader"):
    os.makedirs(LOG_DIR, exist_ok=True)
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    ch = logging.StreamHandler(); ch.setLevel(logging.INFO); ch.setFormatter(fmt)
    lg.addHandler(ch)
    try:
        fh = RotatingFileHandler(os.path.join(LOG_DIR, "autotrader.log"),
                                 maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG); fh.setFormatter(fmt); lg.addHandler(fh)
    except Exception:  # noqa
        pass
    return lg


log = get_logger("app")

# ===================================================================== #
#  설정 로더
# ===================================================================== #
@dataclass
class Settings:
    mode: str
    app_key: str
    app_secret: str
    account_no: str
    account_prod: str
    telegram_token: str
    allowed_chat_ids: list
    strategy: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)

    @property
    def is_paper(self):
        return self.mode == "paper"

    @property
    def base_url(self):
        return PAPER_BASE_URL if self.is_paper else REAL_BASE_URL


def _read_env_file():
    """python-dotenv 가 없을 때를 대비한 간단한 .env 파서."""
    data = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                # 인라인 주석 제거 (따옴표 밖의 #)
                if v and v[0] not in "\"'":
                    v = v.split("#", 1)[0].strip()
                v = v.strip().strip('"').strip("'")
                data[k.strip()] = v
    return data


def _split_account(raw):
    raw = (raw or "").strip()
    if "-" in raw:
        a, b = raw.split("-", 1)
        return a.strip(), b.strip()
    return raw, "01"


def load_settings():
    if load_dotenv:
        load_dotenv(ENV_PATH, override=True)
    env = _read_env_file()

    def g(key, default=""):
        return os.getenv(key, env.get(key, default))

    mode = (g("TRADING_MODE", "paper") or "paper").lower()
    if mode == "real":
        ak, sk, acc = g("KIS_REAL_APP_KEY"), g("KIS_REAL_APP_SECRET"), g("KIS_REAL_ACCOUNT")
    else:
        mode = "paper"
        ak, sk, acc = g("KIS_PAPER_APP_KEY"), g("KIS_PAPER_APP_SECRET"), g("KIS_PAPER_ACCOUNT")
    no, prod = _split_account(acc)
    chat_raw = g("TELEGRAM_ALLOWED_CHAT_IDS", "")
    allowed = [c.strip() for c in chat_raw.split(",") if c.strip()]

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if yaml and os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                ycfg = yaml.safe_load(f) or {}
            cfg.update(ycfg)
        except Exception as e:  # noqa
            log.warning("config.yaml 파싱 실패, 기본값 사용: %s", e)

    return Settings(mode, ak, sk, no, prod, g("TELEGRAM_BOT_TOKEN"), allowed,
                    cfg.get("strategy", {}), cfg.get("risk", {}),
                    cfg.get("engine", {}), cfg.get("universe", {}))


def update_env(updates):
    """.env 의 특정 키를 갱신/추가 (주석 보존)."""
    lines, seen = [], set()
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                st = line.strip()
                if st and not st.startswith("#") and "=" in st:
                    key = st.split("=", 1)[0].strip()
                    if key in updates:
                        lines.append(f"{key}={updates[key]}\n")
                        seen.add(key)
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def validate(s):
    miss = []
    if not s.app_key or "여기에" in s.app_key:
        miss.append("APP_KEY")
    if not s.app_secret or "여기에" in s.app_secret:
        miss.append("APP_SECRET")
    if not s.account_no or s.account_no.startswith("0000"):
        miss.append("ACCOUNT")
    if not s.telegram_token or "여기에" in s.telegram_token or ":" not in s.telegram_token:
        miss.append("TELEGRAM_BOT_TOKEN")
    if not s.allowed_chat_ids or not all(c.isdigit() for c in s.allowed_chat_ids):
        miss.append("TELEGRAM_ALLOWED_CHAT_IDS(숫자)")
    return miss


# ===================================================================== #
#  텔레그램 chat_id 자동 조회 (getUpdates)
# ===================================================================== #
def fetch_chat_ids(token):
    """봇에게 메시지를 보낸 사용자들의 숫자 chat_id 목록을 반환."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    ids = []
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat", {})
        cid = chat.get("id")
        if cid is not None and cid not in ids:
            ids.append(cid)
    return ids


# ===================================================================== #
#  유량 제어
# ===================================================================== #
class RateLimiter:
    def __init__(self, max_per_sec, min_interval=0.0):
        self.max = max_per_sec
        self.min_interval = min_interval   # 연속 호출 최소 간격(초)
        self.calls = deque()
        self.last = 0.0
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            # 1) 연속 호출 최소 간격 확보
            if self.min_interval:
                gap = time.time() - self.last
                if gap < self.min_interval:
                    time.sleep(self.min_interval - gap)
            # 2) 슬라이딩 윈도우(초당 최대건수)
            now = time.time()
            while self.calls and now - self.calls[0] > 1.0:
                self.calls.popleft()
            if len(self.calls) >= self.max:
                wait = 1.0 - (now - self.calls[0]) + 0.05
                if wait > 0:
                    time.sleep(wait)
                now = time.time()
                while self.calls and now - self.calls[0] > 1.0:
                    self.calls.popleft()
            t = time.time()
            self.calls.append(t)
            self.last = t


# ===================================================================== #
#  KIS API 클라이언트
# ===================================================================== #
class KISApi:
    def __init__(self, s):
        self.s = s
        self.base = s.base_url
        # 모의투자는 유량이 매우 빡빡 → 초당 2건·간격 0.5s. 실전은 초당 15건.
        self.limiter = RateLimiter(2, min_interval=0.5) if s.is_paper \
            else RateLimiter(15, min_interval=0.06)
        self._token = None
        self._issued = 0.0
        self._load_token()

    def _load_token(self):
        if not os.path.exists(TOKEN_FILE):
            return
        try:
            d = json.load(open(TOKEN_FILE, encoding="utf-8"))
            if d.get("mode") == self.s.mode and d.get("app_key") == self.s.app_key \
                    and time.time() - d.get("issued", 0) < TOKEN_REFRESH_SEC:
                self._token = d.get("token")
                self._issued = d.get("issued", 0)
                log.info("저장된 토큰 재사용")
        except Exception:  # noqa
            pass

    def _save_token(self):
        try:
            json.dump({"mode": self.s.mode, "app_key": self.s.app_key,
                       "token": self._token, "issued": self._issued},
                      open(TOKEN_FILE, "w", encoding="utf-8"))
        except Exception:  # noqa
            pass

    def token(self):
        if self._token and time.time() - self._issued < TOKEN_REFRESH_SEC:
            return self._token
        self.limiter.acquire()
        r = requests.post(f"{self.base}/oauth2/tokenP", json={
            "grant_type": "client_credentials",
            "appkey": self.s.app_key, "appsecret": self.s.app_secret}, timeout=10)
        r.raise_for_status()
        self._token = r.json()["access_token"]
        self._issued = time.time()
        self._save_token()
        log.info("신규 토큰 발급")
        return self._token

    def _hashkey(self, body):
        self.limiter.acquire()
        r = requests.post(f"{self.base}/uapi/hashkey",
                          headers={"content-type": "application/json",
                                   "appkey": self.s.app_key, "appsecret": self.s.app_secret},
                          data=json.dumps(body), timeout=10)
        r.raise_for_status()
        return r.json()["HASH"]

    def _headers(self, tr_id, hashkey=None):
        h = {"content-type": "application/json; charset=utf-8",
             "authorization": f"Bearer {self.token()}",
             "appkey": self.s.app_key, "appsecret": self.s.app_secret,
             "tr_id": tr_id, "custtype": "P"}
        if hashkey:
            h["hashkey"] = hashkey
        return h

    def _request(self, method, path, tr_id, params=None, body=None, hashkey=None, attempts=4):
        """유량 초과(EGW00201) 시 백오프 후 자동 재시도. 실패해도 예외 대신 dict 반환."""
        url = f"{self.base}{path}"
        last = {}
        for i in range(attempts):
            self.limiter.acquire()
            try:
                if method == "GET":
                    r = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
                else:
                    r = requests.post(url, headers=self._headers(tr_id, hashkey),
                                      data=json.dumps(body), timeout=10)
            except Exception as e:  # noqa
                log.warning("요청 예외 %s: %s (재시도)", path, e)
                time.sleep(0.5 * (i + 1))
                continue
            try:
                j = r.json()
            except Exception:  # noqa
                j = {}
            blob = f"{j.get('msg_cd', '')}{j.get('msg1', '')}{r.text[:120]}"
            if "EGW00201" in blob or "초당 거래건수" in blob:
                wait = 0.8 * (i + 1)
                log.warning("유량 초과 — %.1fs 후 재시도(%d/%d) %s", wait, i + 1, attempts, path)
                time.sleep(wait)
                last = j
                continue
            if r.status_code != 200:
                log.error("%s %s %s: %s", method, path, r.status_code, r.text[:200])
                r.raise_for_status()
            return j
        log.error("유량 초과 재시도 실패: %s", path)
        return last or {"rt_cd": "1", "msg1": "유량 초과"}

    def _get(self, path, tr_id, params):
        return self._request("GET", path, tr_id, params=params)

    def _post(self, path, tr_id, body):
        hk = self._hashkey(body)
        return self._request("POST", path, tr_id, body=body, hashkey=hk)

    # ---- 국내 ---- #
    def domestic_price(self, code):
        d = self._get("/uapi/domestic-stock/v1/quotations/inquire-price",
                      "FHKST01010100",
                      {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        o = d.get("output", {})
        return {"price": float(o.get("stck_prpr", 0) or 0),
                "open": float(o.get("stck_oprc", 0) or 0),
                "high": float(o.get("stck_hgpr", 0) or 0),
                "low": float(o.get("stck_lwpr", 0) or 0),
                "volume": int(o.get("acml_vol", 0) or 0),
                "change_rate": float(o.get("prdy_ctrt", 0) or 0)}

    def domestic_daily(self, code, count=120):
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=count * 2 + 10)).strftime("%Y%m%d")
        d = self._get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                      "FHKST03010100",
                      {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                       "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end,
                       "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        out = []
        for r in (d.get("output2") or []):
            if not r.get("stck_bsop_date"):
                continue
            out.append({"date": r["stck_bsop_date"],
                        "open": float(r.get("stck_oprc", 0) or 0),
                        "high": float(r.get("stck_hgpr", 0) or 0),
                        "low": float(r.get("stck_lwpr", 0) or 0),
                        "close": float(r.get("stck_clpr", 0) or 0),
                        "volume": int(r.get("acml_vol", 0) or 0)})
        out.reverse()
        return out

    def domestic_order(self, code, qty, side, price=0):
        if side == "buy":
            tr = "VTTC0802U" if self.s.is_paper else "TTTC0802U"
        else:
            tr = "VTTC0801U" if self.s.is_paper else "TTTC0801U"
        body = {"CANO": self.s.account_no, "ACNT_PRDT_CD": self.s.account_prod,
                "PDNO": code, "ORD_DVSN": "01" if price == 0 else "00",
                "ORD_QTY": str(int(qty)), "ORD_UNPR": str(int(price))}
        d = self._post("/uapi/domestic-stock/v1/trading/order-cash", tr, body)
        ok = d.get("rt_cd") == "0"
        log.info("국내주문 %s %s x%d -> %s %s", side, code, qty, d.get("rt_cd"), d.get("msg1"))
        return {"ok": ok, "raw": d, "msg": d.get("msg1", "")}

    def domestic_balance(self):
        tr = "VTTC8434R" if self.s.is_paper else "TTTC8434R"
        d = self._get("/uapi/domestic-stock/v1/trading/inquire-balance", tr,
                      {"CANO": self.s.account_no, "ACNT_PRDT_CD": self.s.account_prod,
                       "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                       "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                       "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
                       "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        pos = []
        for r in (d.get("output1") or []):
            q = int(float(r.get("hldg_qty", 0) or 0))
            if q <= 0:
                continue
            pos.append({"code": r.get("pdno"), "name": r.get("prdt_name"), "qty": q,
                        "avg_price": float(r.get("pchs_avg_pric", 0) or 0),
                        "cur_price": float(r.get("prpr", 0) or 0),
                        "pnl_rate": float(r.get("evlu_pfls_rt", 0) or 0),
                        "pnl_amt": float(r.get("evlu_pfls_amt", 0) or 0)})
        summ = (d.get("output2") or [{}])[0]
        return {"cash": float(summ.get("dnca_tot_amt", 0) or 0),
                "total_eval": float(summ.get("tot_evlu_amt", 0) or 0), "positions": pos}

    # ---- 해외(미국) ---- #
    _OVRS = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}

    def overseas_price(self, sym, exch="NAS"):
        d = self._get("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                      {"AUTH": "", "EXCD": exch, "SYMB": sym})
        o = d.get("output", {})
        return {"price": float(o.get("last", 0) or 0), "open": float(o.get("open", 0) or 0),
                "high": float(o.get("high", 0) or 0), "low": float(o.get("low", 0) or 0),
                "volume": int(float(o.get("tvol", 0) or 0)),
                "change_rate": float(o.get("rate", 0) or 0)}

    def overseas_daily(self, sym, exch="NAS", count=120):
        d = self._get("/uapi/overseas-price/v1/quotations/dailyprice", "HHDFS76240000",
                      {"AUTH": "", "EXCD": exch, "SYMB": sym, "GUBN": "0",
                       "BYMD": "", "MODP": "1"})
        out = []
        for r in (d.get("output2") or []):
            if not r.get("xymd"):
                continue
            out.append({"date": r["xymd"], "open": float(r.get("open", 0) or 0),
                        "high": float(r.get("high", 0) or 0), "low": float(r.get("low", 0) or 0),
                        "close": float(r.get("clos", 0) or 0),
                        "volume": int(float(r.get("tvol", 0) or 0))})
        out.reverse()
        return out

    def overseas_order(self, sym, qty, side, price=0, exch="NAS"):
        if side == "buy":
            tr = "VTTT1002U" if self.s.is_paper else "TTTT1002U"
        else:
            tr = "VTTT1001U" if self.s.is_paper else "TTTT1006U"
        if price <= 0:
            price = self.overseas_price(sym, exch)["price"]
        body = {"CANO": self.s.account_no, "ACNT_PRDT_CD": self.s.account_prod,
                "OVRS_EXCG_CD": self._OVRS.get(exch, "NASD"), "PDNO": sym,
                "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{price:.2f}",
                "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00"}
        d = self._post("/uapi/overseas-stock/v1/trading/order", tr, body)
        ok = d.get("rt_cd") == "0"
        log.info("해외주문 %s %s x%d -> %s %s", side, sym, qty, d.get("rt_cd"), d.get("msg1"))
        return {"ok": ok, "raw": d, "msg": d.get("msg1", "")}


# ===================================================================== #
#  지표 (pandas 없이 순수 파이썬)
# ===================================================================== #
def _sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _ema_series(vals, n):
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains) / n
    al = sum(losses) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def _macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return None, None
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    macd_line = [a - b for a, b in zip(ef, es)]
    sig_line = _ema_series(macd_line, sig)
    return macd_line, sig_line


def _bollinger(closes, n=20, k=2.0):
    if len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((x - mid) ** 2 for x in window) / n
    std = var ** 0.5
    return mid + k * std, mid, mid - k * std


def _atr(candles, n=14):
    if len(candles) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def _vol_breakout_target(candles, k=0.5):
    if len(candles) < 2:
        return None
    return candles[-1]["open"] + (candles[-2]["high"] - candles[-2]["low"]) * k


# ===================================================================== #
#  종합 시그널 전략
# ===================================================================== #
@dataclass
class Signal:
    action: str
    score_buy: float
    score_sell: float
    reasons: list
    price: float


class CompositeStrategy:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.ind = self.cfg.get("indicators", {})
        self.buy_th = self.cfg.get("buy_threshold", 2)
        self.sell_th = self.cfg.get("sell_threshold", 2)

    def evaluate(self, candles, price=None):
        if not candles or len(candles) < 30:
            return Signal("hold", 0, 0, ["데이터 부족"], price or 0)
        closes = [c["close"] for c in candles]
        p = float(price if price else closes[-1])
        buy = sell = 0.0
        reasons = []

        c = self.ind.get("ma_cross", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            s_now, s_prev = _sma(closes, c.get("short", 5)), _sma(closes[:-1], c.get("short", 5))
            l_now, l_prev = _sma(closes, c.get("long", 20)), _sma(closes[:-1], c.get("long", 20))
            if None not in (s_now, s_prev, l_now, l_prev):
                if s_prev <= l_prev and s_now > l_now:
                    buy += w; reasons.append(f"골든크로스 +{w}")
                elif s_prev >= l_prev and s_now < l_now:
                    sell += w; reasons.append(f"데드크로스 +{w}")

        c = self.ind.get("rsi", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            r = _rsi(closes, c.get("period", 14))
            if r is not None:
                if r < c.get("low", 30):
                    buy += w; reasons.append(f"RSI 과매도({r:.0f}) +{w}")
                elif r > c.get("high", 70):
                    sell += w; reasons.append(f"RSI 과매수({r:.0f}) +{w}")

        c = self.ind.get("macd", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            ml, sl = _macd(closes, c.get("fast", 12), c.get("slow", 26), c.get("signal", 9))
            if ml and sl and len(ml) >= 2:
                if ml[-2] <= sl[-2] and ml[-1] > sl[-1]:
                    buy += w; reasons.append(f"MACD 골든 +{w}")
                elif ml[-2] >= sl[-2] and ml[-1] < sl[-1]:
                    sell += w; reasons.append(f"MACD 데드 +{w}")

        c = self.ind.get("bollinger", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            up, mid, lo = _bollinger(closes, c.get("period", 20), c.get("num_std", 2.0))
            if up is not None:
                if p < lo:
                    buy += w; reasons.append(f"볼린저 하단이탈 +{w}")
                elif p > up:
                    sell += w; reasons.append(f"볼린저 상단돌파 +{w}")

        c = self.ind.get("vol_breakout", {})
        if c.get("enabled"):
            w = c.get("weight", 2)
            tgt = _vol_breakout_target(candles, c.get("k", 0.5))
            if tgt and p >= tgt > 0:
                buy += w; reasons.append(f"변동성돌파({tgt:.2f}) +{w}")

        if buy >= self.buy_th and buy > sell:
            act = "buy"
        elif sell >= self.sell_th and sell > buy:
            act = "sell"
        else:
            act = "hold"
        return Signal(act, buy, sell, reasons, p)


# ===================================================================== #
#  리스크 관리
# ===================================================================== #
class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.day_start = None
        self.peak = None
        self.halted = False

    def position_size(self, cash, price, candles=None):
        if price <= 0 or cash <= 0:
            return 0
        risk_amt = cash * (self.cfg.get("risk_per_trade_pct", 1.0) / 100.0)
        stop = price * (self.cfg.get("stop_loss_pct", 5.0) / 100.0)
        if candles:
            a = _atr(candles, self.cfg.get("atr_period", 14))
            if a and a > 0:
                stop = a
        if stop <= 0:
            return 0
        q = int(risk_amt / stop)
        if q < 1:
            q = 1 if price <= risk_amt else 0
        return q

    def should_exit(self, avg, cur):
        if avg <= 0:
            return ""
        pct = (cur - avg) / avg * 100
        if pct <= -self.cfg.get("stop_loss_pct", 5.0):
            return "손절"
        if pct >= self.cfg.get("take_profit_pct", 10.0):
            return "익절"
        return ""

    def can_open(self, n):
        return n < self.cfg.get("max_positions", 5)

    def reset_day(self, eq):
        self.day_start = eq
        self.halted = False

    def check_limits(self, eq):
        if self.day_start is None:
            self.day_start = eq
        if self.peak is None or eq > self.peak:
            self.peak = eq
        if self.day_start and self.day_start > 0:
            if (eq - self.day_start) / self.day_start * 100 <= -self.cfg.get("daily_loss_limit_pct", 3.0):
                self.halted = True
                return "일일손실한도"
        if self.peak and self.peak > 0:
            if (eq - self.peak) / self.peak * 100 <= -self.cfg.get("max_drawdown_pct", 15.0):
                self.halted = True
                return "MDD한도"
        return ""


# ===================================================================== #
#  매매일지 (SQLite) + 리포트
# ===================================================================== #
class TradeJournal:
    def __init__(self, path=DB_PATH):
        self.path = path
        self.lock = threading.Lock()
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with self.lock, self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS trades(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, date TEXT, mode TEXT, market TEXT,
                    code TEXT, name TEXT, side TEXT, qty INTEGER,
                    price REAL, amount REAL, reason TEXT,
                    pnl REAL, ok INTEGER, msg TEXT)
            """)

    def log(self, mode, market, code, name, side, qty, price, reason="", pnl=None, ok=True, msg=""):
        now = datetime.now()
        try:
            with self.lock, self._conn() as c:
                c.execute(
                    "INSERT INTO trades(ts,date,mode,market,code,name,side,qty,price,amount,reason,pnl,ok,msg)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"),
                     mode, market, code, name or code, side, int(qty), float(price),
                     float(qty) * float(price), reason, pnl,
                     1 if ok else 0, msg))
        except Exception as e:  # noqa
            log.warning("매매일지 기록 실패: %s", e)

    def recent(self, limit=300):
        try:
            with self.lock, self._conn() as c:
                rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa
            return []

    def summary(self, start_date, end_date):
        """기간(YYYY-MM-DD) 집계."""
        try:
            with self.lock, self._conn() as c:
                rows = c.execute(
                    "SELECT * FROM trades WHERE date>=? AND date<=? AND ok=1",
                    (start_date, end_date)).fetchall()
        except Exception:  # noqa
            rows = []
        buys = [r for r in rows if r["side"] == "buy"]
        sells = [r for r in rows if r["side"] == "sell"]
        pnls = [r["pnl"] for r in sells if r["pnl"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        return {
            "trades": len(rows), "buys": len(buys), "sells": len(sells),
            "buy_amount": sum(r["amount"] for r in buys),
            "sell_amount": sum(r["amount"] for r in sells),
            "realized_pnl": sum(pnls) if pnls else 0.0,
            "wins": len(wins), "losses": len(losses),
            "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        }

    def daily_pnl(self, days=14):
        """최근 days일 일별 실현손익 [(date, pnl), ...]."""
        out = []
        today = datetime.now().date()
        try:
            with self.lock, self._conn() as c:
                for i in range(days - 1, -1, -1):
                    d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
                    row = c.execute(
                        "SELECT COALESCE(SUM(pnl),0) p FROM trades WHERE date=? AND side='sell' AND ok=1",
                        (d,)).fetchone()
                    out.append((d[5:], row["p"] or 0.0))
        except Exception:  # noqa
            pass
        return out


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def week_start_str():
    return (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")


# ===================================================================== #
#  매매 엔진
# ===================================================================== #
class Trader:
    def __init__(self, s, notify=None, journal=None):
        self.s = s
        self.api = KISApi(s)
        self.strategy = CompositeStrategy(s.strategy)
        self.risk = RiskManager(s.risk)
        self.journal = journal or TradeJournal()
        self.notify = notify or (lambda m: None)
        self.auto_enabled = s.engine.get("auto_trade_enabled", True)
        self.running = False
        self._stop = threading.Event()
        self._thread = None
        self._scan_lock = threading.Lock()   # 동시 스캔 방지(유량 절약)
        self.last_signals = {}

    def _dom(self):
        return self.s.universe.get("domestic", []) or []

    def _ovs(self):
        return self.s.universe.get("overseas", []) or []

    def safe_balance(self):
        try:
            return self.api.domestic_balance()
        except Exception as e:  # noqa
            log.warning("잔고조회 실패: %s", e)
            return {"cash": 0, "total_eval": 0, "positions": []}

    def _report(self, kind, market, code, name, side, qty, price, reason, res, pnl=None):
        ok = res.get("ok")
        st = "✅성공" if ok else f"❌실패({res.get('msg')})"
        extra = f" | 실현손익 {pnl:+,.0f}" if pnl is not None else ""
        m = f"{kind} {code} {qty}주 @{price:,.2f}{extra}\n사유: {reason}\n결과: {st}"
        self.notify(m)
        log.info(m.replace("\n", " | "))
        self.journal.log(self.s.mode, market, code, name, side, qty, price,
                         reason, pnl, ok, res.get("msg", ""))

    def _proc_dom(self, code, bal):
        try:
            candles = self.api.domestic_daily(code)
            q = self.api.domestic_price(code)
            sig = self.strategy.evaluate(candles, q["price"])
            self.last_signals[code] = sig
            pos = next((p for p in bal["positions"] if p["code"] == code), None)
            if pos:
                why = self.risk.should_exit(pos["avg_price"], q["price"])
                if why or sig.action == "sell":
                    why = why or "전략매도"
                    if self.auto_enabled:
                        r = self.api.domestic_order(code, pos["qty"], "sell")
                        pnl = (q["price"] - pos["avg_price"]) * pos["qty"]
                        self._report("매도", "domestic", code, pos.get("name", code),
                                     "sell", pos["qty"], q["price"], why, r, pnl=pnl)
                    else:
                        self.notify(f"📉[신호] {code} 매도추천({why}) — 자동OFF")
                    return
            if sig.action == "buy" and not pos:
                if not self.risk.can_open(len(bal["positions"])):
                    return
                qty = self.risk.position_size(bal.get("cash", 0), q["price"], candles)
                if qty < 1:
                    return
                if self.auto_enabled:
                    r = self.api.domestic_order(code, qty, "buy")
                    self._report("매수", "domestic", code, code, "buy", qty,
                                 q["price"], ", ".join(sig.reasons), r)
                else:
                    self.notify(f"📈[신호] {code} 매수추천 {qty}주({', '.join(sig.reasons)}) — 자동OFF")
        except Exception as e:  # noqa
            log.exception("국내 처리오류 %s", code)
            self.notify(f"⚠️ {code} 오류: {e}")

    def _proc_ovs(self, item, bal):
        sym, exch = item.get("symbol"), item.get("exchange", "NAS")
        try:
            candles = self.api.overseas_daily(sym, exch)
            q = self.api.overseas_price(sym, exch)
            sig = self.strategy.evaluate(candles, q["price"])
            self.last_signals[sym] = sig
            if sig.action == "buy":
                if self.auto_enabled:
                    r = self.api.overseas_order(sym, 1, "buy", q["price"], exch)
                    self._report("매수(美)", "overseas", sym, sym, "buy", 1,
                                 q["price"], ", ".join(sig.reasons), r)
                else:
                    self.notify(f"📈[신호] {sym} 매수추천({', '.join(sig.reasons)}) — 자동OFF")
        except Exception as e:  # noqa
            log.exception("해외 처리오류 %s", sym)
            self.notify(f"⚠️ {sym} 오류: {e}")

    def scan_once(self):
        # 이미 다른 스캔이 진행 중이면 건너뜀(유량 초과 방지)
        if not self._scan_lock.acquire(blocking=False):
            log.info("스캔 이미 진행 중 — 이번 호출은 건너뜁니다.")
            return
        try:
            bal = self.safe_balance()
            eq = bal.get("total_eval") or bal.get("cash", 0)
            lim = self.risk.check_limits(eq)
            if lim:
                self.notify(f"🛑 {lim} 도달 — 매매 중단. 필요시 전체청산하세요.")
                return
            for code in self._dom():
                self._proc_dom(code, bal)
            for it in self._ovs():
                self._proc_ovs(it, bal)
        finally:
            self._scan_lock.release()

    def start(self):
        if self.running:
            return False
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.running = False
        self._stop.set()

    def _in_session(self):
        now = datetime.now().strftime("%H:%M")

        def within(rng):
            try:
                a, b = rng.split("-")
                return (a <= now <= b) if a <= b else (now >= a or now <= b)
            except Exception:  # noqa
                return True
        return within(self.s.engine.get("domestic_session", "09:00-15:20")) or \
            within(self.s.engine.get("overseas_session", "23:30-06:00"))

    def _loop(self):
        iv = self.s.engine.get("loop_interval_sec", 60)
        self.notify(f"▶️ 자동매매 루프 시작 (모드 {self.s.mode}, {iv}s)")
        while not self._stop.is_set():
            try:
                if self._in_session():
                    self.scan_once()
            except Exception:  # noqa
                log.exception("루프 오류")
            self._stop.wait(iv)
        self.notify("⏹️ 자동매매 루프 종료")

    def _price_of(self, code, ovs, exch):
        try:
            return (self.api.overseas_price(code, exch) if ovs
                    else self.api.domestic_price(code))["price"]
        except Exception:  # noqa
            return 0.0

    def manual_buy(self, code, qty, ovs=False, exch="NAS"):
        r = self.api.overseas_order(code, qty, "buy", 0, exch) if ovs \
            else self.api.domestic_order(code, qty, "buy")
        self.journal.log(self.s.mode, "overseas" if ovs else "domestic", code, code,
                         "buy", qty, self._price_of(code, ovs, exch), "수동매수",
                         None, r.get("ok"), r.get("msg", ""))
        return r

    def manual_sell(self, code, qty, ovs=False, exch="NAS"):
        # 실현손익 계산용 평단 조회(국내)
        avg = 0.0
        if not ovs:
            for p in self.safe_balance()["positions"]:
                if p["code"] == code:
                    avg = p["avg_price"]
                    break
        price = self._price_of(code, ovs, exch)
        r = self.api.overseas_order(code, qty, "sell", 0, exch) if ovs \
            else self.api.domestic_order(code, qty, "sell")
        pnl = (price - avg) * qty if (avg and price) else None
        self.journal.log(self.s.mode, "overseas" if ovs else "domestic", code, code,
                         "sell", qty, price, "수동매도", pnl, r.get("ok"), r.get("msg", ""))
        return r

    def liquidate_all(self):
        bal = self.safe_balance()
        res = []
        for p in bal["positions"]:
            r = self.api.domestic_order(p["code"], p["qty"], "sell")
            pnl = (p["cur_price"] - p["avg_price"]) * p["qty"]
            self.journal.log(self.s.mode, "domestic", p["code"], p.get("name", p["code"]),
                             "sell", p["qty"], p["cur_price"], "전체청산", pnl,
                             r.get("ok"), r.get("msg", ""))
            res.append((p["code"], r.get("ok")))
        return res

    def portfolio_report(self):
        bal = self.safe_balance()
        L = [f"💼 포트폴리오({self.s.mode})", f"예수금 {bal['cash']:,.0f}원",
             f"총평가 {bal['total_eval']:,.0f}원"]
        if not bal["positions"]:
            L.append("보유 종목 없음")
        for p in bal["positions"]:
            L.append(f"• {p['name']}({p['code']}) {p['qty']}주 평단 {p['avg_price']:,.0f} "
                     f"현재 {p['cur_price']:,.0f} ({p['pnl_rate']:+.2f}%)")
        return "\n".join(L)


# ===================================================================== #
#  텔레그램 봇 (선택 — 라이브러리 있을 때만)
# ===================================================================== #
class TelegramController:
    def __init__(self, s, trader):
        self.s = s
        self.trader = trader
        self.allowed = set(str(c) for c in s.allowed_chat_ids)
        self.app = None          # run 스레드 내부에서 생성(이벤트 루프 바인딩 일치)
        self._loop = None
        self._thread = None

    def _build_app(self):
        from telegram.ext import ApplicationBuilder
        self.app = ApplicationBuilder().token(self.s.telegram_token).build()
        self._register()

    def _register(self):
        from telegram.ext import CommandHandler
        h = self.app.add_handler
        h(CommandHandler(["start", "help"], self.c_help))
        h(CommandHandler("status", self.c_status))
        h(CommandHandler(["balance", "portfolio"], self.c_port))
        h(CommandHandler("auto", self.c_auto))
        h(CommandHandler("run", self.c_run))
        h(CommandHandler("signals", self.c_sig))
        h(CommandHandler("buy", self.c_buy))
        h(CommandHandler("sell", self.c_sell))
        h(CommandHandler("liquidate", self.c_liq))

    @property
    def is_running(self):
        return self._loop is not None and self._thread and self._thread.is_alive()

    def broadcast(self, msg):
        import asyncio
        if self._loop is None or self.app is None:
            return
        for cid in self.allowed:
            asyncio.run_coroutine_threadsafe(
                self.app.bot.send_message(chat_id=int(cid), text=msg), self._loop)

    async def _ok(self, update):
        cid = str(update.effective_chat.id)
        if cid not in self.allowed:
            await update.message.reply_text(f"⛔ 권한 없음. 당신의 chat_id: {cid}")
            return False
        return True

    async def c_help(self, u, c):
        if not await self._ok(u):
            return
        await u.message.reply_text(
            "🤖 명령어\n/status /portfolio\n/auto on|off\n/run /signals\n"
            "/buy 005930 10  | /buy AAPL 1 us NAS\n/sell ...\n/liquidate\n"
            f"모드: {self.s.mode}")

    async def c_status(self, u, c):
        if not await self._ok(u):
            return
        t = self.trader
        await u.message.reply_text(
            f"모드 {self.s.mode}\n루프 {'가동' if t.running else '정지'}\n"
            f"자동매매 {'ON' if t.auto_enabled else 'OFF'}\n중단 {'예' if t.risk.halted else '아니오'}")

    async def c_port(self, u, c):
        import asyncio
        if not await self._ok(u):
            return
        r = await asyncio.to_thread(self.trader.portfolio_report)
        await u.message.reply_text(r)

    async def c_auto(self, u, c):
        if not await self._ok(u):
            return
        a = (c.args[0].lower() if c.args else "")
        if a == "on":
            self.trader.auto_enabled = True
            await u.message.reply_text("✅ 자동매매 ON")
        elif a == "off":
            self.trader.auto_enabled = False
            await u.message.reply_text("⏸️ 자동매매 OFF")
        else:
            await u.message.reply_text("/auto on 또는 /auto off")

    async def c_run(self, u, c):
        import asyncio
        if not await self._ok(u):
            return
        await u.message.reply_text("🔍 스캔...")
        await asyncio.to_thread(self.trader.scan_once)
        await u.message.reply_text("완료")

    async def c_sig(self, u, c):
        if not await self._ok(u):
            return
        sg = self.trader.last_signals
        if not sg:
            await u.message.reply_text("시그널 없음. /run 실행")
            return
        L = ["📡 시그널"]
        for code, s in sg.items():
            e = {"buy": "📈", "sell": "📉", "hold": "⏸"}.get(s.action, "")
            L.append(f"{e} {code}: {s.action} (매수{s.score_buy:.0f}/매도{s.score_sell:.0f})")
        await u.message.reply_text("\n".join(L))

    def _parse(self, args):
        if len(args) < 2:
            return None
        ovs = len(args) >= 3 and args[2].lower() == "us"
        exch = args[3].upper() if ovs and len(args) >= 4 else "NAS"
        return args[0].upper(), int(args[1]), ovs, exch

    async def c_buy(self, u, c):
        import asyncio
        if not await self._ok(u):
            return
        p = self._parse(c.args)
        if not p:
            await u.message.reply_text("/buy 005930 10  또는  /buy AAPL 1 us NAS")
            return
        r = await asyncio.to_thread(self.trader.manual_buy, *p)
        await u.message.reply_text(f"매수 {p[0]} → {'✅' if r.get('ok') else '❌'+r.get('msg','')}")

    async def c_sell(self, u, c):
        import asyncio
        if not await self._ok(u):
            return
        p = self._parse(c.args)
        if not p:
            await u.message.reply_text("/sell 005930 10")
            return
        r = await asyncio.to_thread(self.trader.manual_sell, *p)
        await u.message.reply_text(f"매도 {p[0]} → {'✅' if r.get('ok') else '❌'+r.get('msg','')}")

    async def c_liq(self, u, c):
        import asyncio
        if not await self._ok(u):
            return
        await u.message.reply_text("⚠️ 전체청산...")
        res = await asyncio.to_thread(self.trader.liquidate_all)
        await u.message.reply_text("결과\n" + ("\n".join(f"{a}:{'✅' if b else '❌'}" for a, b in res) or "보유없음"))

    async def _post_init(self, app):
        import asyncio
        self._loop = asyncio.get_running_loop()
        for cid in self.allowed:
            try:
                await app.bot.send_message(chat_id=int(cid),
                                           text=f"🚀 자동매매 봇 가동(모드 {self.s.mode}). /help")
            except Exception:  # noqa
                pass

    def start_in_thread(self):
        import asyncio
        from telegram import Update
        if self.is_running:
            return False

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._build_app()                 # 루프 내부에서 생성 → 루프 바인딩 일치
            self.app.post_init = self._post_init
            try:
                self.app.run_polling(allowed_updates=Update.ALL_TYPES,
                                     stop_signals=None, close_loop=False)
            except Exception:  # noqa
                log.exception("봇 스레드 오류")
            finally:
                self._loop = None
        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        import asyncio
        try:
            if self._loop and self.app.running:
                asyncio.run_coroutine_threadsafe(self.app.stop(), self._loop)
        except Exception:  # noqa
            pass
        self._loop = None


# ===================================================================== #
#  GUI
# ===================================================================== #
# 팔레트 (다크, 순흑/순백 회피 — 웹검색 디자인 가이드 반영)
BG = "#181825"        # 메인 배경 (순흑 X)
BG2 = "#11111b"       # 더 어두운 영역(로그 등)
PANEL = "#1e1e2e"     # 패널
CARD = "#313244"      # 카드
FG = "#cdd6f4"        # 본문 텍스트 (순백 X)
SUB = "#9399b2"       # 보조 텍스트
ACCENT = "#89b4fa"    # 강조(블루)
GREEN = "#a6e3a1"     # 상승/이익
RED = "#f38ba8"       # 하락/손실
YELLOW = "#f9e2af"    # 경고/중립
MAUVE = "#cba6f7"     # 보조 강조
FONT = "맑은 고딕"


class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:  # noqa
            pass


class TradingApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.trader = None
        self.bot = None
        self.journal = TradeJournal()
        self.settings = load_settings()
        root.title("한국투자증권 자동매매 대시보드")
        root.geometry("1040x740")
        root.configure(bg=BG)
        self._build()
        self._attach_log()
        root.after(200, self._poll)
        self._refresh()
        # 시작 시 매매일지/리포트 자동 로딩
        self.root.after(400, self.refresh_journal)
        self.root.after(500, lambda: self.show_report("daily"))

    def _attach_log(self):
        h = QueueLogHandler(self.q)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                         datefmt="%H:%M:%S"))
        h.setLevel(logging.INFO)
        logging.getLogger("app").addHandler(h)

    def _build(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:  # noqa
            pass
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=SUB,
                     padding=(16, 8), font=(FONT, 10, "bold"), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", CARD)],
               foreground=[("selected", ACCENT)])
        st.configure("Treeview", background=PANEL, foreground=FG, fieldbackground=PANEL,
                     rowheight=26, borderwidth=0, font=(FONT, 9))
        st.configure("Treeview.Heading", background=BG2, foreground=ACCENT,
                     font=(FONT, 9, "bold"), relief="flat")
        st.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#11111b")])

        # 헤더 바
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=16, pady=(12, 8))
        title = tk.Label(top, text="📈 자동매매 대시보드", bg=BG, fg=FG,
                         font=(FONT, 14, "bold"))
        title.pack(side="left")
        self.dot = tk.Label(top, text="●", bg=BG, fg=GREEN, font=(FONT, 13))
        self.dot.pack(side="left", padx=(16, 4))
        self.status = tk.Label(top, text="준비", bg=BG, fg=SUB, font=(FONT, 10, "bold"))
        self.status.pack(side="left")
        self.b_bot = self._b(top, "🤖 봇 시작", self.toggle_bot, CARD)
        self.b_scan = self._b(top, "🔍 즉시 스캔", self.scan_now, CARD)
        self.b_auto = self._b(top, "자동매매: --", self.toggle_auto, CARD)
        self.b_eng = self._b(top, "▶ 엔진 시작", self.toggle_engine, ACCENT, dark=True)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self._tab_dash()
        self._tab_journal()
        self._tab_report()
        self._tab_sig()
        self._tab_manual()
        self._tab_settings()
        self._tab_log()
        self.bar = tk.Label(self.root, text="", bg=BG2, fg=SUB, anchor="w",
                            font=(FONT, 8), padx=10, pady=3)
        self.bar.pack(fill="x", side="bottom")

    def _b(self, parent, text, cmd, color, dark=False):
        b = tk.Button(parent, text=text, command=cmd, bg=color,
                      fg=("#11111b" if dark else FG), relief="flat",
                      padx=12, pady=5, font=(FONT, 9, "bold"), cursor="hand2",
                      activebackground=ACCENT, bd=0)
        b.pack(side="right", padx=4)
        return b

    def _card(self, parent, title):
        """지표 카드 위젯 생성. (frame, value_label) 반환."""
        c = tk.Frame(parent, bg=CARD, padx=16, pady=12)
        c.pack(side="left", expand=True, fill="both", padx=6)
        tk.Label(c, text=title, bg=CARD, fg=SUB, font=(FONT, 9)).pack(anchor="w")
        v = tk.Label(c, text="-", bg=CARD, fg=FG, font=(FONT, 16, "bold"))
        v.pack(anchor="w", pady=(4, 0))
        return c, v

    def _tab_dash(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📊 대시보드")

        # 상단 카드 4개
        cards = tk.Frame(f, bg=BG)
        cards.pack(fill="x", pady=(12, 6), padx=4)
        _, self.c_cash = self._card(cards, "💰 예수금(주문가능)")
        _, self.c_eval = self._card(cards, "📊 총평가금액")
        _, self.c_pnl = self._card(cards, "📈 평가손익")
        _, self.c_cnt = self._card(cards, "📦 보유 종목")

        head = tk.Frame(f, bg=BG)
        head.pack(fill="x", pady=(8, 2), padx=8)
        tk.Label(head, text="보유 종목 (모의투자 계좌)", bg=BG, fg=FG,
                 font=(FONT, 11, "bold")).pack(side="left")
        tk.Button(head, text="↻ 새로고침", command=self.refresh_port, bg=CARD, fg=FG,
                  relief="flat", cursor="hand2", padx=10, pady=3, bd=0).pack(side="right")

        cols = ("종목", "수량", "평단가", "현재가", "평가손익", "수익률")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=12)
        widths = (200, 70, 110, 110, 130, 90)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c)
            self.tree.column(c, anchor="center", width=w)
        self.tree.tag_configure("pos", foreground=GREEN)
        self.tree.tag_configure("neg", foreground=RED)
        self.tree.tag_configure("odd", background=BG2)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        # 포트폴리오 비중 막대
        self.alloc = tk.Canvas(f, bg=PANEL, height=70, highlightthickness=0)
        self.alloc.pack(fill="x", padx=8, pady=(0, 10))

    def _tab_journal(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📒 매매일지")
        head = tk.Frame(f, bg=BG)
        head.pack(fill="x", pady=(12, 2), padx=8)
        tk.Label(head, text="매매일지 (전체 주문 기록)", bg=BG, fg=FG,
                 font=(FONT, 11, "bold")).pack(side="left")
        tk.Button(head, text="↻ 새로고침", command=self.refresh_journal, bg=CARD, fg=FG,
                  relief="flat", cursor="hand2", padx=10, pady=3, bd=0).pack(side="right")
        cols = ("시각", "시장", "종목", "구분", "수량", "가격", "금액", "실현손익", "사유", "결과")
        widths = (135, 60, 90, 50, 55, 90, 110, 110, 130, 55)
        self.jtree = ttk.Treeview(f, columns=cols, show="headings", height=18)
        for c, w in zip(cols, widths):
            self.jtree.heading(c, text=c)
            self.jtree.column(c, anchor="center", width=w)
        self.jtree.tag_configure("pos", foreground=GREEN)
        self.jtree.tag_configure("neg", foreground=RED)
        self.jtree.tag_configure("odd", background=BG2)
        self.jtree.pack(fill="both", expand=True, padx=8, pady=8)

    def _tab_report(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📈 리포트")
        btns = tk.Frame(f, bg=BG)
        btns.pack(fill="x", pady=(12, 4), padx=8)
        tk.Label(btns, text="실적 리포트", bg=BG, fg=FG, font=(FONT, 11, "bold")).pack(side="left")
        tk.Button(btns, text="📅 주간(최근 7일)", command=lambda: self.show_report("weekly"),
                  bg=CARD, fg=FG, relief="flat", cursor="hand2", padx=10, pady=3, bd=0).pack(side="right", padx=4)
        tk.Button(btns, text="📆 오늘", command=lambda: self.show_report("daily"),
                  bg=ACCENT, fg="#11111b", relief="flat", cursor="hand2", padx=10, pady=3, bd=0).pack(side="right", padx=4)

        self.rep_title = tk.Label(f, text="", bg=BG, fg=ACCENT, font=(FONT, 12, "bold"))
        self.rep_title.pack(anchor="w", padx=12, pady=(6, 2))

        cards = tk.Frame(f, bg=BG)
        cards.pack(fill="x", pady=6, padx=4)
        _, self.r_trades = self._card(cards, "총 거래수")
        _, self.r_pnl = self._card(cards, "실현손익")
        _, self.r_win = self._card(cards, "승률")
        _, self.r_amt = self._card(cards, "매수/매도 금액")

        tk.Label(f, text="일별 실현손익 (최근 14일)", bg=BG, fg=SUB,
                 font=(FONT, 9)).pack(anchor="w", padx=12, pady=(10, 0))
        self.chart = tk.Canvas(f, bg=PANEL, height=220, highlightthickness=0)
        self.chart.pack(fill="both", expand=True, padx=8, pady=8)

    def _tab_sig(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📡 시그널")
        tk.Button(f, text="↻ 스캔", command=self.scan_now, bg=PANEL, fg=FG,
                  relief="flat", cursor="hand2").pack(anchor="e", padx=8, pady=6)
        cols = ("종목", "판정", "매수점수", "매도점수", "가격")
        self.sig_tree = ttk.Treeview(f, columns=cols, show="headings", height=15)
        for c in cols:
            self.sig_tree.heading(c, text=c)
            self.sig_tree.column(c, anchor="center", width=130)
        self.sig_tree.pack(fill="both", expand=True, padx=8, pady=8)

    def _tab_manual(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="🛒 수동 주문")
        box = tk.Frame(f, bg=PANEL)
        box.pack(padx=20, pady=20, fill="x")

        def row(lbl):
            r = tk.Frame(box, bg=PANEL)
            r.pack(fill="x", pady=6, padx=14)
            tk.Label(r, text=lbl, bg=PANEL, fg=FG, width=10, anchor="w").pack(side="left")
            return r
        r = row("종목코드")
        self.m_code = tk.Entry(r, width=18)
        self.m_code.pack(side="left")
        tk.Label(r, text="(국내 6자리 / 미국 심볼)", bg=PANEL, fg="#888").pack(side="left", padx=8)
        r = row("수량")
        self.m_qty = tk.Entry(r, width=18)
        self.m_qty.insert(0, "1")
        self.m_qty.pack(side="left")
        r = row("시장")
        self.m_mkt = ttk.Combobox(r, values=["국내", "미국"], width=8, state="readonly")
        self.m_mkt.set("국내")
        self.m_mkt.pack(side="left")
        self.m_exch = ttk.Combobox(r, values=["NAS", "NYS", "AMS"], width=6, state="readonly")
        self.m_exch.set("NAS")
        self.m_exch.pack(side="left", padx=8)
        r = tk.Frame(box, bg=PANEL)
        r.pack(fill="x", pady=14, padx=14)
        tk.Button(r, text="매수", command=lambda: self.manual("buy"), bg=RED, fg="#000",
                  relief="flat", width=12, font=("맑은 고딕", 11, "bold"), cursor="hand2").pack(side="left", padx=6)
        tk.Button(r, text="매도", command=lambda: self.manual("sell"), bg=ACCENT, fg="#000",
                  relief="flat", width=12, font=("맑은 고딕", 11, "bold"), cursor="hand2").pack(side="left", padx=6)
        tk.Button(r, text="⚠ 국내 전체청산", command=self.liquidate, bg="#888", fg="#000",
                  relief="flat", width=16, cursor="hand2").pack(side="right", padx=6)

    def _tab_settings(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="⚙ 설정")
        box = tk.Frame(f, bg=PANEL)
        box.pack(padx=20, pady=20, fill="x")

        def row(lbl):
            r = tk.Frame(box, bg=PANEL)
            r.pack(fill="x", pady=8, padx=14)
            tk.Label(r, text=lbl, bg=PANEL, fg=FG, width=20, anchor="w").pack(side="left")
            return r
        s = self.settings
        r = row("거래 모드")
        self.set_mode = ttk.Combobox(r, values=["paper", "real"], width=12, state="readonly")
        self.set_mode.set(s.mode)
        self.set_mode.pack(side="left")
        tk.Label(r, text="(paper=모의 / real=실전)", bg=PANEL, fg="#888").pack(side="left", padx=8)
        r = row("텔레그램 chat_id")
        self.set_chat = tk.Entry(r, width=30)
        self.set_chat.insert(0, ",".join(s.allowed_chat_ids))
        self.set_chat.pack(side="left")
        tk.Button(r, text="🔎 내 chat_id 자동 찾기", command=self.find_chat_id, bg=ACCENT,
                  fg="#000", relief="flat", cursor="hand2").pack(side="left", padx=8)
        r = row("텔레그램 봇 토큰")
        self.set_token = tk.Entry(r, width=46, show="•")
        self.set_token.insert(0, s.telegram_token)
        self.set_token.pack(side="left")
        tk.Label(box, text="※ 앱키/시크릿/계좌는 보안상 .env 파일에서 직접 수정하세요.",
                 bg=PANEL, fg="#888", anchor="w").pack(fill="x", padx=14, pady=(4, 0))
        tk.Button(box, text="💾 저장 (.env 갱신)", command=self.save_settings, bg=GREEN, fg="#000",
                  relief="flat", font=("맑은 고딕", 10, "bold"), cursor="hand2").pack(pady=12)
        self.cfg_status = tk.Label(box, text="", bg=PANEL, fg=FG, justify="left", anchor="w")
        self.cfg_status.pack(fill="x", padx=14, pady=6)
        self._cfg_status()

    def _tab_log(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📜 로그")
        self.log_txt = tk.Text(f, bg="#11111b", fg="#cdd6f4", font=("Consolas", 9), wrap="word")
        self.log_txt.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_txt.configure(state="disabled")

    # ---- chat_id 자동 찾기 ---- #
    def find_chat_id(self):
        token = self.set_token.get().strip()
        if ":" not in token:
            messagebox.showwarning("토큰 필요", "먼저 올바른 봇 토큰을 입력하세요.")
            return
        messagebox.showinfo("안내", "텔레그램에서 본인 봇에게 아무 메시지(/start 등)를 먼저 보낸 뒤 확인을 누르세요.")

        def work():
            try:
                ids = fetch_chat_ids(token)
                if ids:
                    self.q.put(("chatid", ",".join(str(i) for i in ids)))
                else:
                    self.q.put(("popup", ("결과 없음",
                                          "봇에게 보낸 메시지가 없습니다.\n텔레그램에서 봇에게 /start 를 보낸 뒤 다시 시도하세요.")))
            except Exception as e:  # noqa
                self.q.put(("popup", ("오류", f"chat_id 조회 실패:\n{e}")))
        threading.Thread(target=work, daemon=True).start()

    def _cfg_status(self):
        miss = validate(self.settings)
        if miss:
            self.cfg_status.config(text="⚠ 누락/오류: " + ", ".join(miss), fg=RED)
        else:
            self.cfg_status.config(text="✅ 필수 설정 모두 정상", fg=GREEN)

    def save_settings(self):
        try:
            new_mode = self.set_mode.get()
            # 실전 전환 시 강력 경고
            if new_mode == "real" and self.settings.mode != "real":
                if not messagebox.askyesno(
                        "⚠️ 실전투자 전환",
                        "real(실전) 모드는 실제 돈으로 자동 주문이 나갑니다.\n"
                        "검증되지 않은 전략은 손실 위험이 큽니다.\n정말 전환하시겠습니까?"):
                    self.set_mode.set(self.settings.mode)
                    return
            update_env({"TRADING_MODE": new_mode,
                        "TELEGRAM_ALLOWED_CHAT_IDS": self.set_chat.get().strip(),
                        "TELEGRAM_BOT_TOKEN": self.set_token.get().strip()})
            # 실행 중인 엔진/봇을 정지하고 재생성해야 새 모드(서버)가 적용됨
            if self.trader and self.trader.running:
                self.trader.stop()
            if self.bot and self.bot.is_running:
                self.bot.stop()
            self.trader = None
            self.bot = None
            self.settings = load_settings()
            self.b_eng.config(text="▶ 엔진 시작", bg=ACCENT)
            self.b_bot.config(text="🤖 봇 시작", bg=CARD)
            self._cfg_status()
            self._refresh()
            messagebox.showinfo(
                "저장 완료",
                f"저장되었습니다. 현재 모드: {self.settings.mode}\n"
                f"적용하려면 ‘엔진 시작’(필요시 ‘봇 시작’)을 다시 누르세요.")
            self.q.put(("log", f"설정 저장됨 (모드={self.settings.mode}) — 엔진 재시작 필요"))
        except Exception as e:  # noqa
            messagebox.showerror("저장 실패", str(e))

    # ---- 엔진/봇 ---- #
    def _ensure(self):
        if self.trader is None:
            self.trader = Trader(self.settings, notify=self._notify, journal=self.journal)
        return self.trader

    # ---- 매매일지 / 리포트 ---- #
    def refresh_journal(self):
        threading.Thread(target=lambda: self.q.put(("journal", self.journal.recent(300))),
                         daemon=True).start()

    def show_report(self, period):
        def work():
            if period == "weekly":
                start, title = week_start_str(), "주간 리포트 (최근 7일)"
            else:
                start, title = today_str(), "일일 리포트 (오늘)"
            summ = self.journal.summary(start, today_str())
            chart = self.journal.daily_pnl(14)
            self.q.put(("report", (title, summ, chart)))
        threading.Thread(target=work, daemon=True).start()

    def toggle_engine(self):
        miss = validate(self.settings)
        if "APP_KEY" in miss or "ACCOUNT" in miss:
            messagebox.showwarning("설정 필요", "앱키/계좌를 .env 에 먼저 입력하세요.")
            return
        t = self._ensure()
        if t.running:
            t.stop()
            self.b_eng.config(text="▶ 엔진 시작", bg=ACCENT)
        else:
            t.start()
            self.b_eng.config(text="⏹ 엔진 중지", bg=RED)
        self._refresh()

    def toggle_auto(self):
        self._ensure().auto_enabled = not self._ensure().auto_enabled
        self._refresh()

    def scan_now(self):
        t = self._ensure()
        self.q.put(("log", "즉시 스캔..."))
        threading.Thread(target=self._scan_w, args=(t,), daemon=True).start()

    def _scan_w(self, t):
        try:
            t.scan_once()
            self.q.put(("signals", dict(t.last_signals)))
            self.q.put(("log", "스캔 완료"))
            self.refresh_journal()
            self.show_report("daily")
        except Exception as e:  # noqa
            self.q.put(("log", f"스캔 오류: {e}"))

    def toggle_bot(self):
        miss = validate(self.settings)
        if "TELEGRAM_BOT_TOKEN" in miss or any("chat_id" in m or "CHAT" in m for m in miss):
            messagebox.showwarning("설정 필요",
                                   "설정 탭에서 봇 토큰과 숫자 chat_id를 입력·저장하세요.\n"
                                   "(chat_id는 '내 chat_id 자동 찾기' 버튼 사용)")
            return
        try:
            import telegram  # noqa
        except Exception:
            messagebox.showerror("패키지 없음", "python-telegram-bot 미설치.\npip install python-telegram-bot")
            return
        t = self._ensure()
        if self.bot and self.bot.is_running:
            self.bot.stop()
            self.b_bot.config(text="🤖 봇 시작", bg=CARD)
            self.q.put(("log", "봇 중지"))
        else:
            # 매번 새 컨트롤러 생성 (Updater 재실행/루프 충돌 방지)
            self.bot = TelegramController(self.settings, t)
            self.bot.start_in_thread()
            self.b_bot.config(text="🤖 봇 중지", bg=GREEN)
            self.q.put(("log", "봇 시작"))
        self._refresh()

    # ---- 주문 ---- #
    def manual(self, side):
        code = self.m_code.get().strip().upper()
        if not code:
            messagebox.showwarning("입력", "종목코드를 입력하세요.")
            return
        try:
            qty = int(self.m_qty.get())
        except ValueError:
            messagebox.showwarning("입력", "수량은 숫자여야 합니다.")
            return
        ovs = self.m_mkt.get() == "미국"
        kind = "매수" if side == "buy" else "매도"
        if not messagebox.askyesno("확인", f"[{self.settings.mode}] {code} {qty}주 {kind}?"):
            return
        t = self._ensure()

        def work():
            fn = t.manual_buy if side == "buy" else t.manual_sell
            r = fn(code, qty, ovs, self.m_exch.get())
            ok = "성공" if r.get("ok") else f"실패: {r.get('msg')}"
            self.q.put(("log", f"{kind} {code} {qty}주 → {ok}"))
            self.q.put(("popup", (f"{kind} 결과", f"{code} {qty}주: {ok}")))
            self.refresh_journal()
            self.show_report("daily")
        threading.Thread(target=work, daemon=True).start()

    def liquidate(self):
        if not messagebox.askyesno("전체청산", "국내 보유 종목을 모두 시장가 청산할까요?"):
            return
        t = self._ensure()

        def work():
            res = t.liquidate_all()
            self.q.put(("log", "청산: " + (", ".join(f"{a}({'OK' if b else 'X'})" for a, b in res) or "보유없음")))
            self.refresh_journal()
            self.show_report("daily")
        threading.Thread(target=work, daemon=True).start()

    def refresh_port(self):
        t = self._ensure()
        threading.Thread(target=lambda: self.q.put(("portfolio", t.safe_balance())), daemon=True).start()

    # ---- 알림/큐 ---- #
    def _notify(self, msg):
        self.q.put(("log", "🔔 " + msg))
        if self.bot and self.bot.is_running:
            try:
                self.bot.broadcast(msg)
            except Exception:  # noqa
                pass

    def _poll(self):
        try:
            while True:
                kind, p = self.q.get_nowait()
                if kind == "log":
                    self._log(p)
                elif kind == "portfolio":
                    self._render_port(p)
                elif kind == "signals":
                    self._render_sig(p)
                elif kind == "journal":
                    self._render_journal(p)
                elif kind == "report":
                    self._render_report(*p)
                elif kind == "popup":
                    messagebox.showinfo(*p)
                elif kind == "chatid":
                    self.set_chat.delete(0, "end")
                    self.set_chat.insert(0, p)
                    messagebox.showinfo("찾음", f"chat_id: {p}\n저장 버튼을 눌러 적용하세요.")
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _render_journal(self, rows):
        for i in self.jtree.get_children():
            self.jtree.delete(i)
        for idx, r in enumerate(rows):
            pnl = r.get("pnl")
            pnl_s = f"{pnl:+,.0f}" if pnl is not None else "-"
            side = "매수" if r["side"] == "buy" else "매도"
            mkt = "국내" if r["market"] == "domestic" else "미국"
            res = "✅" if r["ok"] else "❌"
            tags = []
            if pnl is not None:
                tags.append("pos" if pnl > 0 else "neg")
            elif idx % 2:
                tags.append("odd")
            self.jtree.insert("", "end", tags=tags, values=(
                r["ts"], mkt, f"{r['name']}({r['code']})", side, r["qty"],
                f"{r['price']:,.2f}", f"{r['amount']:,.0f}", pnl_s,
                (r.get("reason") or "")[:18], res))

    def _render_report(self, title, s, chart):
        self.rep_title.config(text=title)
        self.r_trades.config(text=f"{s['trades']}건", fg=FG)
        pnl = s["realized_pnl"]
        self.r_pnl.config(text=f"{pnl:+,.0f}원", fg=(GREEN if pnl > 0 else RED if pnl < 0 else FG))
        self.r_win.config(text=f"{s['win_rate']:.0f}%  ({s['wins']}/{s['wins']+s['losses']})", fg=FG)
        self.r_amt.config(text=f"{s['buy_amount']:,.0f}\n/ {s['sell_amount']:,.0f}",
                          fg=SUB, font=(FONT, 11, "bold"))
        self._draw_chart(chart)

    def _draw_chart(self, data):
        c = self.chart
        c.delete("all")
        if not data:
            return
        w = c.winfo_width() or 980
        h = c.winfo_height() or 220
        pad = 30
        n = len(data)
        bw = max(8, (w - 2 * pad) / n - 6)
        vals = [v for _, v in data]
        mx = max(1.0, max(abs(v) for v in vals))
        zero = h / 2
        c.create_line(pad, zero, w - pad, zero, fill=SUB)
        for i, (label, v) in enumerate(data):
            x = pad + i * ((w - 2 * pad) / n) + 3
            bh = (abs(v) / mx) * (h / 2 - pad)
            color = GREEN if v > 0 else RED if v < 0 else SUB
            if v >= 0:
                c.create_rectangle(x, zero - bh, x + bw, zero, fill=color, outline="")
            else:
                c.create_rectangle(x, zero, x + bw, zero + bh, fill=color, outline="")
            if i % 2 == 0:
                c.create_text(x + bw / 2, h - 10, text=label, fill=SUB, font=(FONT, 7))

    def _log(self, t):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", t + "\n")
        self.log_txt.see("end")
        if int(self.log_txt.index("end-1c").split(".")[0]) > 1000:
            self.log_txt.delete("1.0", "200.0")
        self.log_txt.configure(state="disabled")

    def _render_port(self, bal):
        positions = bal.get("positions", [])
        total_pnl = sum(p.get("pnl_amt", 0) for p in positions)
        self.c_cash.config(text=f"{bal.get('cash', 0):,.0f}원")
        self.c_eval.config(text=f"{bal.get('total_eval', 0):,.0f}원")
        self.c_pnl.config(text=f"{total_pnl:+,.0f}원",
                          fg=(GREEN if total_pnl > 0 else RED if total_pnl < 0 else FG))
        self.c_cnt.config(text=f"{len(positions)}종목")

        for i in self.tree.get_children():
            self.tree.delete(i)
        for idx, p in enumerate(positions):
            rate = p["pnl_rate"]
            tag = "pos" if rate > 0 else "neg" if rate < 0 else ("odd" if idx % 2 else "")
            self.tree.insert("", "end", tags=(tag,), values=(
                f"{p['name']}({p['code']})", p["qty"], f"{p['avg_price']:,.0f}",
                f"{p['cur_price']:,.0f}", f"{p.get('pnl_amt', 0):+,.0f}", f"{rate:+.2f}%"))
        self._draw_alloc(positions)

    def _draw_alloc(self, positions):
        c = self.alloc
        c.delete("all")
        w = c.winfo_width() or 980
        h = 70
        vals = [(p["name"], p["cur_price"] * p["qty"]) for p in positions]
        total = sum(v for _, v in vals)
        if total <= 0:
            c.create_text(w / 2, h / 2, text="보유 종목 없음", fill=SUB, font=(FONT, 10))
            return
        colors = [ACCENT, GREEN, YELLOW, MAUVE, RED, "#94e2d5", "#fab387"]
        x = 10
        bar_w = w - 20
        y0, y1 = 18, 42
        for i, (name, v) in enumerate(vals):
            seg = bar_w * (v / total)
            col = colors[i % len(colors)]
            c.create_rectangle(x, y0, x + seg, y1, fill=col, outline=BG)
            if seg > 45:
                c.create_text(x + seg / 2, (y0 + y1) / 2, text=f"{name}\n{v/total*100:.0f}%",
                              fill="#11111b", font=(FONT, 7, "bold"))
            x += seg
        c.create_text(10, 58, anchor="w", text="포트폴리오 비중", fill=SUB, font=(FONT, 8))

    def _render_sig(self, sigs):
        for i in self.sig_tree.get_children():
            self.sig_tree.delete(i)
        for code, s in sigs.items():
            e = {"buy": "📈매수", "sell": "📉매도", "hold": "⏸보유"}.get(s.action, s.action)
            self.sig_tree.insert("", "end", values=(code, e, f"{s.score_buy:.0f}",
                                 f"{s.score_sell:.0f}", f"{s.price:,.2f}"))

    def _refresh(self):
        s = self.settings
        run = self.trader and self.trader.running
        auto = self.trader and self.trader.auto_enabled
        bot = self.bot and self.bot.is_running
        self.dot.config(fg=GREEN if s.is_paper else RED)
        self.status.config(text=f"[{s.mode}] 엔진:{'가동' if run else '정지'}  봇:{'ON' if bot else 'OFF'}")
        self.b_auto.config(text=f"자동매매: {'ON' if auto else 'OFF'}", bg=GREEN if auto else PANEL)
        self.bar.config(text=f"모드 {s.mode} | 감시 국내 {len(s.universe.get('domestic', []))} · "
                             f"해외 {len(s.universe.get('overseas', []))} | 폴더 {BASE}")


def main():
    root = tk.Tk()
    TradingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
