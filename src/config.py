"""환경설정(.env) 및 전략설정(config.yaml) 로더."""
import copy
import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

_BASE = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(_BASE, ".env"))

# KIS 도메인 (공식)
REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
REAL_WS_URL = "ws://ops.koreainvestment.com:21000"
PAPER_WS_URL = "ws://ops.koreainvestment.com:31000"

_DEFAULT_CONFIG = {
    "engine": {
        "loop_interval_sec": 60,
        "process_timeout_sec": 30,
        "domestic_session": "09:00-15:20",
        "overseas_session": "23:30-06:00",
        "auto_trade_enabled": True,
    },
    "screener": {
        "enabled": False,
        "overseas_use_buyable": True,
    },
    "strategy": {
        "buy_threshold": 3,
        "sell_threshold": 2,
        "indicators": {},
    },
    "strategy_domestic": {},
    "strategy_overseas": {},
    "risk": {
        "max_positions": 5,
        "risk_per_trade_pct": 1.0,
        "stop_loss_pct": 5.0,
        "take_profit_pct": 10.0,
        "trailing_stop_pct": 0.0,
        "cooldown_min": 0,
        "daily_loss_limit_pct": 3.0,
        "max_drawdown_pct": 15.0,
        "atr_period": 14,
    },
    "costs": {
        "risk_free_rate": 0.025,
        "commission_pct": 0.015,
        "exchange_fee_pct": 0.0036396,
        "tax_kospi_pct": 0.20,
        "tax_kosdaq_pct": 0.20,
        "overseas_commission_pct": 0.025,
        "min_slippage_bps": 5.0,
    },
    "validation": {
        "min_oos_trades": 30,
        "psr_min": 0.95,
        "wfe_min": 0.50,
        "pbo_max": 0.50,
        "dsr_min": 0.95,
        "ror_max": 0.05,
        "mc_ruin_threshold": 0.30,
        "mc_simulations": 1000,
    },
    "universe": {
        "domestic": [],
        "overseas": [],
    },
}


@dataclass
class Settings:
    """런타임 전역 설정."""

    mode: str                       # "paper" | "real"
    app_key: str
    app_secret: str
    account_no: str                 # "12345678" (앞 8자리)
    account_prod: str               # "01" (상품코드)
    telegram_token: str
    allowed_chat_ids: list
    strategy: dict = field(default_factory=dict)
    strategy_domestic: dict = field(default_factory=dict)
    strategy_overseas: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    costs: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    engine: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)
    screener: dict = field(default_factory=dict)
    paper_account: str = ""
    real_account: str = ""

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def base_url(self) -> str:
        return PAPER_BASE_URL if self.is_paper else REAL_BASE_URL

    @property
    def ws_url(self) -> str:
        return PAPER_WS_URL if self.is_paper else REAL_WS_URL

    @property
    def active_account_key(self) -> str:
        return "KIS_PAPER_ACCOUNT" if self.is_paper else "KIS_REAL_ACCOUNT"


def _split_account(raw: str):
    """'12345678-01' -> ('12345678', '01')"""
    raw = (raw or "").strip()
    if "-" in raw:
        no, prod = raw.split("-", 1)
        return no.strip(), prod.strip()
    return raw, "01"


def _deep_merge(base: dict, override: dict) -> dict:
    """기본 설정에 YAML 설정을 재귀 병합한다."""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_config() -> dict:
    cfg_path = os.path.join(_BASE, "config.yaml")
    if not os.path.exists(cfg_path):
        return copy.deepcopy(_DEFAULT_CONFIG)
    with open(cfg_path, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    return _deep_merge(_DEFAULT_CONFIG, loaded)


def load_settings() -> Settings:
    load_dotenv(env_path(), override=True)
    mode = os.getenv("TRADING_MODE", "paper").lower()
    paper_account = os.getenv("KIS_PAPER_ACCOUNT", "")
    real_account = os.getenv("KIS_REAL_ACCOUNT", "")

    if mode == "real":
        app_key = os.getenv("KIS_REAL_APP_KEY", "")
        app_secret = os.getenv("KIS_REAL_APP_SECRET", "")
        account_raw = real_account
    else:
        mode = "paper"
        app_key = os.getenv("KIS_PAPER_APP_KEY", "")
        app_secret = os.getenv("KIS_PAPER_APP_SECRET", "")
        account_raw = paper_account

    account_no, account_prod = _split_account(account_raw)

    chat_ids_raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    allowed = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]

    cfg = _load_config()
    base_strategy = cfg.get("strategy", {})
    strategy_domestic = _deep_merge(base_strategy, cfg.get("strategy_domestic", {}))
    strategy_overseas = _deep_merge(base_strategy, cfg.get("strategy_overseas", {}))

    return Settings(
        mode=mode,
        app_key=app_key,
        app_secret=app_secret,
        account_no=account_no,
        account_prod=account_prod,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        allowed_chat_ids=allowed,
        strategy=base_strategy,
        strategy_domestic=strategy_domestic,
        strategy_overseas=strategy_overseas,
        risk=cfg.get("risk", {}),
        costs=cfg.get("costs", {}),
        validation=cfg.get("validation", {}),
        engine=cfg.get("engine", {}),
        universe=cfg.get("universe", {}),
        screener=cfg.get("screener", {}),
        paper_account=paper_account,
        real_account=real_account,
    )


def env_path() -> str:
    return os.path.join(_BASE, ".env")


def update_env(updates: dict):
    """.env 파일의 특정 키 값을 갱신/추가 (주석·기타 줄 보존)."""
    path = env_path()
    lines = []
    seen = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    if key in updates:
                        lines.append(f"{key}={updates[key]}\n")
                        seen.add(key)
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")
    # 새로 추가되는 키
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def validate(settings: Settings) -> list:
    """필수값 검증. 비어있는 항목 리스트 반환."""
    missing = []
    if not settings.app_key or "여기에" in settings.app_key:
        missing.append("APP_KEY")
    if not settings.app_secret or "여기에" in settings.app_secret:
        missing.append("APP_SECRET")
    if not settings.account_no or settings.account_no.startswith("0000"):
        missing.append("ACCOUNT")
    if not settings.telegram_token or "여기에" in settings.telegram_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.allowed_chat_ids:
        missing.append("TELEGRAM_ALLOWED_CHAT_IDS")
    return missing
