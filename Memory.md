# Memory.md

## 현재 프로젝트 상태 (2026-06-03, 6차 개선 완료)
- Tkinter 기반 한국투자증권 KIS 자동매매 앱이며 권장 GUI 실행 경로는 `run_app.bat` -> `autotrader.py`이다.
- 전략: `src/strategy.py`가 master이고 `autotrader.py`의 `CompositeStrategy`는 이를 호출하는 호환 래퍼다.
- 전략 판정: 보수적 4단계 게이트(레짐 필터 -> ADX 추세 강도 -> 변동성 돌파/신고가 1차 트리거 -> RSI/MA/MACD/볼린저 보조 확인)를 사용한다.
- 백테스트: 신호 봉 종가가 아닌 다음 봉 시가 체결, 비용/슬리피지 분리 반영, `returns`/`equity_curve`/거래별 비용 상세를 반환한다.
- Full validation: PSR, Monte Carlo MDD/파산확률, WFA 결과를 CLI `--full-validation`에서 확인한다.
- 신규 모듈: `src/costs.py`, `src/metrics.py`, `src/slippage.py`, `src/montecarlo.py`, `src/wfa.py`.
- `config.yaml`과 `src/config.py`에 `costs`와 `validation` 섹션이 추가됐다.
- 비용 기준값: 기준금리 2.5%, 국내 거래세 0.20%, 국내 수수료 0.015%, 유관기관제비용 0.0036396%, 최소 슬리피지 5bps.
- 검증 게이트 기준값: 최소 OOS 거래수 30, PSR 0.95, WFE 0.50, PBO 0.50, DSR 0.95, Probability of Ruin 0.05.
- 실전 주문, 실제 청산, 계좌 자금 이동, KIS 실조회는 이번 작업에서 수행하지 않았다.

## 변경 파일
- [x] `autotrader.py`: 해외 잔고 NAS/NYS/AMS 병합, `src.strategy` 래퍼 전환, `confirm_threshold` 기본값 반영
- [x] `config.yaml`: `costs`, `validation`, `confirm_threshold` 설정 추가
- [x] `src/config.py`: `Settings.costs`, `Settings.validation` 노출
- [x] `src/costs.py`: 국내/해외 매수·매도 비용 계산
- [x] `src/metrics.py`: Sharpe, Sortino, Calmar, MDD, Omega, PSR, DSR 계산
- [x] `src/slippage.py`: Corwin-Schultz 스프레드와 변동성 슬리피지 추정
- [x] `src/montecarlo.py`: reshuffle, block bootstrap, probability of ruin
- [x] `src/wfa.py`: rolling/anchored WFA, WFE, 파라미터 안정성
- [x] `src/backtest.py`: next-bar 체결, 비용/슬리피지, validation payload
- [x] `src/strategy.py`: 보수적 게이트 전략
- [x] `run.py`: `--full-validation`, `--mc-sims` 옵션
- [x] `tests/test_backtest_validation.py`: 비용/성과지표/슬리피지/MC/WFA/백테스트 규칙 테스트
- [x] `tests/test_strategy.py`, `tests/test_strategy_alignment.py`, `tests/test_portfolio_snapshot.py`: 전략/설정/잔고 회귀 테스트 보강
- [x] `README.md`, `docs/plans/kis-autotrader-improvement-plan.md`, `docs/VALIDATION.md`: 6차 개선 상태 반영

## 최신 검증 결과
- [x] `py -m compileall autotrader.py app.py run.py src tests`: OK
- [x] `py -m unittest discover -s tests -p "test_*.py" -v`: 68 tests OK
- [x] `git diff --check`: 종료 코드 0
- [x] `.venv\Scripts\python.exe run.py backtest 005930`: 832봉 로드, 9 trades, total_return_pct 37.76
- [x] `.venv\Scripts\python.exe run.py backtest 005930 --full-validation --mc-sims 200`: 거래수 부족으로 validation/WFA no-go 표시

## 남은 작업
- [ ] GUI 재시작 후 보수적 게이트 전략의 시그널 탭 표시를 화면에서 확인한다.
- [ ] 실제 KIS 모의 계좌로 국내/미국 잔고가 의도대로 표시되는지 읽기 전용으로 확인한다.
- [ ] CPCV/PBO는 다종목 풀링과 충분한 OOS 표본 확보 후 별도 모듈로 재검토한다.
- [ ] 분봉 수집은 별도 데이터 인프라가 필요하므로 이번 범위에서 제외한다.

## 주의 사항
- `.env`, `trades.db`, `logs/`, `.venv/`는 커밋 대상이 아니다.
- `codex_prompts_backtest.md`는 현재 작업 지시 원본으로 untracked 상태다.
- 실전 주문, 주문 취소, 서비스 계정/시크릿 변경은 사용자가 명시적으로 요청한 경우에만 수행한다.
