"""API 그룹별 TPS 제한 준수 (공식 문서 기준)."""
import time
from collections import defaultdict

# 그룹별 초당 최대 요청 수 (2026-07 공식 문서)
TPS_LIMITS = {
    "AUTH": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,
    "MARKET_DATA_CHART": 5,
    "ORDER": 6,          # 09:00~09:10 KST 는 3
    "ORDER_HISTORY": 5,
    "ORDER_INFO": 6,
}


class RateLimiter:
    """그룹별 최소 호출 간격을 보장하는 단순 리미터."""

    def __init__(self) -> None:
        self._last_call: dict[str, float] = defaultdict(float)

    def wait(self, group: str) -> None:
        limit = TPS_LIMITS.get(group, 3)
        min_interval = 1.0 / limit
        elapsed = time.time() - self._last_call[group]
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call[group] = time.time()
