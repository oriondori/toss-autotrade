"""변동성 돌파 전략 (래리 윌리엄스).

당일 시가 + K x (전일 고가 - 전일 저가) 를 돌파하면 매수,
당일 종가에 청산하는 단순 데이트레이딩 전략.
"""


class VolatilityBreakout:
    name = "변동성돌파"

    def __init__(self, k: float = 0.5) -> None:
        self.k = k

    def entry_price(self, prev: dict, today: dict) -> float | None:
        """오늘 매수가 발생하면 진입가, 아니면 None.

        prev/today: {'open','high','low','close','volume'} (일봉)
        """
        target = today["open"] + self.k * (prev["high"] - prev["low"])
        if prev["high"] == prev["low"]:  # 변동성 0 (거래정지 등) 은 스킵
            return None
        return target if today["high"] >= target else None
