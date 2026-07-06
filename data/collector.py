"""캔들 수집기 — 토스증권 캔들 API → SQLite 적재.

사용:
  python -m data.collector 005930 000660          # 일봉 3년치
  python -m data.collector 005930 --days 30       # 일봉 30일
  python -m data.collector AAPL --interval 1m --days 5
"""
import argparse
import sqlite3
import sys

from data import db


def save_candles(symbol: str, interval: str, candles: list[dict]) -> int:
    """캔들 리스트를 DB에 저장 (중복은 갱신). 저장 건수 반환."""
    rows = [
        (symbol, interval, c["timestamp"],
         float(c["openPrice"]), float(c["highPrice"]),
         float(c["lowPrice"]), float(c["closePrice"]), float(c["volume"]))
        for c in candles
    ]
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO candles (symbol,interval,ts,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def collect(market, symbol: str, interval: str = "1d", days: int = 1095) -> int:
    """페이지네이션으로 과거 캔들 수집. 총 저장 건수 반환."""
    # 1d: 봉 수 ≈ 거래일 수 (연 약 250일), 1m: 하루 약 390봉
    target = int(days * 250 / 365) + 10 if interval == "1d" else days * 400
    total, before = 0, None

    while total < target:
        params = {"interval": interval, "count": 200}
        if before:
            params["before"] = before
        resp = market.candles(symbol, **params)
        page = resp.get("result", resp)
        candles = page.get("candles", [])
        if not candles:
            break
        total += save_candles(symbol, interval, candles)
        before = page.get("nextBefore")
        print(f"  {symbol} [{interval}] {total}봉 저장 (다음: {before or '끝'})")
        if not before:
            break
    return total


def load_candles(symbol: str, interval: str = "1d", limit: int = 5000) -> list[dict]:
    """백테스트용 캔들 로드 (시간 오름차순)."""
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM candles WHERE symbol=? AND interval=? "
            "ORDER BY ts DESC LIMIT ?", (symbol, interval, limit))
        return sorted([dict(r) for r in rows], key=lambda r: r["ts"])


def main() -> None:
    p = argparse.ArgumentParser(description="토스증권 캔들 수집기")
    p.add_argument("symbols", nargs="+", help="종목 심볼 (예: 005930 AAPL)")
    p.add_argument("--interval", default="1d", choices=["1m", "1d"])
    p.add_argument("--days", type=int, default=1095, help="수집 기간(일), 기본 3년")
    args = p.parse_args()

    from core.http import TossClient
    from core.market import MarketApi

    db.init()
    market = MarketApi(TossClient())

    for sym in args.symbols:
        print(f"▶ {sym} 수집 시작 ({args.interval}, {args.days}일)")
        try:
            n = collect(market, sym, args.interval, args.days)
            db.log("INFO", "SYS", f"캔들 수집 완료: {sym} {args.interval} {n}봉")
            print(f"✅ {sym}: 총 {n}봉\n")
        except Exception as e:  # noqa: BLE001
            db.log("ERROR", "SYS", f"캔들 수집 실패: {sym} {e}")
            print(f"❌ {sym}: {e}\n")
            sys.exit(1)


if __name__ == "__main__":
    main()
