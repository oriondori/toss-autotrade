"""Paper trading engine — virtual fills and portfolio state.

State is persisted to paper_state.json so bot restarts don't lose positions.
All fills are also recorded to the orders table (mode='paper').
"""
import json
import math
import threading
from pathlib import Path

from data import db

_lock = threading.Lock()
_STATE_FILE = Path(__file__).parent.parent / "paper_state.json"

_FEE_SELL = 0.00015 + 0.0018   # brokerage + transaction tax on sell side
_FEE_BUY = 0.00015              # brokerage on buy side

_s: dict = {
    "cash": 0.0,
    "initial_cash": 0.0,
    "positions": {},        # symbol -> {qty, avg_price, name}
    "today_orders": 0,
    "today_realized": 0.0,
    "today_buy_amount": 0.0,  # total buy cost today (for daily limit check)
    "today_date": "",
}


def init(initial_cash: float) -> None:
    """Load persisted state or start fresh."""
    global _s
    saved = _load()
    if saved and saved.get("initial_cash", 0) > 0:
        with _lock:
            _s.update(saved)
        db.log("INFO", "PAPER", f"Paper state restored: cash={_s['cash']:,.0f} KRW, "
               f"positions={list(_s['positions'].keys())}")
    else:
        with _lock:
            _s["cash"] = initial_cash
            _s["initial_cash"] = initial_cash
        _persist()
        db.log("INFO", "PAPER", f"Paper trading started: {initial_cash:,.0f} KRW")


def _load() -> dict | None:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _persist() -> None:
    _STATE_FILE.write_text(
        json.dumps(_s, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def snapshot() -> dict:
    with _lock:
        return {
            "cash": _s["cash"],
            "initial_cash": _s["initial_cash"],
            "positions": {k: dict(v) for k, v in _s["positions"].items()},
            "today_realized": _s["today_realized"],
            "today_buy_amount": _s["today_buy_amount"],
            "today_date": _s["today_date"],
            "today_orders": _s["today_orders"],
        }


def today_orders() -> int:
    with _lock:
        return _s["today_orders"]


def reset_daily(today_date: str) -> None:
    with _lock:
        _s["today_orders"] = 0
        _s["today_realized"] = 0.0
        _s["today_buy_amount"] = 0.0
        _s["today_date"] = today_date
        _persist()


def buy(symbol: str, name: str, price: float, qty: int) -> bool:
    """Virtual buy. Returns False if insufficient cash."""
    cost = price * qty * (1 + _FEE_BUY)
    with _lock:
        if _s["cash"] < cost:
            return False
        _s["cash"] -= cost
        pos = _s["positions"]
        if symbol in pos:
            old = pos[symbol]
            new_qty = old["qty"] + qty
            old["avg_price"] = (old["avg_price"] * old["qty"] + price * qty) / new_qty
            old["qty"] = new_qty
        else:
            pos[symbol] = {"qty": qty, "avg_price": price, "name": name}
        _s["today_orders"] += 1
        _s["today_buy_amount"] += cost
        _persist()

    oid = db.add_order(symbol=symbol, name=name, side="BUY", order_type="MARKET",
                       price=price, qty=qty, status="FILLED", fill_price=price,
                       reason="Volatility breakout", mode="paper")
    db.log("INFO", "FILL",
           f"[PAPER] BUY {symbol}({name}) {qty}sh @{price:,.0f} cost={cost:,.0f} (#{oid})")
    from monitor import notifier
    notifier.buy(symbol, name, price, qty)
    return True


def sell(symbol: str, name: str, price: float, reason: str = "End of day") -> float | None:
    """Virtual sell. Returns None if no position, else realized PnL."""
    with _lock:
        if symbol not in _s["positions"]:
            return None
        pos = _s["positions"].pop(symbol)

    qty = pos["qty"]
    avg = pos["avg_price"]
    proceeds = price * qty * (1 - _FEE_SELL)
    realized = proceeds - avg * qty

    with _lock:
        _s["cash"] += proceeds
        _s["today_realized"] += realized
        _s["today_orders"] += 1
        _persist()

    oid = db.add_order(symbol=symbol, name=name, side="SELL", order_type="MARKET",
                       price=price, qty=qty, status="FILLED", fill_price=price,
                       reason=reason, mode="paper")
    db.log("INFO", "FILL",
           f"[PAPER] SELL {symbol}({name}) {qty}sh @{price:,.0f} "
           f"PnL={realized:+,.0f} (#{oid})")
    from monitor import notifier
    if "Stop loss" in reason:
        notifier.stop_loss(symbol, name, price, realized)
    else:
        notifier.sell(symbol, name, price, qty, realized)
    return realized


def total_asset(market_prices: dict[str, float] | None = None) -> float:
    """Current total asset = cash + market value of positions."""
    with _lock:
        positions = dict(_s["positions"])
        cash = _s["cash"]
    if not market_prices:
        return cash
    mv = sum(p["qty"] * market_prices.get(sym, p["avg_price"])
             for sym, p in positions.items())
    return cash + mv
