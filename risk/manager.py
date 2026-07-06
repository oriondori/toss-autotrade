"""Risk manager — pre-trade checks before paper (and future live) orders."""
import math

from engine import paper


class RiskManager:
    def __init__(self, cfg: dict) -> None:
        self.max_position_pct = cfg.get("max_position_pct", 0.10)
        self.daily_loss_cb = cfg.get("daily_loss_cb", -0.02)
        self.stop_loss = cfg.get("stop_loss", -0.03)
        self.max_daily_orders = cfg.get("max_daily_orders", 20)
        self.daily_buy_limit = cfg.get("daily_buy_limit", 1_000_000)

    def calc_qty(self, price: float, total_asset: float, snap: dict) -> int:
        """Shares to buy: position size 기준이되 일 한도 잔여액 초과 불가."""
        budget = min(
            total_asset * self.max_position_pct,
            self.daily_buy_limit - snap.get("today_buy_amount", 0),
        )
        return max(1, math.floor(budget / price))

    def can_buy(self, symbol: str, price: float, qty: int,
                snap: dict, order_count: int | None = None) -> tuple[bool, str]:
        """Returns (allowed, reason). reason is empty string when allowed.

        order_count: 오늘 주문 건수. 생략 시 페이퍼 건수 사용(페이퍼 호출용).
                     실주문 호출 시에는 실제 live 주문 건수를 넘겨야 함."""
        if order_count is None:
            order_count = paper.today_orders()
        if order_count >= self.max_daily_orders:
            return False, f"Daily order limit reached ({self.max_daily_orders})"

        cost = price * qty

        # 예수금 초과 방지 (미수 금지)
        if snap["cash"] < cost:
            return False, f"Insufficient cash ({cost:,.0f} > {snap['cash']:,.0f})"

        # 일 최대 매매금액 한도
        today_bought = snap.get("today_buy_amount", 0)
        if today_bought + cost > self.daily_buy_limit:
            remain = max(0, self.daily_buy_limit - today_bought)
            return False, f"Daily buy limit reached (limit {self.daily_buy_limit:,.0f}, remain {remain:,.0f})"

        # 일손실 서킷브레이커
        initial = snap["initial_cash"]
        if initial > 0 and snap["today_realized"] / initial < self.daily_loss_cb:
            pct = snap["today_realized"] / initial * 100
            return False, f"Daily loss circuit breaker ({pct:.1f}%)"

        return True, ""

    def check_stop_loss(self, symbol: str, current_price: float,
                        snap: dict) -> bool:
        """True if position is below stop-loss threshold."""
        pos = snap["positions"].get(symbol)
        if pos is None:
            return False
        return current_price / pos["avg_price"] - 1 < self.stop_loss
