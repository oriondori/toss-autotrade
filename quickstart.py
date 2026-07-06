"""Step 1 연동 검증 스크립트 — 전부 읽기 전용(주문 없음)이라 안전합니다.

실행 전:
  1) pip install -r requirements.txt
  2) .env.example 을 .env 로 복사 후 client_id/secret 입력
실행:
  python quickstart.py
"""
import json
import sys

from core.account import AccountApi
from core.http import ApiError, TossClient
from core.market import MarketApi


def show(title: str, data) -> None:
    print(f"\n{'=' * 50}\n■ {title}\n{'=' * 50}")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])


def main() -> None:
    print("토스증권 Open API 연동 검증을 시작합니다 (읽기 전용).")

    client = TossClient()
    market = MarketApi(client)
    account = AccountApi(client)

    # 1. 토큰 발급 (get_token 은 내부에서 자동 호출됨)
    print("\n[1/5] OAuth 토큰 발급...", end=" ")
    client.tm.get_token()
    print("OK")

    # 2. 현재가 — 삼성전자
    print("[2/5] 현재가 조회 (005930 삼성전자)...")
    show("현재가", market.prices("005930"))

    # 3. 장 운영 정보
    print("[3/5] 국내 장 운영 캘린더...")
    show("장 캘린더(KR)", market.market_calendar("KR"))

    # 4. 계좌 목록
    print("[4/5] 계좌 목록...")
    accts = account.accounts()
    show("계좌 목록", accts)

    # 5. 보유 주식 (첫 계좌 기준)
    print("[5/5] 보유 주식...")
    try:
        seq = _first_account_seq(accts)
        client.account_seq = seq
        show(f"보유 주식 (account={seq})", account.holdings())
    except Exception as e:  # 계좌 구조가 예상과 다르면 응답을 보고 수정
        print(f"  → 보유 주식 조회 생략: {e}")
        print("  → 위 '계좌 목록' 응답에서 accountSeq 값을 확인하세요.")

    print("\n✅ 연동 검증 완료! 다음 단계는 캔들 수집(Step 2)입니다.")


def _first_account_seq(accts) -> str:
    """계좌 응답에서 첫 accountSeq 추출 (응답 구조에 따라 조정)."""
    if isinstance(accts, list) and accts:
        item = accts[0]
    elif isinstance(accts, dict):
        lst = accts.get("accounts") or accts.get("data") or accts.get("result") or []
        if not lst:
            raise ValueError("계좌 리스트를 찾지 못함")
        item = lst[0]
    else:
        raise ValueError("알 수 없는 응답 구조")
    for key in ("accountSeq", "accountNo", "seq", "id"):
        if isinstance(item, dict) and key in item:
            return str(item[key])
    raise ValueError(f"accountSeq 필드를 찾지 못함: {item}")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        print(f"\n❌ API 에러: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"\n❌ 설정 오류: {e}")
        sys.exit(1)
