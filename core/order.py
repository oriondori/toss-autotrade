"""실주문 모듈 — 매수/매도 3단 사전검증 후 POST /api/v1/orders.

사전검증 순서:
  1. buying-power  — 예수금 >= 주문금액
  2. warnings      — 투자경고·VI·정리매매 여부
  3. commissions   — 수수료 확인 (로깅용)

안전장치:
  - mode='live' 일 때만 실제 API 호출 (paper는 engine/paper.py 경유)
  - clientOrderId UUID 멱등키 (중복 주문 방지)
  - 미체결 주문 5분 자동취소
  - 1억 이상 주문 시 confirmHighValueOrder=true
"""
import uuid
import time
from typing import Any

from data import db

# 실주문은 지정가만 사용 (설계가이드 5단계 규정 — 시장가는 Toss API가 order-type-not-allowed로 거부)
_FILL_BUFFER = 0.01  # 즉시 체결 유도용 버퍼: 매수 +1% / 매도 -1%


def _round_tick(price: float) -> int:
    """KRX 호가단위로 반올림 (지정가 주문은 호가단위에 맞아야 함)."""
    if price < 2_000:
        tick = 1
    elif price < 5_000:
        tick = 5
    elif price < 20_000:
        tick = 10
    elif price < 50_000:
        tick = 50
    elif price < 200_000:
        tick = 100
    elif price < 500_000:
        tick = 500
    else:
        tick = 1000
    return int(round(price / tick) * tick)


