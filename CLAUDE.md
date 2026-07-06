# TossAutoTrade — Claude Code 컨텍스트

토스증권 Open API 자동매매 봇 + FastAPI 대시보드(:8000). 상세 인수인계: `../00_인수인계서.md` **필독**.

## 현재 상태
- Phase 1~3 완료: API 연동(실검증됨) · 캔들 수집(005930/000660 3년 일봉) · 백테스트 엔진+변동성돌파
- 다음: P4 페이퍼 트레이딩 (engine/scheduler·executor·paper, risk/manager, config.yaml)

## 핵심 규칙
- 실행: `실행.bat`(서버) `수집.bat` `백테스트.bat` — **배치파일은 영문 메시지만** (cmd UTF-8 한글 깨짐)
- `.env`에 실키 있음 — 커밋·출력 금지
- Sandbox 없는 실계좌 — 주문 코드는 페이퍼 모드 검증 후에만 live 활성화
- 새 토큰 발급 시 기존 토큰 무효화됨 (core/http.py가 401 자동 복구)
- ACCOUNT API 1 TPS — 캐싱 필수. Rate limit은 core/ratelimit.py 경유
- API 응답은 `{'result': ...}` envelope, 숫자가 문자열로 옴 (lastPrice 등)
- 봇 제어는 webapp/state.py 공유 상태 (paused/killed) — 새 코드도 이 상태 준수

## 테스트
- 로직 검증: DB·백테스트는 오프라인 테스트 가능 (API 불필요)
- 사용자는 비개발자 — 새 기능도 .bat 실행 방식 유지
