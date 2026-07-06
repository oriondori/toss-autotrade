"""Market session detection (KST, UTC+9) + Korean holiday support."""
from datetime import datetime, timezone, timedelta, time as dtime

_KST = timezone(timedelta(hours=9))
_MARKET_OPEN  = dtime(9, 0)
_MARKET_CLOSE = dtime(15, 14)  # 동시호가(15:20~15:30) 전에 시장가 매도

_holidays: set[str] = set()   # "YYYY-MM-DD" 형식, 시작 시 load_holidays()로 채움


def load_holidays(market) -> None:
    """Toss market-calendar API에서 당해 + 내년 휴장일 캐싱."""
    global _holidays
    loaded: set[str] = set()
    now = now_kst()
    for year in (now.year, now.year + 1):
        try:
            r = market.market_calendar("KR")
            result = r.get("result", r) if isinstance(r, dict) else {}
            # API 응답: [{date, type}] 또는 {holidays:[{date}]}
            items = []
            if isinstance(result, list):
                items = result
            elif isinstance(result, dict):
                items = result.get("holidays", result.get("items", []))
            for item in items:
                d = item.get("date", "")
                if d:
                    loaded.add(d[:10])
        except Exception:
            pass
    if loaded:
        _holidays = loaded
        from data import db
        db.log("INFO", "SYS", f"Holiday calendar loaded: {len(_holidays)}일")


def is_holiday(date_str: str) -> bool:
    return date_str in _holidays


def now_kst() -> datetime:
    return datetime.now(_KST)


def session() -> str:
    """Current market session: PRE | OPEN | CLOSE | WEEKEND | HOLIDAY"""
    now = now_kst()
    if now.weekday() >= 5:
        return "WEEKEND"
    today = now.strftime("%Y-%m-%d")
    if is_holiday(today):
        return "HOLIDAY"
    t = now.time()
    if t < _MARKET_OPEN:
        return "PRE"
    if t >= _MARKET_CLOSE:
        return "CLOSE"
    return "OPEN"


def today_str() -> str:
    return now_kst().strftime("%Y-%m-%d")