class OrderClient:
    def __init__(self, client) -> None:
        self.c = client

    # ── 사전검증 ─────────────────────────────────────────────

    def check_buying_power(self, cost_krw: float) -> tuple[bool, float]:
        """예수금 확인. (ok, available) 반환."""
        try:
            r = self.c.get("/api/v1/buying-power", "ORDER",
                           {"currency": "KRW"}, need_account=True)
            available = float((r.get("result") or {}).get("cashBuyingPower", 0))
            return available >= cost_krw, available
        except Exception as e:
            db.log("WARN", "ORDER", f"buying-power 조회 실패: {e}")
            return False, 0.0

    def check_warnings(self, symbol: str) -> tuple[bool, str]:
        """투자 경고 확인. (safe, reason) 반환."""
        try:
            from core.market import MarketApi
            r = self.c.get(f"/api/v1/stocks/{symbol}/warnings", "STOCK")
            result = r.get("result", {}) if isinstance(r, dict) else {}
            flags = []
            if result.get("investmentRisk"):     flags.append("투자위험")
            if result.get("investmentCaution"):  flags.append("투자주의")
            if result.get("vi"):                 flags.append("VI발동")
            if result.get("liquidation"):        flags.append("정리매매")
            if result.get("overheated"):         flags.append("과열종목")
            if flags:
                return False, "/".join(flags)
            return True, ""
        except Exception as e:
            db.log("WARN", "ORDER", f"warnings 조회 실패 ({symbol}): {e}")
            return True, ""   # 조회 실패 시 통과 (과도한 차단 방지)

    def get_commission(self, symbol: str, price: float, qty: int) -> float:
        """예상 수수료 조회 (로깅용, 실패해도 주문 진행)."""
        try:
            r = self.c.get("/api/v1/commissions", "ORDER",
                           {"symbol": symbol, "price": str(int(price)),
                            "quantity": str(qty)}, need_account=True)
            result = r.get("result", {}) if isinstance(r, dict) else {}
            return float(result.get("commission", 0))
        except Exception:
            return 0.0

    # ── 실주문 ───────────────────────────────────────────────

    def buy(self, symbol: str, name: str, price: float, qty: int) -> dict:
        """실 매수 주문. 지정가(현재가+1%, 즉시체결 유도) 3단 검증 후 POST. 반환: {ok, order_id, reason}"""
        limit_price = _round_tick(price * (1 + _FILL_BUFFER))
        cost = limit_price * qty

        # 1. 예수금
        ok, available = self.check_buying_power(cost)
        if not ok:
            reason = f"예수금 부족 (필요 {cost:,.0f}원, 가용 {available:,.0f}원)"
            db.log("WARN", "ORDER", f"[LIVE] {symbol} 매수 차단: {reason}")
            return {"ok": False, "reason": reason}

        # 2. 투자경고
        safe, warn_reason = self.check_warnings(symbol)
        if not safe:
            reason = f"투자경고 [{warn_reason}]"
            db.log("WARN", "ORDER", f"[LIVE] {symbol} 매수 차단: {reason}")
            return {"ok": False, "reason": reason}

        # 3. 수수료 로깅
        commission = self.get_commission(symbol, limit_price, qty)
        if commission:
            db.log("INFO", "ORDER",
                   f"[LIVE] {symbol} 예상수수료: {commission:,.0f}원")

        # 주문 실행
        client_order_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": "BUY",
            "orderType": "LIMIT",
            "price": str(limit_price),
            "quantity": str(qty),
            "clientOrderId": client_order_id,
        }
        if cost >= 100_000_000:
            body["confirmHighValueOrder"] = True

        try:
            r = self.c.post("/api/v1/orders", "ORDER", body, need_account=True)
            result = r.get("result", {}) if isinstance(r, dict) else {}
            order_id = result.get("orderId", "")
            db.log("INFO", "ORDER",
                   f"[LIVE] BUY {symbol}({name}) {qty}주 지정가@{limit_price:,.0f} "
                   f"orderId={order_id} clientId={client_order_id}")
            oid = db.add_order(symbol=symbol, name=name, side="BUY",
                               order_type="LIMIT", price=limit_price, qty=qty,
                               status="PENDING", client_order_id=client_order_id,
                               reason="Volatility breakout", mode="live")
            # 5분 후 미체결 자동취소 스케줄 (별도 스레드)
            self._schedule_cancel(order_id, client_order_id, oid, symbol, name)
            return {"ok": True, "order_id": order_id, "db_id": oid}
        except Exception as e:
            reason = str(e)
            db.log("ERROR", "ORDER", f"[LIVE] {symbol} 주문 실패: {reason}")
            return {"ok": False, "reason": reason}

    def sell(self, symbol: str, name: str, qty: int, price: float,
             reason: str = "End of day") -> dict:
        """실 매도 주문. 지정가(현재가-1%, 즉시체결 유도). 반환: {ok, order_id, reason}"""
        limit_price = _round_tick(price * (1 - _FILL_BUFFER))
        client_order_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": "SELL",
            "orderType": "LIMIT",
            "price": str(limit_price),
            "quantity": str(qty),
            "clientOrderId": client_order_id,
        }
        try:
            r = self.c.post("/api/v1/orders", "ORDER", body, need_account=True)
            result = r.get("result", {}) if isinstance(r, dict) else {}
            order_id = result.get("orderId", "")
            db.log("INFO", "ORDER",
                   f"[LIVE] SELL {symbol}({name}) {qty}주 지정가@{limit_price:,.0f} reason={reason} "
                   f"orderId={order_id}")
            oid = db.add_order(symbol=symbol, name=name, side="SELL",
                               order_type="LIMIT", price=limit_price, qty=qty,
                               status="PENDING", client_order_id=client_order_id,
                               reason=reason, mode="live")
            self._schedule_cancel(order_id, client_order_id, oid, symbol, name)
            return {"ok": True, "order_id": order_id, "db_id": oid}
        except Exception as e:
            reason_str = str(e)
            db.log("ERROR", "ORDER", f"[LIVE] {symbol} 매도 실패: {reason_str}")
            return {"ok": False, "reason": reason_str}

    def cancel(self, order_id: str) -> bool:
        """주문 취소. 성공 시 True."""
        try:
            self.c.post(f"/api/v1/orders/{order_id}/cancel", "ORDER",
                        json={}, need_account=True)
            db.log("INFO", "ORDER", f"[LIVE] 주문취소: {order_id}")
            return True
        except Exception as e:
            db.log("WARN", "ORDER", f"[LIVE] 취소 실패 {order_id}: {e}")
            return False

    def _schedule_cancel(self, order_id: str, client_order_id: str,
                         db_id: int, symbol: str, name: str,
                         wait_sec: int = 300) -> None:
        """5분 후 미체결 주문 자동취소 (별도 데몬 스레드)."""
        import threading

        def _cancel_if_pending():
            time.sleep(wait_sec)
            try:
                r = self.c.get(f"/api/v1/orders/{order_id}", "ORDER",
                               need_account=True)
                status = (r.get("result") or {}).get("status", "")
                if status in ("PENDING", "PARTIALLY_FILLED", ""):
                    self.cancel(order_id)
                    db.log("WARN", "ORDER",
                           f"[LIVE] {symbol} 미체결 자동취소 (5분 경과)")
            except Exception:
                pass

        if order_id:
            t = threading.Thread(target=_cancel_if_pending, daemon=True)
            t.start()
