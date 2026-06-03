# 검증 결과

## 2026-06-03 6차 전략 검증 고도화

### 검증 명령
```powershell
py -m compileall autotrader.py app.py run.py src tests
py -m unittest discover -s tests -p "test_*.py" -v
git diff --check

.\.venv\Scripts\python.exe run.py backtest 005930
.\.venv\Scripts\python.exe run.py backtest 005930 --full-validation --mc-sims 200
```

### 결과
- `py -m compileall autotrader.py app.py run.py src tests`: OK
- `py -m unittest discover -s tests -p "test_*.py" -v`: 68 tests OK
- `git diff --check`: 종료 코드 0
- `.venv\Scripts\python.exe run.py backtest 005930`: 832봉 로드, 9 trades, `total_return_pct=37.76`, `mdd_pct=-37.77`
- `.venv\Scripts\python.exe run.py backtest 005930 --full-validation --mc-sims 200`: validation은 `거래수 부족 (9 < 30)`, WFA는 `OOS 거래수 부족 (0 < 30)`으로 no-go 표시

### 참고
- 기본 `py` 환경에는 `FinanceDataReader`가 없어 CLI 백테스트가 실패했으므로, 프로젝트 로컬 `.venv`를 생성하고 `requirements.txt` 의존성을 설치해 CLI 검증을 수행했다.
- `.venv`는 로컬 실행 환경이며 커밋 대상이 아니다.
- KIS 네트워크 실조회, 실전 주문, 실제 청산은 수행하지 않았다.
- `git diff --check`에서 CRLF 변환 경고가 표시될 수 있으나 whitespace 오류는 없었다.

## 2026-06-02 KIS 자동매매 개선 1-5차

### 검증 명령
```powershell
py -m unittest discover -s tests -p "test_*.py"
py -m compileall autotrader.py app.py run.py src tests
git diff --check
```

### 결과
- `py -m unittest discover -s tests -p "test_*.py"`: 35 tests OK
- `py -m compileall autotrader.py app.py run.py src tests`: OK
- `git diff --check`: 종료 코드 0

### 참고
- `git diff --check`에서 `LF will be replaced by CRLF` 경고가 표시됐지만 whitespace 오류는 없었다.
- KIS 네트워크 실조회, 실전 주문, 실제 청산은 수행하지 않았다.
- 별도 시크릿 스캔 스크립트는 저장소에 없으며, 이번 변경 파일에 `.env`는 포함되지 않았다.
- `tests/test_strategy.py`, `tests/test_risk.py`, `tests/test_rate_limiter.py`를 추가해 문서 명세의 테스트 영역을 분리 검증했다.
