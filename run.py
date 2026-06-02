"""실행 진입점.

사용법:
  python run.py                 # 텔레그램 봇 + 자동매매 엔진 가동
  python run.py --no-auto       # 자동매매 루프 자동시작 안 함 (텔레그램에서 /auto on)
  python run.py backtest 005930 # 단일 종목 백테스트
  python run.py check           # 설정/연결 점검

처음에는 .env 의 TRADING_MODE=paper(모의) 로 충분히 검증한 뒤 real 로 전환하세요.
"""
import sys

from src.config import load_settings, validate
from src.logger import get_logger

log = get_logger("main")


def cmd_check():
    s = load_settings()
    missing = validate(s)
    print(f"거래 모드 : {s.mode}")
    print(f"BASE URL : {s.base_url}")
    print(f"계좌      : {s.account_no}-{s.account_prod}")
    print(f"감시 국내 : {s.universe.get('domestic')}")
    print(f"감시 해외 : {s.universe.get('overseas')}")
    if missing:
        print(f"\n⚠️ 누락된 설정: {', '.join(missing)} — .env 를 확인하세요.")
        return
    print("\n토큰 발급 테스트...")
    from src.kis_api import KISApi
    api = KISApi(s)
    try:
        tok = api.token()
        print(f"✅ 토큰 발급 성공 (앞 10자리: {tok[:10]}...)")
        bal = api.domestic_balance()
        print(f"✅ 잔고조회 성공: 예수금 {bal['cash']:,.0f}원, 보유 {len(bal['positions'])}종목")
    except Exception as e:
        print(f"❌ 연결 실패: {e}")


def cmd_backtest(code: str):
    s = load_settings()
    from src.backtest import load_history, run_backtest
    market = "domestic" if code.isdigit() else "overseas"
    print(f"{code} 과거 데이터 로딩...")
    candles = load_history(code, start="2023-01-01", market=market)
    if not candles:
        print("데이터를 가져오지 못했습니다.")
        return
    print(f"{len(candles)}봉 로드. 백테스트 실행...")
    strategy_cfg = s.strategy_domestic if market == "domestic" else s.strategy_overseas
    res = run_backtest(candles, strategy_cfg)
    print("\n===== 백테스트 결과 =====")
    for k, v in res.items():
        print(f"{k:18}: {v}")
    print("\n※ 과거 성과가 미래를 보장하지 않습니다. 수수료/슬리피지 반영됨.")


def cmd_run(auto: bool):
    s = load_settings()
    missing = validate(s)
    if missing:
        print(f"⚠️ 누락된 설정: {', '.join(missing)} — .env 를 먼저 채우세요.")
        return

    from src.trader import Trader
    from src.telegram_bot import TelegramController

    trader = Trader(s)
    bot = TelegramController(s, trader)

    if auto:
        trader.start()  # 자동매매 루프 시작(별도 스레드)

    log.info("텔레그램 봇 가동 (모드=%s, 자동매매=%s)", s.mode, auto)
    try:
        bot.run()  # 블로킹
    finally:
        trader.stop()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "check":
        cmd_check()
    elif args and args[0] == "backtest":
        if len(args) < 2:
            print("사용법: python run.py backtest <종목코드>")
        else:
            cmd_backtest(args[1])
    else:
        auto = "--no-auto" not in args
        cmd_run(auto)
