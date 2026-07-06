"""Telegram notification helper.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to enable.
All functions are silent no-ops when credentials are missing or the request fails,
so bot operation is never interrupted by notification failures.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _send(text: str) -> None:
    if not (_TOKEN and _CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
            json={"chat_id": _CHAT, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


# ── 기존 알림 ────────────────────────────────────────────

def buy(symbol: str, name: str, price: float, qty: int) -> None:
    _send(
        f"🟢 <b>[매수]</b> {name} ({symbol})\n"
        f"체결가: {price:,.0f}원  수량: {qty}주\n"
        f"총 비용: {price * qty:,.0f}원"
    )


def sell(symbol: str, name: str, price: float, qty: int, pnl: float) -> None:
    icon = "📈" if pnl >= 0 else "📉"
    _send(
        f"{icon} <b>[매도]</b> {name} ({symbol})\n"
        f"체결가: {price:,.0f}원  수량: {qty}주\n"
        f"실현손익: <b>{pnl:+,.0f}원</b>"
    )


def stop_loss(symbol: str, name: str, price: float, pnl: float) -> None:
    _send(
        f"🔴 <b>[손절]</b> {name} ({symbol})\n"
        f"현재가: {price:,.0f}원  손익: {pnl:+,.0f}원"
    )


def circuit_breaker(daily_pnl: float, threshold_pct: float) -> None:
    _send(
        f"🚨 <b>[서킷브레이커 발동]</b>\n"
        f"일 손실 {daily_pnl:+,.0f}원 → 임계치 {threshold_pct*100:.0f}% 초과\n"
        f"오늘 신규 매수 차단됩니다."
    )


def daily_report(date: str, realized: float, total_asset: float,
                 wins: int = 0, losses: int = 0) -> None:
    icon = "📈" if realized >= 0 else "📉"
    _send(
        f"{icon} <b>[일일 리포트] {date}</b>\n"
        f"실현손익: <b>{realized:+,.0f}원</b>\n"
        f"총 자산: {total_asset:,.0f}원\n"
        f"승: {wins}  패: {losses}"
    )


def error(message: str) -> None:
    _send(f"⚠️ <b>[에러]</b>\n{message}")


# ── 신규 알림 ────────────────────────────────────────────

def market_open_briefing(targets: list[dict]) -> None:
    """09:00 장 시작 시 오늘 목표가 브리핑."""
    lines = ["📢 <b>장 시작 — 오늘 목표가</b>\n"]
    for t in targets:
        status = "✅ 도달" if t.get("reached") else "⏳ 대기"
        target = t.get("target")
        t_str = f"{target:,.0f}원" if target else "계산 중"
        lines.append(f"• {t['name']}  목표: {t_str}  {status}")
    _send("\n".join(lines))


def target_approaching(symbol: str, name: str, current: float,
                       target: float, progress: float) -> None:
    """목표가 90% 이상 진행 시 사전 경보."""
    _send(
        f"⚡ <b>[목표가 근접]</b> {name} ({symbol})\n"
        f"현재가: {current:,.0f}원\n"
        f"목표가: {target:,.0f}원  진행률: {progress:.0f}%\n"
        f"곧 매수 신호가 발생할 수 있습니다."
    )


def position_warning(symbol: str, name: str, current: float,
                     avg_price: float, drop_pct: float) -> None:
    """보유 포지션 -1.5% 하락 경보 (손절선 -3% 절반 지점)."""
    _send(
        f"⚠️ <b>[포지션 경보]</b> {name} ({symbol})\n"
        f"매수가: {avg_price:,.0f}원\n"
        f"현재가: {current:,.0f}원  ({drop_pct:+.1f}%)\n"
        f"손절선({-3:.0f}%)의 절반 — 모니터링 필요"
    )


def pre_close_alert(positions: list[dict]) -> None:
    """15:20 장 마감 10분 전 청산 예정 알림."""
    if not positions:
        _send("🔔 <b>[마감 10분 전]</b>\n보유 포지션 없음. 오늘 거래 종료.")
        return
    lines = ["🔔 <b>[마감 10분 전]</b> 15:30 자동 청산 예정\n"]
    for p in positions:
        pnl = (p.get("current", 0) - p.get("avg_price", 0)) * p.get("qty", 0)
        lines.append(
            f"• {p['name']}  {p.get('qty', 0)}주  "
            f"평가손익: {pnl:+,.0f}원"
        )
    _send("\n".join(lines))
