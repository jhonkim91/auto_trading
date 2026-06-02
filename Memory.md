# Memory.md

## 현재 프로젝트 상태
- Tkinter 기반 한국투자증권 KIS 자동매매 앱이다.
- 권장 GUI 실행 경로는 `run_app.bat` -> `autotrader.py`이다.
- 모의/실전 계좌는 `KIS_PAPER_ACCOUNT`, `KIS_REAL_ACCOUNT`, `TRADING_MODE` 기준으로 분리 적용한다.
- 대시보드는 국내 원화 잔고와 미국 달러 잔고를 분리 표시한다.
- 매매일지, 일일/주간 리포트, 실패기록 정리는 현재 모드 기록만 조회한다.
- KIS 호출 제한은 `autotrader.py`와 `src/kis_api.py` 모두 모의 초당 3건/0.4초 간격, 실전 초당 15건/0.07초 간격으로 통일했다.
- 해외 잔고는 NAS/NYS/AMS 기본 거래소를 모두 조회해 거래소+종목 기준으로 병합한다. USD 현금은 중복 합산하지 않고 거래소별 조회값의 최대값을 사용한다.
- `src/trader.py.liquidate_all()`은 국내와 미국 포지션을 모두 청산 대상으로 포함한다.
- `src/risk.py.should_exit()`은 손절, 트레일링 스탑, 익절 순서로 청산 사유를 판단한다.
- 설정 저장 또는 계좌 모드 전환 시 `token.dat`를 삭제해 이전 모드 토큰 재사용을 막는다.
- `src/strategy.py`는 신고가 돌파, ADX 약추세 차감, 레짐 필터, 현재 시가 기반 변동성 돌파 보정을 반영한다.
- 해외 자동매수는 KIS 매수가능금액/주문가능수량을 우선 조회하고 리스크 산정 수량을 주문가능수량으로 제한한다.
- 일일 손실/MDD 상태는 `logs/risk_state_<mode>.json`에 모드별로 저장한다.
- `config.yaml` 부분 설정은 `autotrader.py`와 `src/config.py`에서 기본값과 재귀 병합해 중첩 설정 누락을 막는다.
- 스크리너 활성화 시 국내 거래량 순위와 미국 조건검색 후보를 사용하고, 조회 실패 시 기존 고정 유니버스로 되돌린다.
- 종목 1개 처리 지연은 `engine.process_timeout_sec` 기준으로 제한해 다음 종목 스캔이 계속되게 한다.
- 텔레그램 봇 정지는 종료 future와 스레드 join을 대기하고, 재시작 시 새 컨트롤러를 생성한다.
- `strategy_domestic`/`strategy_overseas` override로 국내/미국 전략 파라미터를 공통 `strategy` 기본값과 분리 적용한다.
- 텔레그램 `/daily`는 오늘 매매기록 기준 P&L을 즉시 조회한다.

## 변경 파일
- [x] `autotrader.py`: KIS 호출 제한 통일, 토큰 캐시 삭제, 설정 재귀 병합, 스크리너, 현재 시가 기반 돌파, 종목별 타임아웃, `/daily`, 전체청산 문구 정리
- [x] `app.py`: 설정 저장/모드 전환 토큰 캐시 삭제, 전체청산 문구 정리, 텔레그램 봇 재시작 컨트롤러 초기화
- [x] `src/kis_api.py`: 호출 제한 상수화, 최소 호출 간격 적용, 해외 잔고 거래소 메타데이터/예수금 필드 보강, 국내/미국 스크리너 API 추가
- [x] `src/trader.py`: 해외 거래소별 잔고 병합, 해외 잔고 기반 매수 수량 계산, 국내/미국 전체청산, 보유 고점 전달, 시장별 전략, 스크리너/쿨다운/타임아웃/일일 리포트 적용
- [x] `src/risk.py`: 트레일링 스탑 판단 추가
- [x] `src/indicators.py`: ADX, 신고가, 현재 시가 기반 변동성 돌파 계산 추가
- [x] `src/strategy.py`: `new_high`, `adx`, `regime` 전략 설정 반영
- [x] `src/config.py`: `config.yaml` 누락/부분 설정에 대한 기본값 병합, `screener`/`process_timeout_sec`/시장별 전략 설정 노출
- [x] `src/telegram_bot.py`: 상태 표시, `/daily`, 전체청산 문구, 봇 정지 대기 처리 정리
- [x] `run.py`: 백테스트 시 국내/해외 전략 설정 분리 적용
- [x] `README.md`: 권장 실행 경로, 최신 전략/유량/전체청산/텔레그램 명령 반영
- [x] `tests/test_portfolio_snapshot.py`: 호출 제한, 트레일링 스탑, 해외 잔고 병합, 국내/미국 전체청산, 매수가능금액, 리스크 상태 저장, 스크리너/쿨다운 테스트 추가
- [x] `tests/test_strategy_alignment.py`: 신고가, ADX, 레짐 필터, 현재 시가 기반 변동성 돌파 테스트 추가
- [x] `tests/test_strategy.py`, `tests/test_risk.py`, `tests/test_rate_limiter.py`: 문서 명세별 전략/리스크/유량 테스트 추가
- [x] `docs/plans/kis-autotrader-improvement-plan.md`: 다운로드 분석 문서 기준 개선 로드맵 정리
- [x] `docs/VALIDATION.md`: 최신 검증 결과 기록

## 최신 검증 결과
- [x] `py -m unittest discover -s tests -p "test_*.py"`: 33 tests OK
- [x] `py -m compileall autotrader.py app.py run.py src tests`: OK
- [x] `git diff --check`: 종료 코드 0, CRLF 변환 경고만 표시

## 남은 작업
- [ ] GUI 재시작 후 모의/실전 배너, 계좌 전환 버튼, 매매일지 모드 필터링을 화면에서 확인한다.
- [ ] 실제 KIS 모의 계좌로 국내/미국 잔고가 의도대로 표시되는지 읽기 전용으로 확인한다.
- [ ] `autotrader.py`와 `src/` 중복 구조 축소는 별도 대형 리팩토링으로 검토한다.

## 주의 사항
- 이번 작업에서 실전 주문, 실제 청산, 계좌 자금 이동은 수행하지 않았다.
- 실제 API 키, 토큰, 계좌번호 등 시크릿 값은 기록하지 않는다.
