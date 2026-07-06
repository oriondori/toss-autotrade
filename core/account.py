"""계좌·보유자산 조회 (X-Tossinvest-Account 헤더 필요)."""
from typing import Any

from .http import TossClient


def first_account_seq(accts) -> str:
    """계좌 응답에서 첫 계좌의 seq 추출 (다양한 응답 구조 대응)."""
    if isinstance(accts, dict):
        lst = (accts.get("result") or accts.get("accounts")
               or accts.get("data") or [])
    else:
        lst = accts
    if not isinstance(lst, list) or not lst:
        raise ValueError(f"계좌 리스트를 찾지 못함: {str(accts)[:200]}")
    item = lst[0]
    for key in ("accountSeq", "seq", "accountNo", "accountNumber", "id"):
        if isinstance(item, dict) and key in item:
            return str(item[key])
    raise ValueError(f"accountSeq 필드를 찾지 못함: {str(item)[:200]}")


class AccountApi:
    def __init__(self, client: TossClient) -> None:
        self.c = client
        self._accounts_cache: Any = None  # ACCOUNT 그룹은 1 TPS → 반드시 캐싱

    def accounts(self, refresh: bool = False) -> Any:
        """계좌 목록. accountSeq 확인용."""
        if self._accounts_cache is None or refresh:
            self._accounts_cache = self.c.get("/api/v1/accounts", "ACCOUNT")
        return self._accounts_cache

    def holdings(self) -> Any:
        """보유 주식 (종목별 상세 + 평가손익). account_seq 설정 필요."""
        return self.c.get("/api/v1/holdings", "ASSET", need_account=True)

    def buying_power(self, currency: str = "KRW") -> int:
        """주문가능금액(예수금). currency: 'KRW' | 'USD'"""
        r = self.c.get("/api/v1/buying-power", "ORDER",
                       {"currency": currency}, need_account=True)
        val = (r.get("result") or {}).get("cashBuyingPower", 0)
        return int(float(val))
