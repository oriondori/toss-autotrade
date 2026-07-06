"""종목 추천 엔진 — 현재 포트폴리오 진단 + 대안 종목 제안.

점수 체계 (0~100):
  가격 적합성  (0~25): 주당 가격이 max_position 내에서 5주 이상 살 수 있으면 만점
  변동성      (0~35): 최근 20일 평균 일중 변동폭 % → 변동성 돌파 전략 유리도
  유동성      (0~25): 평균 거래대금(억원) → 슬리피지 영향
  백테스트    (0~15): Sharpe 비례 (캔들 데이터 있을 때만)
"""
import math
import threading
import time
from datetime import datetime

from data.collector import load_candles
from data.backtest import run as bt_run
from scanner.universe import UNIVERSE
from data import db

_lock = threading.Lock()
_state: dict = {
    "running": False,
    "last_run": None,
    "results": [],      # 전체 유니버스 점수
    "diagnosis": [],    # 현재 watchlist 진단
    "replacements": {}, # symbol -> [후보 list]
    "error": "",
}

MIN_CANDLES = 60


def get_state() -> dict:
    with _lock:
        return dict(_state)


def run_analysis(market=None, config: dict | None = None) -> None:
    """백그라운드 분석 실행."""
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
        _state["error"] = ""

    try:
        cfg = config or _load_config()
        risk = cfg.get("risk", {})
        watchlist = cfg.get("watchlist", [])
        initial_cash = cfg.get("paper", {}).get("initial_cash", 10_000_000)
        max_pct = risk.get("max_position_pct", 0.20)
        max_position = initial_cash * max_pct

        # 현재 주가 조회
        prices = {}
        if market:
            all_syms = [s["symbol"] for s in UNIVERSE]
            try:
                for i in range(0, len(all_syms), 10):
                    batch = all_syms[i:i + 10]
                    raw = market.prices(batch)
                    for p in raw.get("result", []):
                        try:
                            prices[p["symbol"]] = float(p["lastPrice"])
                        except (KeyError, ValueError):
                            pass
                    time.sleep(0.3)
            except Exception as e:
                db.log("WARN", "RECOMMEND", f"가격 조회 부분 실패: {e}")

        # 감시 종목 현재가도 조회
        wl_syms = [w["symbol"] for w in watchlist]
        if market and wl_syms:
            try:
                raw = market.prices(wl_syms)
                for p in raw.get("result", []):
                    try:
                        prices[p["symbol"]] = float(p["lastPrice"])
                    except (KeyError, ValueError):
                        pass
            except Exception:
                pass

        watchlist_set = {w["symbol"] for w in watchlist}

        # 전체 유니버스 점수 계산
        scored = []
        for stock in UNIVERSE:
            sym = stock["symbol"]
            name = stock["name"]
            price = prices.get(sym)
            candles = load_candles(sym, "1d")
            has_data = len(candles) >= MIN_CANDLES

            entry = {
                "symbol": sym,
                "name": name,
                "price": price,
                "has_data": has_data,
                "candle_count": len(candles),
                "in_watchlist": sym in watchlist_set,
                "price_ok": price is not None and price <= max_position,
                "max_qty": int(max_position / price) if price and price > 0 else 0,
                "score": 0,
                "price_score": 0,
                "vol_score": 0,
                "liq_score": 0,
                "bt_score": 0,
                "avg_range_pct": None,
                "turnover_b": None,
                "best_k": None,
                "sharpe": None,
                "mdd_pct": None,
                "win_rate": None,
                "trades": None,
                "issue": None,
            }

            # 가격 적합성 (0~25)
            if price and price > 0:
                qty = int(max_position / price)
                if qty >= 10:
                    entry["price_score"] = 25
                elif qty >= 5:
                    entry["price_score"] = 20
                elif qty >= 3:
                    entry["price_score"] = 12
                elif qty >= 1:
                    entry["price_score"] = 5
                else:
                    entry["price_score"] = 0
                    entry["issue"] = f"주당 {price:,.0f}원 — 포지션 한도({max_position:,.0f}원) 초과"

            if has_data:
                recent = candles[-20:]

                # 변동성 (0~35)
                ranges = [(c["high"] - c["low"]) / c["open"] * 100
                          for c in recent if c["open"] > 0]
                avg_range = sum(ranges) / len(ranges) if ranges else 0
                entry["avg_range_pct"] = round(avg_range, 2)
                entry["vol_score"] = min(35, avg_range * 10)

                # 유동성 (0~25)
                vols = [c["close"] * c["volume"] for c in recent if c["volume"]]
                avg_turn = sum(vols) / len(vols) if vols else 0
                turn_b = avg_turn / 1e8
                entry["turnover_b"] = round(turn_b)
                entry["liq_score"] = min(25, math.log10(max(turn_b, 1)) * 10) if turn_b > 0 else 0

                # 백테스트 Sharpe → 점수 (0~15)
                best_k, best_sharpe = _best_k(candles)
                if best_k is not None:
                    from strategy.volatility_breakout import VolatilityBreakout
                    m = bt_run(candles, VolatilityBreakout(k=best_k))
                    entry["best_k"] = best_k
                    entry["sharpe"] = round(m["sharpe"], 3)
                    entry["mdd_pct"] = round(m["mdd"] * 100, 1)
                    entry["win_rate"] = round(m["win_rate"] * 100, 1)
                    entry["trades"] = m["trades"]
                    sharpe_norm = max(0, min(1, (m["sharpe"] + 0.5) / 1.5))
                    entry["bt_score"] = sharpe_norm * 15

            entry["score"] = int(
                entry["price_score"] + entry["vol_score"]
                + entry["liq_score"] + entry["bt_score"]
            )
            scored.append(entry)

        scored.sort(key=lambda x: (-x["score"], x["name"]))

        # 현재 감시 종목 진단
        diagnosis = []
        for w in watchlist:
            sym = w["symbol"]
            rec = next((s for s in scored if s["symbol"] == sym), None)
            if rec is None:
                continue
            issues = []
            if rec["issue"]:
                issues.append(rec["issue"])
            if rec["sharpe"] is not None and rec["sharpe"] < 0:
                issues.append(f"백테스트 Sharpe {rec['sharpe']} (음수)")
            if rec["mdd_pct"] is not None and rec["mdd_pct"] < -40:
                issues.append(f"MDD {rec['mdd_pct']}% (위험)")
            diagnosis.append({
                **rec,
                "k": w.get("k"),
                "issues": issues,
                "needs_replace": len(issues) > 0,
            })

        # 문제 종목별 대안 추천 (상위 3개, 같은 유니버스 중 price_ok + 최고 점수)
        replacements = {}
        for d in diagnosis:
            if not d["needs_replace"]:
                continue
            sym = d["symbol"]
            candidates = [
                s for s in scored
                if s["symbol"] != sym
                and s["symbol"] not in watchlist_set
                and s["price_ok"]
                and s["has_data"]
                and (s["sharpe"] is None or s["sharpe"] >= 0)
            ][:3]
            replacements[sym] = candidates

        with _lock:
            _state["results"] = scored
            _state["diagnosis"] = diagnosis
            _state["replacements"] = replacements
            _state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _state["running"] = False

        db.log("INFO", "RECOMMEND", f"분석 완료: {len(scored)}종목 평가, "
               f"{len([d for d in diagnosis if d['needs_replace']])}개 교체 권장")

    except Exception as e:
        with _lock:
            _state["running"] = False
            _state["error"] = str(e)
        db.log("ERROR", "RECOMMEND", f"분석 실패: {e}")


def _best_k(candles: list[dict]) -> tuple[float | None, float]:
    """K=0.1~0.9 중 Sharpe 최고값 반환."""
    from strategy.volatility_breakout import VolatilityBreakout
    best_k, best_s = None, -999.0
    for k10 in range(1, 10):
        k = k10 / 10
        try:
            m = bt_run(candles, VolatilityBreakout(k=k))
            if m["sharpe"] > best_s:
                best_s, best_k = m["sharpe"], k
        except Exception:
            pass
    return best_k, best_s


def _load_config() -> dict:
    import yaml
    from pathlib import Path
    cfg_path = Path(__file__).parent.parent / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
