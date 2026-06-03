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


def cmd_backtest(code: str, full_validation: bool = False, mc_sims: int = 1000):
    s = load_settings()
    from src.backtest import load_history, run_backtest
    history_market = "domestic" if code.isdigit() else "overseas"
    cost_market = "kosdaq" if code.isdigit() else "overseas"
    print(f"{code} 과거 데이터 로딩...")
    candles = load_history(code, start="2023-01-01", market=history_market)
    if not candles:
        print("데이터를 가져오지 못했습니다.")
        return
    print(f"{len(candles)}봉 로드. 백테스트 실행...")
    strategy_cfg = s.strategy_domestic if history_market == "domestic" else s.strategy_overseas
    res = run_backtest(
        candles,
        strategy_cfg,
        market=cost_market,
        costs_cfg=s.costs,
        rf=s.costs.get("risk_free_rate", 0.025),
    )
    print("\n===== 백테스트 결과 =====")
    for k, v in res.items():
        if k not in ("trades", "equity_curve", "returns", "validation"):
            print(f"{k:22}: {v}")

    if full_validation:
        print("\nFull Validation 실행 중... (PSR·MC·WFA 포함, 수 분 소요 가능)")
        from src.backtest import validate_backtest
        from src.wfa import param_stability_score, run_wfa

        validation_cfg = dict(s.validation or {})
        validation_cfg["min_oos_trades"] = max(validation_cfg.get("min_oos_trades", 30), 10)
        validation = validate_backtest(res, validation_cfg, n_mc_sims=mc_sims)

        print("\n===== Validation 결과 =====")
        if not validation["calculated"]:
            print(f"계산 불가: {validation['reason']}")
        else:
            print(f"PSR (Sharpe>0 확률) : {validation.get('psr', 'N/A')}")
            print(f"MC 95% MDD         : {validation.get('mdd_mc_p95', 'N/A')}")
            print(f"Prob of Ruin       : {validation.get('prob_of_ruin', 'N/A')}")
            print("\n게이트별 결과:")
            for gate, info in validation.get("gates", {}).items():
                icon = "PASS" if info["pass"] else "FAIL"
                print(f"  {icon} {gate:20} {info['value']} (기준: {info['threshold']})")
            go = "GO" if validation["go_no_go"] else f"NO-GO ({validation.get('reason', '')})"
            print(f"\n최종 판정: {go}")

        def _wfa_backtest(sample_candles, params):
            sample = run_backtest(
                sample_candles,
                params,
                market=cost_market,
                costs_cfg=s.costs,
                use_cs_slippage=False,
                rf=s.costs.get("risk_free_rate", 0.025),
            )
            returns = sample.get("returns", [])
            return {
                "returns": returns,
                "trades": sample.get("trades", []),
                "sharpe": _simple_sharpe(returns),
                "total_return_pct": sample.get("total_return_pct", 0),
            }

        wfa_result = run_wfa(
            candles,
            [strategy_cfg],
            _wfa_backtest,
            min_is_trades=1,
            min_oos_trades=validation_cfg.get("min_oos_trades", 10),
        )
        print("\n===== WFA 결과 =====")
        if not wfa_result.calculated:
            print(f"계산 불가: {wfa_result.reason}")
        else:
            print(f"WFE                : {wfa_result.wfe}")
            print(f"OOS 거래수         : {wfa_result.n_oos_trades}")
            stability = param_stability_score(wfa_result.best_params_history)
            if stability:
                print(f"파라미터 안정성    : {stability}")

    print("\n※ 과거 성과가 미래를 보장하지 않습니다. 수수료/슬리피지 반영됨.")


def _simple_sharpe(returns):
    """CLI WFA용 간단 Sharpe 계산."""
    if not returns or len(returns) < 2:
        return 0.0
    import math

    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0


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
            print("사용법: python run.py backtest <종목코드> [--full-validation] [--mc-sims N]")
        else:
            full_validation = "--full-validation" in args
            mc_sims = 1000
            if "--mc-sims" in args:
                index = args.index("--mc-sims")
                if index + 1 < len(args):
                    mc_sims = int(args[index + 1])
            cmd_backtest(args[1], full_validation=full_validation, mc_sims=mc_sims)
    else:
        auto = "--no-auto" not in args
        cmd_run(auto)
