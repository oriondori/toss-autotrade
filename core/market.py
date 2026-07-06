"""시세·종목·시장 정보 조회 (토큰만 필요, 읽기 전용)."""
from typing import Any

from .http import TossClient


class MarketApi:
    def __init__(self, client: TossClient) -> None:
        self.c = client

    # --- 시세 (MARKET_DATA, 10 TPS) ---
    def prices(self, symbols: list[str] | str) -> Any:
        """현재가. symbols: '005930' 또는 ['005930', 'AAPL']"""
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        return self.c.get("/api/v1/prices", "MARKET_DATA", {"symbols": symbols})

    def orderbook(self, symbol: str) -> Any:
        return self.c.get("/api/v1/orderbook", "MARKET_DATA", {"symbol": symbol})

    def trades(self, symbol: str) -> Any:
        return self.c.get("/api/v1/trades", "MARKET_DATA", {"symbol": symbol})

    def price_limits(self, symbol: str) -> Any:
        return self.c.get("/api/v1/price-limits", "MARKET_DATA", {"symbol": symbol})

    # --- 캔들 (MARKET_DATA_CHART, 5 TPS) ---
    def candles(self, symbol: str, **params: Any) -> Any:
        """캔들(1분봉·일봉). 파라미터는 공식 스펙 참고 (interval, from, to 등)."""
        return self.c.get("/api/v1/candles", "MARKET_DATA_CHART",
                          {"symbol": symbol, **params})

    # --- 종목 (STOCK, 5 TPS) ---
    def stocks(self, symbols: list[str] | str) -> Any:
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        return self.c.get("/api/v1/stocks", "STOCK", {"symbols": symbols})

    def warnings(self, symbol: str) -> Any:
        """매수 유의사항 (정리매매·과열·투자경고·VI 등) — 주문 전 필수 체크."""
        return self.c.get(f"/api/v1/stocks/{symbol}/warnings", "STOCK")

    # --- 시장 정보 (MARKET_INFO, 3 TPS) ---
    def exchange_rate(self) -> Any:
        return self.c.get("/api/v1/exchange-rate", "MARKET_INFO")

    def market_calendar(self, region: str = "KR") -> Any:
        """region: 'KR' | 'US'"""
        return self.c.get(f"/api/v1/market-calendar/{region}", "MARKET_INFO")
