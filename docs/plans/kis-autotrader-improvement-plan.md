# KIS 자동매매 개선 업데이트 계획

## 기준 자료
- `C:\Users\demon\Downloads\kis_autotrader_analysis.md`
- 현재 저장소 `main` 체크아웃

## 핵심 판단
- 권장 실행 경로는 여전히 `run_app.bat` -> `autotrader.py`이지만, `app.py`/`run.py`가 사용하는 `src/` 모듈도 함께 보강해야 모의/실전 전환 시 동작 차이가 줄어든다.
- 이번 업데이트는 실거래 영향이 큰 Critical 항목을 먼저 줄이고, 전략 단일화와 운영 안정성은 후속 단계로 유지한다.

## 로드맵
1. [x] 1차 안정화: KIS 유량 제한 통일, 해외 거래소별 잔고 병합, 미국 포지션 전체청산, 트레일링 스탑 모듈 적용, 모드 전환 토큰 캐시 정리
2. [x] 2차 전략 단일화: `src/strategy.py`에 ADX, 레짐 필터, 신고가 돌파, 변동성 돌파 기준 보정 반영
3. [x] 3차 USD 안정화: 해외 예수금 필드 우선순위 확대, 매수가능금액 기반 해외 포지션 사이징 강화
4. [x] 4차 운영 안정성: 일일 손실/MDD 상태 영속화(JSON + SQLite `equity_state`)
5. [x] 5차 문서 대비 보강: 설정 재귀 병합, 국내/미국 스크리너, 종목별 처리 타임아웃, 텔레그램 중지/표시 문구 정리
6. [ ] 6차 유지보수: `autotrader.py`와 `src/` 중복 축소

## 이번 구현 범위
- `src/kis_api.py`의 모의/실전 RateLimiter를 `autotrader.py`와 동일한 정책으로 맞춘다.
- `src/trader.py.safe_overseas_balance()`가 NAS/NYS/AMS 기본 거래소를 조회하고 중복 포지션을 합친다.
- `src/trader.py.liquidate_all()`이 국내와 미국 포지션을 모두 청산 대상으로 포함한다.
- `src/risk.py.should_exit()`에 트레일링 스탑을 추가하고 `src/trader.py`가 보유 고점 `_peak`를 전달한다.
- 모드 전환 시 `token.dat`를 삭제해 이전 모드 토큰 파일이 남지 않게 한다.

## 1차 구현 결과
- `src/kis_api.py`와 `autotrader.py`의 KIS 호출 제한을 모의 초당 3건/0.4초 간격, 실전 초당 15건/0.07초 간격으로 통일한다.
- 해외 잔고는 NAS/NYS/AMS 기본 거래소를 모두 조회하고, 포지션은 거래소+종목 기준으로 중복 제거한다.
- 해외 USD 현금은 거래소별 조회에서 중복 합산하지 않고 최대값을 사용하며, 총평가는 현금+포지션 평가액으로 재계산한다.
- `liquidate_all()`은 국내와 미국 포지션을 함께 청산 대상으로 반환한다.
- `app.py`와 `autotrader.py` 모두 설정 저장/계좌 전환 시 `token.dat`를 삭제한다.

## 2차 구현 결과
- `src/indicators.py`에 ADX 근사값과 신고가 계산 함수를 추가한다.
- `src/strategy.py`에 `new_high`, `adx`, `regime` 설정을 반영한다.
- 변동성 돌파 목표가는 현재가 조회의 당일 시가가 있으면 그 값을 우선 사용한다.
- `src/trader.py`는 국내/미국 시그널 평가 시 현재가와 당일 시가를 함께 넘긴다.

## 3차 구현 결과
- `src/config.py`가 `screener` 설정을 노출해 `overseas_use_buyable`을 사용할 수 있게 한다.
- `src/kis_api.py`에 해외주식 매수가능금액 조회 `overseas_buyable()`을 추가한다.
- `src/trader.py`는 해외 매수 시 매수가능금액/주문가능수량을 우선 조회하고, 리스크 산정 수량을 주문가능수량으로 제한한다.

## 4차 구현 결과
- `src/risk.py`와 `autotrader.py`의 리스크 상태를 `logs/risk_state_<mode>.json`과 SQLite `equity_state` 테이블에 저장한다.
- 일일 시작 자산, 최고 자산, 거래 중단 상태가 모의/실전 모드별로 분리 보존된다.
- 날짜가 바뀌면 일일 시작 자산과 당일 중단 상태는 새 일자로 초기화하고, MDD 기준 최고 자산은 유지한다.

## 5차 구현 결과
- `autotrader.py.load_settings()`는 `config.yaml` 부분 설정을 재귀 병합해 중첩 기본값이 사라지지 않게 했다.
- `src/kis_api.py`에 국내 거래량 순위 `domestic_volume_rank()`와 미국 조건검색 `overseas_search()`를 추가했다.
- `autotrader.py`와 `src/trader.py` 모두 스크리너가 켜지면 국내/미국 후보를 동적으로 발굴하고, 실패 시 기존 고정 유니버스로 되돌린다.
- 국내/미국 종목 처리에 `process_timeout_sec`를 적용해 특정 종목 처리가 지연돼도 다음 종목으로 넘어간다.
- 청산 후 쿨다운 키를 국내/미국 매수 경로에 맞춰 적용해 즉시 재진입을 차단한다.
- 텔레그램 봇 중지 시 종료 future와 스레드 join을 대기하고, 재시작 시 새 컨트롤러를 만든다.
- 텔레그램 `/status`, `/help`, `/liquidate` 문구는 국내/미국 통합 처리 기준으로 정리했다.
- `strategy_domestic`/`strategy_overseas` override를 추가해 국내/미국 전략 임계값과 지표 설정을 분리 적용한다.
- 텔레그램 `/daily` 명령으로 오늘 매매기록 기준 P&L을 즉시 조회한다.
- `README.md`에 권장 실행 경로, 모듈 분리형 실행 경로, 최신 유량/전략/전체청산 동작을 반영했다.

## 이번 범위에서 제외
- 실제 주문/청산 실행 검증
- KIS 네트워크 API 실조회
- `autotrader.py`와 `src/` 중복 구조의 대규모 축소
