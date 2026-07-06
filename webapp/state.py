"""봇 런타임 상태 (화면·봇 공유)."""
import threading

_lock = threading.Lock()

_state = {
    "mode": "paper",        # paper / live
    "running": True,        # 봇 루프 동작 여부
    "paused": False,        # 일시정지
    "killed": False,        # 긴급 중단
    "api_ok": False,        # 토스 API 연결 상태
    "market_session": "-",  # 장 상태 표기
    "circuit_breaker": False,
}


def get() -> dict:
    with _lock:
        return dict(_state)


def set(**kw) -> dict:
    with _lock:
        _state.update(kw)
        return dict(_state)
