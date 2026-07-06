# toss-autotrade — Step 1: 인증 + 조회 (읽기 전용)

주문 기능 없음. 전부 조회 API라 안전하게 실행할 수 있습니다.

## 오늘 할 일 (실행 순서)

```bash
# 1. Python 3.10+ 확인
python --version

# 2. 가상환경 + 패키지 설치
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 3. 키 설정 — .env.example 을 .env 로 복사 후 값 입력
copy .env.example .env        # 편집기로 열어 client_id/secret 입력

# 4. 연동 검증 실행
python quickstart.py

# 5. 대시보드 실행 (키 없어도 동작)
python main.py        # → http://localhost:8000
```

성공 시: 토큰 발급 → 삼성전자 현재가 → 장 캘린더 → 계좌 목록 → 보유 주식 순으로 출력됩니다.

## 구조

```
core/auth.py       토큰 발급·캐싱·자동갱신
core/ratelimit.py  그룹별 TPS 제한 준수
core/http.py       공통 클라이언트 (401/429 자동 처리)
core/market.py     시세·종목·환율·캘린더 조회
core/account.py    계좌·보유주식 조회
data/db.py         SQLite (시그널·주문·손익·로그·캔들)
webapp/server.py   FastAPI 대시보드 API
webapp/static/     대시보드 화면
main.py            진입점 (봇 루프 + 웹서버, :8000)
quickstart.py      Step 1 검증 스크립트
```

## 다음 단계
- Step 2: 캔들 수집 + SQLite 저장 (`data/`)
- Step 3: 백테스트 + 전략
- 상세 계획: `../02_전체구조_기술사항_WBS.md`
