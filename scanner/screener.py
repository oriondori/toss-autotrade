"""종목 자동 스캐너 — 가격·변동폭·거래대금 기준 필터링 및 점수화.

점수 기준 (0~100):
  변동폭 점수 (0~50): 최근 20일 일중 평균 변동폭 % → 높을수록 변동성돌파 유리
  거래대금 점수 (0~50): 최근 일 평균 거래대금(억원) → 높을수록 슬리피지 적음
"""
import math
import threading
from datetime import datetime

from data.collector import load_candles
from scanner.universe import UNIVERSE

_lock = threading.Lock()
_state: dict = {
    "scanning": False,
    "last_scan": None,
    "results": [],
    "error": "",
}

MIN_CANDLES = 10  # 점수 산출에 필요한 최소 일봉 수


def get_state() -> dict:
    with _lock:
        state = dict(_state)
        state["results"] = [dict(r) for r in _state.get("results", [])]
    # 마지막 스캔에서 데이터 없던 종목만 DB 재확인 (수집 직후 즉시 반영)
    for r in state["results"]:
        if not r.get("has_data"):
            candles = load_candles(r["symbol"], "1d")
            count = len(candles)
            if count >= MIN_CANDLES:
                recent = candles[-20:]
                ranges = [(c["high"] - c["low"]) / c["open"] * 100 for c in recent if c["open"] > 0]
                avg_range = sum(ranges) / len(ranges) if ranges else 0
                volumes = [c["close"] * c["volume"] for c in recent if c["volume"]]
                avg_turnover = sum(volumes) / len(volumes) if volumes else 0
                turnover_b = avg_turnover / 1e8
                range_score = min(50, avg_range * 15)
                vol_score = min(50, math.log10(max(turnover_b, 1)) * 20) if turnover_b > 0 else 0
                r["has_data"] = True
                r["candle_count"] = count
                r["avg_range_pct"] = round(avg_range, 2)
                r["turnover_b"] = round(turnover_b)
                r["score"] = int(range_score + vol_score)
    return state


def run_scan(market) -> None:
    """Background scan — prices from API, metrics from DB candles."""
    with _lock:
        if _state["scanning"]:
            return
        _state["scanning"] = True
        _state["error"] = ""

    try:
        symbols = [s["symbol"] for s in UNIVERSE]
        prices = _fetch_prices(market, symbols) if market else {}

        # Load DB candles and build watchlist set
        watchlist_syms = _current_watchlist_symbols()

        results = []
        for stock in UNIVERSE:
            sym = stock["symbol"]
            name = stock["name"]
            price = prices.get(sym)

            candles = load_candles(sym, "1d")
            has_data = len(candles) >= MIN_CANDLES

            entry: dict = {
                "symbol": sym,
                "name": name,
                "price": price,
                "avg_range_pct": None,
                "turnover_b": None,
                "score": None,
                "candle_count": len(candles),
                "has_data": has_data,
                "in_watchlist": sym in watchlist_syms,
            }

            if has_data:
                recent = candles[-20:]
                ranges = [
                    (c["high"] - c["low"]) / c["open"] * 100
                    for c in recent if c["open"] > 0
                ]
                avg_range = sum(ranges) / len(ranges) if ranges else 0

                volumes = [c["close"] * c["volume"] for c in recent if c["volume"]]
                avg_turnover = sum(volumes) / len(volumes) if volumes else 0
                turnover_b = avg_turnover / 1e8

                range_score = min(50, avg_range * 15)
                vol_score = min(50, math.log10(max(turnover_b, 1)) * 20) if turnover_b > 0 else 0
                score = int(range_score + vol_score)

                entry["avg_range_pct"] = round(avg_range, 2)
                entry["turnover_b"] = round(turnover_b)
                entry["score"] = score

            results.append(entry)

        # Sort: has_data + score desc, then no-data by name
        results.sort(key=lambda x: (not x["has_data"], -(x["score"] or 0), x["name"]))

        with _lock:
            _state["results"] = results
            _state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _state["scanning"] = False

    except Exception as e:
        with _lock:
            _state["scanning"] = False
            _state["error"] = str(e)


def _fetch_prices(market, symbols: list[str]) -> dict[str, float]:
    """Batch price fetch (10 symbols per request)."""
    prices: dict[str, float] = {}
    for i in range(0, len(symbols), 10):
        batch = symbols[i:i + 10]
        try:
            raw = market.prices(batch)
            for p in raw.get("result", []):
                try:
                    prices[p["symbol"]] = float(p["lastPrice"])
                except (KeyError, ValueError):
                    pass
        except Exception:
            pass
    return prices


def _current_watchlist_symbols() -> set[str]:
    """config.yaml 의 현재 워치리스트 심볼 집합."""
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return {w["symbol"] for w in cfg.get("watchlist", [])}
    except Exception:
        return set()
