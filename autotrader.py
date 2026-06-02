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
import concurrent.futures
from collections import deque
from contextlib import contextmanager
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
        "process_timeout_sec": 30,
        "domestic_session": "09:00-15:20",
        "overseas_session": "23:30-06:00",
        "auto_trade_enabled": True,
    },
    "screener": {
        "enabled": True,        # True면 고정 universe 대신 시장 전체에서 자동 발굴
        "market": "all",        # 국내: all / kospi / kosdaq
        "pool_size": 30,        # 후보 수
        "top_k": 15,            # 심층 전략분석할 상위 모멘텀 종목 수(유량 보호)
        "min_price": 2000,      # 국내 동전주 제외(원)
        "max_price": 500000,    # 국내(원)
        "momentum_rank": True,  # 등락률 기준 모멘텀 정렬
        # --- 미국장(USD 기준) ---
        "overseas_market": "NAS",      # NAS / NYS / AMS
        "overseas_min_price": 5,       # USD
        "overseas_max_price": 1000,    # USD
        "overseas_pool": ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL",
                          "META", "AMD", "NFLX", "AVGO", "PLTR", "COIN"],  # 조건검색 실패시 후보풀
        # True: 통합증거금(원화로 미국매수, 자동환전) 기준 매수가능금액 사용
        # False: USD 예수금만 사용(사전 환전 방식)
        "overseas_use_buyable": True,
    },
    "strategy": {
        "buy_threshold": 3,
        "sell_threshold": 2,
        "indicators": {
            "ma_cross": {"enabled": True, "short": 5, "long": 20, "weight": 1},
            "rsi": {"enabled": True, "period": 14, "low": 30, "high": 70, "weight": 1},
            "macd": {"enabled": True, "fast": 12, "slow": 26, "signal": 9, "weight": 1},
            "bollinger": {"enabled": True, "period": 20, "num_std": 2.0, "weight": 1},
            "vol_breakout": {"enabled": True, "k": 0.5, "weight": 2},
            "new_high": {"enabled": True, "period": 60, "weight": 1},
            "adx": {"enabled": True, "period": 14, "min": 20, "penalty": 1},
            "regime": {"enabled": True, "ma": 60},
        },
    },
    "strategy_domestic": {},
    "strategy_overseas": {},
    "risk": {
        "max_positions": 5,
        "risk_per_trade_pct": 1.0,
        "stop_loss_pct": 5.0,
        "take_profit_pct": 10.0,
        "trailing_stop_pct": 4.0,
        "cooldown_min": 30,
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
    strategy_domestic: dict = field(default_factory=dict)
    strategy_overseas: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)
    screener: dict = field(default_factory=dict)
    paper_account: str = ""
    real_account: str = ""

    @property
    def is_paper(self):
        return self.mode == "paper"

    @property
    def base_url(self):
        return PAPER_BASE_URL if self.is_paper else REAL_BASE_URL

    @property
    def active_account_key(self):
        return "KIS_PAPER_ACCOUNT" if self.is_paper else "KIS_REAL_ACCOUNT"


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


def _deep_merge(base, override):
    """기본 설정에 사용자 설정을 재귀 병합한다."""
    merged = json.loads(json.dumps(base))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings():
    if load_dotenv:
        load_dotenv(ENV_PATH, override=True)
    env = _read_env_file()

    def g(key, default=""):
        return os.getenv(key, env.get(key, default))

    mode = (g("TRADING_MODE", "paper") or "paper").lower()
    paper_account = g("KIS_PAPER_ACCOUNT")
    real_account = g("KIS_REAL_ACCOUNT")
    if mode == "real":
        ak, sk, acc = g("KIS_REAL_APP_KEY"), g("KIS_REAL_APP_SECRET"), real_account
    else:
        mode = "paper"
        ak, sk, acc = g("KIS_PAPER_APP_KEY"), g("KIS_PAPER_APP_SECRET"), paper_account
    no, prod = _split_account(acc)
    chat_raw = g("TELEGRAM_ALLOWED_CHAT_IDS", "")
    allowed = [c.strip() for c in chat_raw.split(",") if c.strip()]

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if yaml and os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                ycfg = yaml.safe_load(f) or {}
            cfg = _deep_merge(cfg, ycfg)
        except Exception as e:  # noqa
            log.warning("config.yaml 파싱 실패, 기본값 사용: %s", e)

    base_strategy = cfg.get("strategy", {})
    strategy_domestic = _deep_merge(base_strategy, cfg.get("strategy_domestic", {}))
    strategy_overseas = _deep_merge(base_strategy, cfg.get("strategy_overseas", {}))

    return Settings(
        mode=mode,
        app_key=ak,
        app_secret=sk,
        account_no=no,
        account_prod=prod,
        telegram_token=g("TELEGRAM_BOT_TOKEN"),
        allowed_chat_ids=allowed,
        strategy=base_strategy,
        strategy_domestic=strategy_domestic,
        strategy_overseas=strategy_overseas,
        risk=cfg.get("risk", {}),
        engine=cfg.get("engine", {}),
        universe=cfg.get("universe", {}),
        screener=cfg.get("screener", {}),
        paper_account=paper_account,
        real_account=real_account,
    )


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


def _to_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _first_float(data, keys):
    for key in keys:
        value = _to_float((data or {}).get(key))
        if value:
            return value
    return 0.0


def _normalize_position(position, market, currency):
    item = dict(position or {})
    item["market"] = market
    item["market_label"] = "국내" if market == "domestic" else "미국"
    item["currency"] = currency
    item["position_value"] = _to_float(item.get("cur_price")) * _to_float(item.get("qty"))
    return item


def build_portfolio_snapshot(domestic, overseas):
    """국내 원화 잔고와 미국 달러 잔고를 통화별로 분리한 표시용 스냅샷으로 합친다."""
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
    pnl_krw = sum(_to_float(p.get("pnl_amt")) for p in domestic_positions)
    pnl_usd = sum(_to_float(p.get("pnl_amt")) for p in overseas_positions)
    return {
        "cash": cash_krw,
        "cash_krw": cash_krw,
        "cash_usd": cash_usd,
        "total_eval": total_eval_krw,
        "total_eval_krw": total_eval_krw,
        "total_eval_usd": total_eval_usd,
        "pnl_krw": pnl_krw,
        "pnl_usd": pnl_usd,
        "positions": domestic_positions + overseas_positions,
        "domestic": domestic,
        "overseas": overseas,
    }


def _format_money(value, currency="KRW", signed=False):
    value = _to_float(value)
    if currency == "USD":
        return f"{value:+,.2f} USD" if signed else f"{value:,.2f} USD"
    return f"{value:+,.0f}원" if signed else f"{value:,.0f}원"


def _format_balance_lines(krw_value, usd_value=0, include_usd=False, signed=False):
    lines = [_format_money(krw_value, "KRW", signed=signed)]
    if include_usd or _to_float(usd_value):
        lines.append(_format_money(usd_value, "USD", signed=signed))
    return "\n".join(lines)


