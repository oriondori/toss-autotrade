"""
매일 장 마감 후 실행 — config.yaml 감시 종목의 최근 캔들 수집.
cron: 5 7 * * 1-5  (KST 16:05, 평일)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from data import db
from data.collector import collect
from core.http import TossClient
from core.market import MarketApi


def main() -> None:
    db.init()
    market = MarketApi(TossClient())

    # 휴장일 확인 (API로 최신 달력 로드 후 오늘 날짜 체크)
    from engine.scheduler import load_holidays, is_holiday, today_str, now_kst
    load_holidays(market)
    today = today_str()
    if now_kst().weekday() >= 5:
        print(f"오늘은 주말 ({today}) — 수집 생략")
        return
    if is_holiday(today):
        print(f"오늘은 공휴일/휴장일 ({today}) — 수집 생략")
        return

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    symbols = [w["symbol"] for w in cfg.get("watchlist", [])]

    if not symbols:
        print("watchlist 비어있음 — 종료")
        return

    print(f"=== 일봉 수집 시작: {len(symbols)}개 종목 ({today}) ===")


    ok, fail = [], []
    for sym in symbols:
        try:
            n = collect(market, sym, interval="1d", days=7)
            db.log("INFO", "COLLECT", f"캔들 수집 완료: {sym} {n}봉")
            print(f"  OK  {sym}: {n}봉")
            ok.append(sym)
        except Exception as e:  # noqa: BLE001
            db.log("ERROR", "COLLECT", f"캔들 수집 실패: {sym} {e}")
            print(f"  FAIL {sym}: {e}")
            fail.append(sym)

    print(f"=== 완료: 성공 {len(ok)}개, 실패 {len(fail)}개 ===")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