def mode_label(mode):
    return "실전" if mode == "real" else "모의"


def risk_state_path(mode) -> str:
    """모드별 리스크 상태 파일 경로를 반환한다."""
    safe_mode = "real" if mode == "real" else "paper"
    return os.path.join(LOG_DIR, f"risk_state_{safe_mode}.json")


def clear_token_cache() -> bool:
    """계좌/모드 변경 시 기존 KIS 토큰 캐시를 삭제한다."""
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        os.remove(TOKEN_FILE)
        log.info("KIS 토큰 캐시 삭제: %s", TOKEN_FILE)
        return True
    except OSError as e:
        log.warning("KIS 토큰 캐시 삭제 실패: %s", e)
        return False


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
        # src.kis_api와 같은 보수적 호출 제한 정책을 사용한다.
        self.limiter = RateLimiter(3, min_interval=0.4) if s.is_paper \
            else RateLimiter(15, min_interval=0.07)
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

    def domestic_volume_rank(self, market="all", count=30):
        """거래량 순위(시장 전체 스크리너용). 한 번 호출로 활발한 종목 다수 반환."""
        iscd = {"all": "0000", "kospi": "0001", "kosdaq": "1001"}.get(market, "0000")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": iscd,
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",          # 0 평균거래량
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }
        d = self._get("/uapi/domestic-stock/v1/quotations/volume-rank", "FHPST01710000", params)
        out = []
        for r in (d.get("output") or [])[:count]:
            code = r.get("mksc_shrn_iscd")
            if not code:
                continue
            out.append({
                "code": code,
                "name": r.get("hts_kor_isnm", code),
                "price": float(r.get("stck_prpr", 0) or 0),
                "change_rate": float(r.get("prdy_ctrt", 0) or 0),
                "volume": int(float(r.get("acml_vol", 0) or 0)),
            })
        return out

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

    def overseas_balance(self, exch="NAS"):
        """미국 잔고(USD). 반환: {cash(USD 예수금), positions:[...]}"""
        tr = "VTTS3012R" if self.s.is_paper else "TTTS3012R"
        params = {
            "CANO": self.s.account_no, "ACNT_PRDT_CD": self.s.account_prod,
            "OVRS_EXCG_CD": self._OVRS.get(exch, "NASD"), "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        }
        d = self._get("/uapi/overseas-stock/v1/trading/inquire-balance", tr, params)
        pos = []
        for r in (d.get("output1") or []):
            qty = int(float(r.get("ovrs_cblc_qty", 0) or 0))
            if qty <= 0:
                continue
            pos.append({
                "code": r.get("ovrs_pdno"), "name": r.get("ovrs_item_name"),
                "qty": qty, "avg_price": float(r.get("pchs_avg_pric", 0) or 0),
                "cur_price": float(r.get("now_pric2", 0) or 0),
                "pnl_rate": float(r.get("evlu_pfls_rt", 0) or 0),
                "pnl_amt": float(r.get("frcr_evlu_pfls_amt", 0) or 0),
                "market": "overseas", "currency": "USD",
            })
        summ = d.get("output2") or {}
        if isinstance(summ, list):
            summ = summ[0] if summ else {}
        log.debug("해외잔고 요약 키(%s): %s", exch, list((summ or {}).keys()))
        cash = _first_float(
            summ,
            (
                "frcr_dncl_amt_2",
                "frcr_dncl_amt1",
                "frcr_dncl_amt",
                "frcr_buy_psbl_amt1",
                "ovrs_ord_psbl_amt",
                "ovrs_tot_dncl_amt",
            ),
        )
        total_eval = _first_float(
            summ,
            ("tot_evlu_amt", "ovrs_tot_evlu_amt", "frcr_evlu_tota", "frcr_evlu_amt2"),
        )
        if not total_eval and (cash or pos):
            total_eval = cash + sum(_to_float(p.get("cur_price")) * _to_float(p.get("qty")) for p in pos)
        return {"cash": cash, "total_eval": total_eval, "positions": pos}

    def overseas_buyable(self, sym, price, exch="NAS"):
        """해외주식 매수가능금액 조회(통합증거금·환율 반영).
        반환: {amount: 주문가능 USD, qty: 주문가능 수량}"""
        tr = "VTTS3007R" if self.s.is_paper else "TTTS3007R"
        params = {
            "CANO": self.s.account_no, "ACNT_PRDT_CD": self.s.account_prod,
            "OVRS_EXCG_CD": self._OVRS.get(exch, "NASD"),
            "OVRS_ORD_UNPR": f"{price:.2f}", "ITEM_CD": sym,
        }
        d = self._get("/uapi/overseas-stock/v1/trading/inquire-psamount", tr, params)
        o = d.get("output") or {}
        if isinstance(o, list):
            o = o[0] if o else {}
        amount = float(o.get("ord_psbl_frcr_amt") or o.get("frcr_ord_psbl_amt1")
                       or o.get("ovrs_ord_psbl_amt") or 0)
        qty = int(float(o.get("ovrs_ord_psbl_qty") or o.get("ord_psbl_qty")
                        or o.get("max_ord_psbl_qty") or 0))
        return {"amount": amount, "qty": qty}

    def overseas_search(self, exch="NAS", min_price=0, max_price=0, count=30):
        """미국 조건검색(시장 전체 스크리너). 가격대 조건으로 후보 리스트 반환."""
        params = {
            "AUTH": "", "EXCD": exch,
            "CO_YN_PRICECUR": "1" if (min_price or max_price) else "0",
            "CO_ST_PRICECUR": str(min_price or ""), "CO_EN_PRICECUR": str(max_price or ""),
            "CO_YN_RATE": "0", "CO_ST_RATE": "", "CO_EN_RATE": "",
            "CO_YN_VALX": "0", "CO_ST_VALX": "", "CO_EN_VALX": "",
            "CO_YN_SHAR": "0", "CO_ST_SHAR": "", "CO_EN_SHAR": "",
            "CO_YN_VOLUME": "0", "CO_ST_VOLUME": "", "CO_EN_VOLUME": "",
            "CO_YN_AMT": "0", "CO_ST_AMT": "", "CO_EN_AMT": "",
            "CO_YN_EPS": "0", "CO_ST_EPS": "", "CO_EN_EPS": "",
            "CO_YN_PER": "0", "CO_ST_PER": "", "CO_EN_PER": "",
            "KEYB": "",
        }
        d = self._get("/uapi/overseas-price/v1/quotations/inquire-search", "HHDFS76410000", params)
        out = []
        for r in (d.get("output2") or [])[:count]:
            sym = r.get("symb")
            if not sym:
                continue
            out.append({
                "code": sym,
                "name": r.get("name") or r.get("knam") or sym,
                "price": float(r.get("last", 0) or 0),
                "change_rate": float(r.get("rate", 0) or 0),
            })
        return out


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


def _vol_breakout_target(candles, k=0.5, current_open=None):
    if len(candles) < 2:
        return None
    today_open = current_open if current_open and current_open > 0 else candles[-1]["open"]
    return today_open + (candles[-2]["high"] - candles[-2]["low"]) * k


def _highest_high(candles, n):
    """최근 n봉의 최고가(신고가 돌파 판정용)."""
    if len(candles) < n or n <= 0:
        return None
    return max(c["high"] for c in candles[-n:])


def _adx(candles, n=14):
    """추세 강도(DX 기반 근사). 0~100, 높을수록 추세가 강함."""
    if len(candles) < n + 1:
        return None
    plus_dm = minus_dm = tr_sum = 0.0
    for i in range(-n, 0):
        up = candles[i]["high"] - candles[i - 1]["high"]
        dn = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm += up if (up > dn and up > 0) else 0.0
        minus_dm += dn if (dn > up and dn > 0) else 0.0
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr_sum += max(h - l, abs(h - pc), abs(l - pc))
    if tr_sum == 0:
        return None
    pdi = 100 * plus_dm / tr_sum
    mdi = 100 * minus_dm / tr_sum
    denom = pdi + mdi
    if denom == 0:
        return 0.0
    return 100 * abs(pdi - mdi) / denom


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
    adx: float = 0.0          # 추세 강도(0~100)
    trend_ok: bool = True     # 장기추세(레짐) 통과 여부
    vol_target: float = 0.0    # 변동성 돌파 목표가


class CompositeStrategy:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.ind = self.cfg.get("indicators", {})
        self.buy_th = self.cfg.get("buy_threshold", 2)
        self.sell_th = self.cfg.get("sell_threshold", 2)

    def evaluate(self, candles, price=None, current_open=None):
        if not candles or len(candles) < 30:
            return Signal("hold", 0, 0, ["데이터 부족"], price or 0)
        closes = [c["close"] for c in candles]
        p = float(price if price else closes[-1])
        buy = sell = 0.0
        reasons = []
        adx_val = _adx(candles, (self.ind.get("adx", {}) or {}).get("period", 14))
        trend_ok = True
        vol_target = 0.0

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
            vol_target = _vol_breakout_target(candles, c.get("k", 0.5), current_open=current_open) or 0.0
            if vol_target and p >= vol_target > 0:
                buy += w; reasons.append(f"변동성돌파({vol_target:.2f}) +{w}")

        # N일 신고가 돌파 (레퍼런스: new_high_breakout)
        c = self.ind.get("new_high", {})
        if c.get("enabled"):
            w = c.get("weight", 1)
            period = c.get("period", 60)
            hh = _highest_high(candles[:-1], period)
            if hh and p >= hh:
                buy += w; reasons.append(f"{period}일 신고가 돌파 +{w}")

        # ADX 추세강도 확인: 추세가 약하면 매수 점수 차감(휩쏘 방지)
        c = self.ind.get("adx", {})
        if c.get("enabled"):
            thr = c.get("min", 20)
            if adx_val is not None and adx_val < thr:
                buy = max(0.0, buy - c.get("penalty", 1))
                reasons.append(f"추세약함(ADX {adx_val:.0f}<{thr})")

        # 판정
        if buy >= self.buy_th and buy > sell:
            act = "buy"
        elif sell >= self.sell_th and sell > buy:
            act = "sell"
        else:
            act = "hold"

        # 추세 필터(레짐): 장기 이평 아래면 신규 매수 보류 (하락장 매수 방지)
        rc = self.ind.get("regime", {})
        if rc.get("enabled"):
            ma_p = rc.get("ma", 60)
            lma = _sma(closes, ma_p) if len(closes) >= ma_p else None
            if lma is not None and p < lma:
                trend_ok = False
                if act == "buy":
                    act = "hold"
                    reasons.append(f"추세필터: 가격<{ma_p}일선 매수보류")
        return Signal(act, buy, sell, reasons, p, adx=adx_val or 0.0, trend_ok=trend_ok, vol_target=vol_target)


# ===================================================================== #
#  리스크 관리
# ===================================================================== #
class RiskManager:
    def __init__(self, cfg, state_path=None):
        self.cfg = cfg or {}
        self.state_path = state_path
        self.state_date = None
        self.day_start = None
        self.peak = None
        self.halted = False
        self._load_state()

    def _today(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _load_state(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.state_date = data.get("date")
            self.day_start = data.get("day_start_equity")
            self.peak = data.get("peak_equity")
            self.halted = bool(data.get("halted", False))
        except Exception:  # noqa
            self.state_date = None
            self.day_start = None
            self.peak = None
            self.halted = False

    def _save_state(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({
                "date": self.state_date,
                "day_start_equity": self.day_start,
                "peak_equity": self.peak,
                "halted": self.halted,
            }, f, ensure_ascii=False, indent=2)

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
        if q < 1 and price <= risk_amt:
            q = 1
        # 보유금액(예수금)으로 살 수 있는 최대 수량으로 제한 (수수료 0.5% 버퍼)
        affordable = int(cash // (price * 1.005))
        q = min(q, affordable)
        return q if q >= 1 else 0

    def should_exit(self, avg, cur, peak=None):
        if avg <= 0:
            return ""
        pct = (cur - avg) / avg * 100
        if pct <= -self.cfg.get("stop_loss_pct", 5.0):
            return "손절"
        # 트레일링 스탑: 보유 고점 대비 하락폭이 기준 초과 + 수익 구간일 때
        ts = self.cfg.get("trailing_stop_pct", 0)
        if ts and peak and peak > 0:
            draw = (cur - peak) / peak * 100
            if draw <= -ts and cur > avg:
                return "트레일링스탑"
        if pct >= self.cfg.get("take_profit_pct", 10.0):
            return "익절"
        return ""

    def can_open(self, n):
        return n < self.cfg.get("max_positions", 5)

    def reset_day(self, eq):
        self.state_date = self._today()
        self.day_start = eq
        if self.peak is None or eq > self.peak:
            self.peak = eq
        self.halted = False
        self._save_state()

    def check_limits(self, eq):
        today = self._today()
        if self.state_date != today:
            self.state_date = today
            self.day_start = eq
            self.halted = False
        elif self.day_start is None:
            self.day_start = eq
        if self.peak is None or eq > self.peak:
            self.peak = eq
        self._save_state()
        if self.day_start and self.day_start > 0:
            if (eq - self.day_start) / self.day_start * 100 <= -self.cfg.get("daily_loss_limit_pct", 3.0):
                self.halted = True
                self._save_state()
                return "일일손실한도"
        if self.peak and self.peak > 0:
            if (eq - self.peak) / self.peak * 100 <= -self.cfg.get("max_drawdown_pct", 15.0):
                self.halted = True
                self._save_state()
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

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

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

    def recent(self, limit=300, mode=None):
        try:
            with self.lock, self._conn() as c:
                if mode:
                    rows = c.execute(
                        "SELECT * FROM trades WHERE mode=? ORDER BY id DESC LIMIT ?",
                        (mode, limit),
                    ).fetchall()
                else:
                    rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa
            return []

    def clear_failed(self, mode=None):
        """체결 실패(ok=0)한 기록만 삭제. 실제 매수/매도 성공 기록은 보존."""
        try:
            with self.lock, self._conn() as c:
                if mode:
                    cur = c.execute("DELETE FROM trades WHERE ok=0 AND mode=?", (mode,))
                else:
                    cur = c.execute("DELETE FROM trades WHERE ok=0")
                return cur.rowcount
        except Exception:  # noqa
            return 0

    def summary(self, start_date, end_date, mode=None):
        """기간(YYYY-MM-DD) 집계."""
        try:
            with self.lock, self._conn() as c:
                if mode:
                    rows = c.execute(
                        "SELECT * FROM trades WHERE date>=? AND date<=? AND ok=1 AND mode=?",
                        (start_date, end_date, mode),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT * FROM trades WHERE date>=? AND date<=? AND ok=1",
                        (start_date, end_date),
                    ).fetchall()
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

    def daily_pnl(self, days=14, mode=None):
        """최근 days일 일별 실현손익 [(date, pnl), ...]."""
        out = []
        today = datetime.now().date()
        try:
            with self.lock, self._conn() as c:
                for i in range(days - 1, -1, -1):
                    d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
                    if mode:
                        row = c.execute(
                            "SELECT COALESCE(SUM(pnl),0) p FROM trades "
                            "WHERE date=? AND side='sell' AND ok=1 AND mode=?",
                            (d, mode),
                        ).fetchone()
                    else:
                        row = c.execute(
                            "SELECT COALESCE(SUM(pnl),0) p FROM trades "
                            "WHERE date=? AND side='sell' AND ok=1",
                            (d,),
                        ).fetchone()
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
        self.strategy_domestic = CompositeStrategy(getattr(s, "strategy_domestic", None) or s.strategy)
        self.strategy_overseas = CompositeStrategy(getattr(s, "strategy_overseas", None) or s.strategy)
        self.risk = RiskManager(s.risk, state_path=risk_state_path(s.mode))
        self.journal = journal or TradeJournal()
        self.notify = notify or (lambda m: None)
        self.auto_enabled = s.engine.get("auto_trade_enabled", True)
        self.running = False
        self._stop = threading.Event()
        self._thread = None
        self._scan_lock = threading.Lock()   # 동시 스캔 방지(유량 절약)
        self.last_signals = {}
        self._peak = {}        # 보유 고점(트레일링 스탑용)
        self._cooldown = {}    # 청산 후 재매수 쿨다운 타임스탬프
        self.last_candidates = []  # 최근 스크리너 후보

    def _in_cooldown(self, code):
        mins = self.s.risk.get("cooldown_min", 0)
        if not mins:
            return False
        ts = self._cooldown.get(code)
        return ts is not None and (time.time() - ts) < mins * 60

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
            price = q["price"]
            sig = self.strategy_domestic.evaluate(candles, price, current_open=q.get("open"))
            self.last_signals[code] = sig
            pos = next((p for p in bal["positions"] if p["code"] == code), None)
            if pos:
                # 보유 고점 갱신(트레일링 스탑)
                peak = max(self._peak.get(code, pos["avg_price"]), price)
                self._peak[code] = peak
                why = self.risk.should_exit(pos["avg_price"], price, peak)
                if why or sig.action == "sell":
                    why = why or "전략매도"
                    if self.auto_enabled:
                        r = self.api.domestic_order(code, pos["qty"], "sell")
                        pnl = (price - pos["avg_price"]) * pos["qty"]
                        self._report("매도", "domestic", code, pos.get("name", code),
                                     "sell", pos["qty"], price, why, r, pnl=pnl)
                        self._cooldown[code] = time.time()   # 청산 후 쿨다운 시작
                        self._peak.pop(code, None)
                    else:
                        self.notify(f"📉[신호] {code} 매도추천({why}) — 자동OFF")
                    return
            if sig.action == "buy" and not pos:
                if self._in_cooldown(code):
                    return
                if not self.risk.can_open(len(bal["positions"])):
                    return
                qty = self.risk.position_size(bal.get("cash", 0), price, candles)
                if qty < 1:
                    return
                if self.auto_enabled:
                    r = self.api.domestic_order(code, qty, "buy")
                    self._report("매수", "domestic", code, code, "buy", qty,
                                 price, ", ".join(sig.reasons), r)
                    self._peak[code] = price
                else:
                    self.notify(f"📈[신호] {code} 매수추천 {qty}주({', '.join(sig.reasons)}) — 자동OFF")
        except Exception as e:  # noqa
            log.exception("국내 처리오류 %s", code)
            self.notify(f"⚠️ {code} 오류: {e}")

    def _proc_ovs(self, item, obal):
        sym, exch = item.get("symbol"), item.get("exchange", "NAS")
        try:
            candles = self.api.overseas_daily(sym, exch)
            q = self.api.overseas_price(sym, exch)
            price = q["price"]
            sig = self.strategy_overseas.evaluate(candles, price, current_open=q.get("open"))
            self.last_signals[sym] = sig
            pos = next((p for p in obal["positions"] if p["code"] == sym), None)
            if pos:
                peak = max(self._peak.get(sym, pos["avg_price"]), price)
                self._peak[sym] = peak
                why = self.risk.should_exit(pos["avg_price"], price, peak)
                if why or sig.action == "sell":
                    why = why or "전략매도"
                    if self.auto_enabled:
                        r = self.api.overseas_order(sym, pos["qty"], "sell", price, exch)
                        pnl = (price - pos["avg_price"]) * pos["qty"]
                        self._report("매도(美)", "overseas", sym, sym, "sell",
                                     pos["qty"], price, why, r, pnl=pnl)
                        self._cooldown[sym] = time.time()
                        self._peak.pop(sym, None)
                    else:
                        self.notify(f"📉[신호] {sym} 매도추천({why}) — 자동OFF")
                    return
            if sig.action == "buy" and not pos:
                if self._in_cooldown(sym):
                    return
                if not self.risk.can_open(len(obal["positions"])):
                    return
                # 매수가능금액 산정: 통합증거금이면 원화로도 가능(매수가능금액 조회 우선)
                cash = obal.get("cash", 0)   # USD 예수금
                maxqty = None
                if (self.s.screener or {}).get("overseas_use_buyable", True):
                    try:
                        b = self.api.overseas_buyable(sym, price, exch)
                        if b["amount"] > 0:
                            cash = b["amount"]      # 통합증거금/환율 반영 주문가능 USD
                        if b["qty"] > 0:
                            maxqty = b["qty"]
                    except Exception as e:  # noqa
                        log.warning("%s 매수가능금액 조회 실패, 예수금 기준: %s", sym, e)
                if cash <= 0 and not maxqty:
                    log.info("%s 매수가능금액 0/미확인 — 건너뜀", sym)
                    return
                qty = self.risk.position_size(cash, price, candles) if cash > 0 else (maxqty or 0)
                if maxqty is not None:
                    qty = min(qty, maxqty)
                if qty < 1:
                    log.info("%s 매수가능 수량 부족(1주 미만) — 건너뜀", sym)
                    return
                if self.auto_enabled:
                    r = self.api.overseas_order(sym, qty, "buy", price, exch)
                    self._report("매수(美)", "overseas", sym, sym, "buy", qty,
                                 price, ", ".join(sig.reasons), r)
                    self._peak[sym] = price
                else:
                    self.notify(f"📈[신호] {sym} 매수추천 {qty}주({', '.join(sig.reasons)}) — 자동OFF")
        except Exception as e:  # noqa
            log.exception("해외 처리오류 %s", sym)
            self.notify(f"⚠️ {sym} 오류: {e}")

    def screen_candidates(self, cash=0):
        """스크리너가 켜져 있으면 시장 전체에서 후보 발굴, 아니면 고정 universe.
        cash>0 면 보유현금으로 최소 1주 살 수 있는 종목만 남긴다."""
        sc = self.s.screener or {}
        if not sc.get("enabled"):
            return [{"code": c, "name": c} for c in self._dom()]
        try:
            pool = self.api.domestic_volume_rank(sc.get("market", "all"), sc.get("pool_size", 30))
        except Exception as e:  # noqa
            log.warning("스크리너 조회 실패, 고정 universe 사용: %s", e)
            return [{"code": c, "name": c} for c in self._dom()]
        lo = sc.get("min_price", 0)
        hi = sc.get("max_price", 10 ** 12)
        pool = [x for x in pool if lo <= x["price"] <= hi]
        if cash and cash > 0:   # 보유현금으로 1주 이상 가능한 종목만(수수료 0.5% 버퍼)
            pool = [x for x in pool if x["price"] > 0 and x["price"] * 1.005 <= cash]
        if sc.get("momentum_rank", True):
            pool.sort(key=lambda x: x["change_rate"], reverse=True)
        top = pool[: sc.get("top_k", 15)]
        self.last_candidates = top
        return top

    def screen_overseas(self, cash=0):
        """미국장 후보 발굴(USD 기준). 조건검색 실패시 후보풀 사용.
        cash>0(USD) 면 그 금액으로 1주 살 수 있는 종목만 남긴다."""
        sc = self.s.screener or {}
        exch = sc.get("overseas_market", "NAS")
        if not sc.get("enabled"):
            return self._ovs()
        lo = sc.get("overseas_min_price", 0)
        hi = sc.get("overseas_max_price", 10 ** 9)
        topk = sc.get("top_k", 15)
        pool = []
        try:
            pool = self.api.overseas_search(exch, lo, hi, sc.get("pool_size", 30))
        except Exception as e:  # noqa
            log.warning("미국 스크리너 실패, 후보풀 사용: %s", e)
        if not pool:
            return [{"symbol": s, "exchange": exch} for s in sc.get("overseas_pool", [])[:topk]]
        pool = [x for x in pool if lo <= x["price"] <= hi]
        if cash and cash > 0:   # USD 예수금으로 1주 이상 가능한 종목만
            pool = [x for x in pool if x["price"] > 0 and x["price"] * 1.005 <= cash]
        if sc.get("momentum_rank", True):
            pool.sort(key=lambda x: x["change_rate"], reverse=True)
        self.last_candidates = pool[:topk]
        return [{"symbol": x["code"], "exchange": exch} for x in pool[:topk]]

    def safe_overseas_balance(self):
        exch = (self.s.screener or {}).get("overseas_market", "NAS")
        try:
            return self.api.overseas_balance(exch)
        except Exception as e:  # noqa
            log.warning("미국 잔고조회 실패: %s", e)
            return {"cash": 0, "total_eval": 0, "positions": []}

    def portfolio_balance(self):
        """대시보드/리포트 표시용 국내+미국 잔고 스냅샷을 반환한다."""
        return build_portfolio_snapshot(self.safe_balance(), self.safe_overseas_balance())

    def _process_with_timeout(self, label, target, timeout, *args):
        """종목 하나가 지연돼도 전체 스캔이 계속되도록 처리 시간을 제한한다."""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(target, *args)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning("%s 처리 타임아웃(%ds) — 다음 종목으로 진행", label, timeout)
        except Exception as e:  # noqa
            log.exception("%s 처리 오류: %s", label, e)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def scan_once(self):
        # 이미 다른 스캔이 진행 중이면 건너뜀(유량 초과 방지)
        if not self._scan_lock.acquire(blocking=False):
            log.info("스캔 이미 진행 중 — 이번 호출은 건너뜁니다.")
            return
        try:
            self.last_signals = {}   # 현재 스캔 결과만 표시
            bal = self.safe_balance()
            eq = bal.get("total_eval") or bal.get("cash", 0)
            lim = self.risk.check_limits(eq)
            if lim:
                self.notify(f"🛑 {lim} 도달 — 매매 중단. 필요시 전체청산하세요.")
                return
            dom_open = self._dom_open()
            ovs_open = self._ovs_open()
            timeout = int((self.s.engine or {}).get("process_timeout_sec", 30) or 30)

            # 국내장: 열려 있거나, 둘 다 닫혀 있으면 기본 분석용으로 국내 스캔
            if dom_open or not ovs_open:
                codes = [c["code"] for c in self.screen_candidates(bal.get("cash", 0))]
                for p in bal["positions"]:
                    if p["code"] not in codes:
                        codes.append(p["code"])
                for code in codes:
                    self._process_with_timeout(f"국내 {code}", self._proc_dom, timeout, code, bal)

            # 미국장: 열려 있으면 미국 종목 발굴(USD 기준)
            if ovs_open:
                obal = self.safe_overseas_balance()
                # 통합증거금이면 USD예수금이 0이어도 원화로 매수 가능 → 화면필터는 끄고
                # 주문 단계의 매수가능금액 조회로 정확히 판단
                us_cash = 0 if (self.s.screener or {}).get("overseas_use_buyable", True) \
                    else obal.get("cash", 0)
                items = self.screen_overseas(us_cash)
                cand = [it["symbol"] for it in items]
                for p in obal["positions"]:   # 미국 보유종목은 청산 관리 위해 포함
                    if p["code"] not in cand:
                        items.append({"symbol": p["code"],
                                      "exchange": (self.s.screener or {}).get("overseas_market", "NAS")})
                for it in items:
                    self._process_with_timeout(f"해외 {it.get('symbol', '')}", self._proc_ovs, timeout, it, obal)
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

    @staticmethod
    def _within(rng):
        now = datetime.now().strftime("%H:%M")
        try:
            a, b = rng.split("-")
            return (a <= now <= b) if a <= b else (now >= a or now <= b)
        except Exception:  # noqa
            return True

    def _dom_open(self):
        return self._within(self.s.engine.get("domestic_session", "09:00-15:20"))

    def _ovs_open(self):
        return self._within(self.s.engine.get("overseas_session", "23:30-06:00"))

    def _in_session(self):
        return self._dom_open() or self._ovs_open()

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
        res = []
        # 국내 전체 청산
        for p in self.safe_balance()["positions"]:
            r = self.api.domestic_order(p["code"], p["qty"], "sell")
            pnl = (p["cur_price"] - p["avg_price"]) * p["qty"]
            self.journal.log(self.s.mode, "domestic", p["code"], p.get("name", p["code"]),
                             "sell", p["qty"], p["cur_price"], "전체청산", pnl,
                             r.get("ok"), r.get("msg", ""))
            self._peak.pop(p["code"], None)
            res.append((p["code"], r.get("ok")))
        # 미국 전체 청산
        exch = (self.s.screener or {}).get("overseas_market", "NAS")
        for p in self.safe_overseas_balance()["positions"]:
            r = self.api.overseas_order(p["code"], p["qty"], "sell", p.get("cur_price", 0) or 0, exch)
            pnl = (p["cur_price"] - p["avg_price"]) * p["qty"]
            self.journal.log(self.s.mode, "overseas", p["code"], p.get("name", p["code"]),
                             "sell", p["qty"], p["cur_price"], "전체청산", pnl,
                             r.get("ok"), r.get("msg", ""))
            self._peak.pop(p["code"], None)
            res.append((p["code"], r.get("ok")))
        return res

    def portfolio_report(self):
        bal = self.portfolio_balance()
        has_usd = bool(bal.get("overseas", {}).get("positions")) or bool(bal.get("cash_usd"))
        L = [
            f"💼 포트폴리오({self.s.mode})",
            "예수금 " + _format_balance_lines(bal.get("cash_krw"), bal.get("cash_usd"), has_usd),
            "총평가 " + _format_balance_lines(
                bal.get("total_eval_krw"), bal.get("total_eval_usd"), has_usd
            ),
        ]
        if not bal["positions"]:
            L.append("보유 종목 없음")
        for p in bal["positions"]:
            currency = p.get("currency", "KRW")
            L.append(
                f"• [{p.get('market_label', '-')}] {p['name']}({p['code']}) {p['qty']}주 "
                f"평단 {_format_money(p['avg_price'], currency)} "
                f"현재 {_format_money(p['cur_price'], currency)} "
                f"손익 {_format_money(p.get('pnl_amt', 0), currency, signed=True)} "
                f"({p['pnl_rate']:+.2f}%)"
            )
        return "\n".join(L)

    def daily_report(self):
        """텔레그램에서 즉시 조회할 수 있는 오늘 실현손익 요약."""
        today = today_str()
        s = self.journal.summary(today, today, self.s.mode)
        return "\n".join([
            f"📆 오늘 리포트({self.s.mode})",
            f"거래 {s['trades']}건 · 매수 {s['buys']}건 · 매도 {s['sells']}건",
            f"실현손익 {s['realized_pnl']:+,.0f}",
            f"승/패 {s['wins']}/{s['losses']} · 승률 {s['win_rate']:.1f}%",
            f"매수금액 {s['buy_amount']:,.0f} · 매도금액 {s['sell_amount']:,.0f}",
        ])


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
        h(CommandHandler("daily", self.c_daily))
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
            "🤖 명령어\n/status /portfolio /daily\n/auto on|off\n/run /signals\n"
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

    async def c_daily(self, u, c):
        import asyncio
        if not await self._ok(u):
            return
        r = await asyncio.to_thread(self.trader.daily_report)
        await u.message.reply_text(r)

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
                future = asyncio.run_coroutine_threadsafe(self.app.stop(), self._loop)
                future.result(timeout=5.0)
        except Exception:  # noqa
            log.warning("봇 stop 대기 중 오류", exc_info=True)
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        self._loop = None
        self._thread = None


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
        self.title = tk.Label(top, text="📈 자동매매 대시보드", bg=BG, fg=FG,
                              font=(FONT, 14, "bold"))
        self.title.pack(side="left")
        self.dot = tk.Label(top, text="●", bg=BG, fg=GREEN, font=(FONT, 13))
        self.dot.pack(side="left", padx=(16, 4))
        self.status = tk.Label(top, text="준비", bg=BG, fg=SUB, font=(FONT, 10, "bold"))
        self.status.pack(side="left")
        self.b_bot = self._b(top, "🤖 봇 시작", self.toggle_bot, CARD)
        self.b_scan = self._b(top, "🔍 즉시 스캔", self.scan_now, CARD)
        self.b_auto = self._b(top, "자동매매: --", self.toggle_auto, CARD)
        self.b_eng = self._b(top, "▶ 엔진 시작", self.toggle_engine, ACCENT, dark=True)
        self.b_mode = self._b(top, "계좌 전환", self.switch_mode, YELLOW, dark=True)

        self.mode_banner = tk.Label(self.root, text="", bg=ACCENT, fg="#11111b",
                                    font=(FONT, 11, "bold"), padx=12, pady=7, anchor="w")
        self.mode_banner.pack(fill="x", padx=16, pady=(0, 8))

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
        _, self.c_cash = self._card(cards, "💰 예수금")
        _, self.c_eval = self._card(cards, "📊 총평가금액")
        _, self.c_pnl = self._card(cards, "📈 평가손익")
        _, self.c_cnt = self._card(cards, "📦 보유 종목")

        head = tk.Frame(f, bg=BG)
        head.pack(fill="x", pady=(8, 2), padx=8)
        tk.Label(head, text="보유 종목 (국내/미국 모의투자 계좌)", bg=BG, fg=FG,
                 font=(FONT, 11, "bold")).pack(side="left")
        tk.Button(head, text="↻ 새로고침", command=self.refresh_port, bg=CARD, fg=FG,
                  relief="flat", cursor="hand2", padx=10, pady=3, bd=0).pack(side="right")

        cols = ("시장", "종목", "수량", "평단가", "현재가", "평가손익", "수익률")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=12)
        widths = (60, 190, 70, 115, 115, 130, 90)
        for c, w in zip(cols, widths):
            self.tree.heading(c, text=c)
            self.tree.column(c, anchor="center", width=w)
        self.tree.tag_configure("pos", foreground=GREEN)
        self.tree.tag_configure("neg", foreground=RED)
        self.tree.tag_configure("odd", background=BG2)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        # 포트폴리오 비중 막대
        self.alloc = tk.Canvas(f, bg=PANEL, height=96, highlightthickness=0)
        self.alloc.pack(fill="x", padx=8, pady=(0, 10))

    def _tab_journal(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📒 매매일지")
        head = tk.Frame(f, bg=BG)
        head.pack(fill="x", pady=(12, 2), padx=8)
        self.journal_title = tk.Label(head, text="", bg=BG, fg=FG,
                                      font=(FONT, 11, "bold"))
        self.journal_title.pack(side="left")
        tk.Button(head, text="↻ 새로고침", command=self.refresh_journal, bg=CARD, fg=FG,
                  relief="flat", cursor="hand2", padx=10, pady=3, bd=0).pack(side="right")
        tk.Button(head, text="🗑 실패기록 정리", command=self.clear_failed_journal, bg=CARD, fg=YELLOW,
                  relief="flat", cursor="hand2", padx=10, pady=3, bd=0).pack(side="right", padx=4)
        cols = ("모드", "시각", "시장", "종목", "구분", "수량", "가격", "금액", "실현손익", "사유", "결과")
        widths = (55, 135, 60, 90, 50, 55, 90, 110, 110, 130, 55)
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
        head = tk.Frame(f, bg=BG)
        head.pack(fill="x", padx=8, pady=6)
        tk.Label(head, text="스크리너 발굴 종목 · 시그널", bg=BG, fg=FG,
                 font=(FONT, 11, "bold")).pack(side="left")
        tk.Button(head, text="↻ 스캔", command=self.scan_now, bg=CARD, fg=FG,
                  relief="flat", cursor="hand2", padx=10, pady=3, bd=0).pack(side="right")
        cols = ("종목", "판정", "매수점수", "매도점수", "ADX", "추세", "가격")
        widths = (110, 80, 80, 80, 70, 70, 110)
        self.sig_tree = ttk.Treeview(f, columns=cols, show="headings", height=16)
        for c, w in zip(cols, widths):
            self.sig_tree.heading(c, text=c)
            self.sig_tree.column(c, anchor="center", width=w)
        self.sig_tree.tag_configure("buy", foreground=GREEN)
        self.sig_tree.tag_configure("sell", foreground=RED)
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
        tk.Button(r, text="⚠ 전체청산", command=self.liquidate, bg="#888", fg="#000",
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
        r = row("모의 계좌")
        self.set_paper_account = tk.Entry(r, width=24)
        self.set_paper_account.insert(0, s.paper_account)
        self.set_paper_account.pack(side="left")
        tk.Label(r, text="KIS_PAPER_ACCOUNT", bg=PANEL, fg="#888").pack(side="left", padx=8)
        r = row("실전 계좌")
        self.set_real_account = tk.Entry(r, width=24)
        self.set_real_account.insert(0, s.real_account)
        self.set_real_account.pack(side="left")
        tk.Label(r, text="KIS_REAL_ACCOUNT", bg=PANEL, fg=RED).pack(side="left", padx=8)
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
        tk.Label(box, text="※ 앱키/시크릿은 보안상 .env 파일에서 직접 수정하세요. 계좌는 모의/실전으로 분리 저장됩니다.",
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
            self.cfg_status.config(text=f"✅ 필수 설정 정상 | 현재 적용 계좌: {self.settings.active_account_key}",
                                   fg=GREEN)

    def save_settings(self):
        try:
            new_mode = self.set_mode.get()
            paper_account = self.set_paper_account.get().strip()
            real_account = self.set_real_account.get().strip()
            # 실전 전환 시 강력 경고
            if new_mode == "real" and self.settings.mode != "real":
                if not messagebox.askyesno(
                        "⚠️ 실전투자 전환",
                        "real(실전) 모드는 실제 돈으로 자동 주문이 나갑니다.\n"
                        "검증되지 않은 전략은 손실 위험이 큽니다.\n정말 전환하시겠습니까?"):
                    self.set_mode.set(self.settings.mode)
                    return
            update_env({"TRADING_MODE": new_mode,
                        "KIS_PAPER_ACCOUNT": paper_account,
                        "KIS_REAL_ACCOUNT": real_account,
                        "TELEGRAM_ALLOWED_CHAT_IDS": self.set_chat.get().strip(),
                        "TELEGRAM_BOT_TOKEN": self.set_token.get().strip()})
            self._reload_after_mode_change()
            messagebox.showinfo(
                "저장 완료",
                f"저장되었습니다. 현재 모드: {self.settings.mode}\n"
                f"적용하려면 ‘엔진 시작’(필요시 ‘봇 시작’)을 다시 누르세요.")
            self.q.put(("log", f"설정 저장됨 (모드={self.settings.mode}) — 엔진 재시작 필요"))
        except Exception as e:  # noqa
            messagebox.showerror("저장 실패", str(e))

    def switch_mode(self):
        current = self.settings.mode
        new_mode = "real" if current != "real" else "paper"
        if new_mode == "real":
            if not messagebox.askyesno(
                    "⚠️ 실전 계좌로 전환",
                    "실전 계좌 화면으로 전환합니다.\n"
                    "엔진과 봇은 중지되고, 이후 주문은 KIS_REAL_ACCOUNT 기준으로 나갈 수 있습니다.\n"
                    "정말 실전으로 전환하시겠습니까?"):
                return
        update_env({"TRADING_MODE": new_mode})
        self._reload_after_mode_change()
        self.q.put(("log", f"계좌 모드 전환: {current} → {new_mode}"))
        messagebox.showinfo("전환 완료", f"{mode_label(new_mode)} 계좌 모드로 전환했습니다.\n엔진을 다시 시작하세요.")

    def _reload_after_mode_change(self):
        """모드/계좌 변경 뒤 이전 엔진·봇 인스턴스를 버리고 화면을 새 모드로 다시 맞춘다."""
        token_removed = clear_token_cache()
        if self.trader and self.trader.running:
            self.trader.stop()
        if self.bot and self.bot.is_running:
            self.bot.stop()
        self.trader = None
        self.bot = None
        self.settings = load_settings()
        self.b_eng.config(text="▶ 엔진 시작", bg=ACCENT)
        self.b_bot.config(text="🤖 봇 시작", bg=CARD)
        if hasattr(self, "set_mode"):
            self.set_mode.set(self.settings.mode)
        self._cfg_status()
        self._refresh()
        self.refresh_journal()
        self.show_report("daily")
        if token_removed:
            self.q.put(("log", "KIS 토큰 캐시 삭제됨 — 새 모드/계좌에서 재발급"))

    # ---- 엔진/봇 ---- #
    def _ensure(self):
        if self.trader is None:
            self.trader = Trader(self.settings, notify=self._notify, journal=self.journal)
        return self.trader

    # ---- 매매일지 / 리포트 ---- #
    def refresh_journal(self):
        mode = self.settings.mode
        threading.Thread(target=lambda: self.q.put(("journal", self.journal.recent(300, mode))),
                         daemon=True).start()

    def clear_failed_journal(self):
        if not messagebox.askyesno("실패기록 정리",
                                   f"{mode_label(self.settings.mode)} 모드의 실패 기록만 삭제합니다.\n"
                                   "실제 매수/매도 성공 기록은 보존됩니다. 진행할까요?"):
            return

        def work():
            n = self.journal.clear_failed(self.settings.mode)
            self.q.put(("log", f"{mode_label(self.settings.mode)} 실패/미체결 기록 {n}건 정리"))
            self.refresh_journal()
            self.show_report("daily")
        threading.Thread(target=work, daemon=True).start()

    def show_report(self, period):
        def work():
            if period == "weekly":
                start, title = week_start_str(), f"{mode_label(self.settings.mode)} 주간 리포트 (최근 7일)"
            else:
                start, title = today_str(), f"{mode_label(self.settings.mode)} 일일 리포트 (오늘)"
            summ = self.journal.summary(start, today_str(), self.settings.mode)
            chart = self.journal.daily_pnl(14, self.settings.mode)
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
        if not messagebox.askyesno("전체청산", "국내/미국 보유 종목을 모두 시장가 청산할까요?"):
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
        threading.Thread(target=lambda: self.q.put(("portfolio", t.portfolio_balance())), daemon=True).start()

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
            mode = mode_label(r.get("mode"))
            tags = []
            if pnl is not None:
                tags.append("pos" if pnl > 0 else "neg")
            elif idx % 2:
                tags.append("odd")
            self.jtree.insert("", "end", tags=tags, values=(
                mode, r["ts"], mkt, f"{r['name']}({r['code']})", side, r["qty"],
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
        has_usd = any(p.get("currency") == "USD" for p in positions) or bool(bal.get("cash_usd"))
        pnl_values = [_to_float(bal.get("pnl_krw")), _to_float(bal.get("pnl_usd"))]
        non_zero_pnl = [v for v in pnl_values if v]
        pnl_color = FG
        if non_zero_pnl and all(v > 0 for v in non_zero_pnl):
            pnl_color = GREEN
        elif non_zero_pnl and all(v < 0 for v in non_zero_pnl):
            pnl_color = RED

        self.c_cash.config(
            text=_format_balance_lines(bal.get("cash_krw"), bal.get("cash_usd"), has_usd)
        )
        self.c_eval.config(
            text=_format_balance_lines(bal.get("total_eval_krw"), bal.get("total_eval_usd"), has_usd)
        )
        self.c_pnl.config(
            text=_format_balance_lines(bal.get("pnl_krw"), bal.get("pnl_usd"), has_usd, signed=True),
            fg=pnl_color,
        )
        self.c_cnt.config(text=f"{len(positions)}종목")

        for i in self.tree.get_children():
            self.tree.delete(i)
        for idx, p in enumerate(positions):
            rate = p["pnl_rate"]
            tag = "pos" if rate > 0 else "neg" if rate < 0 else ""
            tags = [tag] if tag else []
            if idx % 2:
                tags.append("odd")
            currency = p.get("currency", "KRW")
            self.tree.insert("", "end", tags=tuple(tags), values=(
                p.get("market_label", "-"), f"{p['name']}({p['code']})", p["qty"],
                _format_money(p["avg_price"], currency),
                _format_money(p["cur_price"], currency),
                _format_money(p.get("pnl_amt", 0), currency, signed=True),
                f"{rate:+.2f}%",
            ))
        self._draw_alloc(positions)

    def _draw_alloc(self, positions):
        c = self.alloc
        c.delete("all")
        w = c.winfo_width() or 980
        h = c.winfo_height() or 96
        groups = []
        for currency, label in (("KRW", "국내 비중"), ("USD", "미국 비중")):
            vals = [
                (p["name"], _to_float(p.get("position_value")))
                for p in positions
                if p.get("currency", "KRW") == currency
            ]
            vals = [(name, value) for name, value in vals if value > 0]
            total = sum(value for _, value in vals)
            if total > 0:
                groups.append((label, vals, total))
        if not groups:
            c.create_text(w / 2, h / 2, text="보유 종목 없음", fill=SUB, font=(FONT, 10))
            return
        colors = [ACCENT, GREEN, YELLOW, MAUVE, RED, "#94e2d5", "#fab387"]
        bar_w = w - 20
        row_gap = 38
        start_y = 18 if len(groups) == 1 else 10
        for row, (label, vals, total) in enumerate(groups):
            x = 10
            y0 = start_y + row * row_gap
            y1 = y0 + 18
            for i, (name, value) in enumerate(vals):
                seg = bar_w * (value / total)
                col = colors[i % len(colors)]
                c.create_rectangle(x, y0, x + seg, y1, fill=col, outline=BG)
                if seg > 55:
                    c.create_text(
                        x + seg / 2, (y0 + y1) / 2, text=f"{name} {value/total*100:.0f}%",
                        fill="#11111b", font=(FONT, 7, "bold"),
                    )
                x += seg
            c.create_text(10, y1 + 13, anchor="w", text=label, fill=SUB, font=(FONT, 8))

    def _render_sig(self, sigs):
        for i in self.sig_tree.get_children():
            self.sig_tree.delete(i)
        # 매수>보유>매도 순으로 보기 좋게 정렬
        order = {"buy": 0, "sell": 1, "hold": 2}
        rows = sorted(sigs.items(), key=lambda kv: (order.get(kv[1].action, 3), -kv[1].score_buy))
        for code, s in rows:
            e = {"buy": "📈매수", "sell": "📉매도", "hold": "⏸보유"}.get(s.action, s.action)
            adx = getattr(s, "adx", 0) or 0
            trend = "▲상승" if getattr(s, "trend_ok", True) else "▼하락"
            tag = s.action if s.action in ("buy", "sell") else ""
            self.sig_tree.insert("", "end", tags=(tag,), values=(
                code, e, f"{s.score_buy:.0f}", f"{s.score_sell:.0f}",
                f"{adx:.0f}", trend, f"{s.price:,.2f}"))

    def _refresh(self):
        s = self.settings
        run = self.trader and self.trader.running
        auto = self.trader and self.trader.auto_enabled
        bot = self.bot and self.bot.is_running
        if s.is_paper:
            title = "📈 모의 자동매매 대시보드"
            banner = "모의투자 화면 | KIS_PAPER_ACCOUNT 적용 | 실전 계좌와 매매기록 분리"
            banner_bg = ACCENT
            banner_fg = "#11111b"
            switch_text = "🔁 실전 계좌로 전환"
            switch_bg = RED
            title_fg = ACCENT
        else:
            title = "⚠ 실전 자동매매 대시보드"
            banner = "실전투자 화면 | KIS_REAL_ACCOUNT 적용 | 실제 주문 가능"
            banner_bg = RED
            banner_fg = "#11111b"
            switch_text = "🔁 모의 계좌로 복귀"
            switch_bg = GREEN
            title_fg = RED
        self.title.config(text=title, fg=title_fg)
        self.mode_banner.config(text=banner, bg=banner_bg, fg=banner_fg)
        self.b_mode.config(text=switch_text, bg=switch_bg, fg="#11111b")
        if hasattr(self, "journal_title"):
            self.journal_title.config(
                text=f"{mode_label(s.mode)} 매매일지 (현재 모드 기록만 표시, 체결 성공만 리포트 반영)",
                fg=title_fg,
            )
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
