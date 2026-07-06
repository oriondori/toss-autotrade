"""Strategy evaluation -> risk check -> paper/live order pipeline.

Called every 30-second poll tick from main.bot_loop().
Implements volatility breakout: buy when price crosses (today_open + K * prev_range),
sell at 15:14 (before 동시호가 15:20–15:30). EOD sell attempted once per symbol per day.

Live mode: config.yaml live.enabled=true + live.symbols 에 있는 종목만 실주문.
           나머지 종목은 live 모드에서도 계속 페이퍼.
"""
from datetime import datetime, time as dtime

from data import db
from data.collector import load_candles
from engine import paper
from engine.scheduler import session, today_str
from risk.manager import RiskManager
from webapp import state

_daily: dict[str, dict] = {}   # symbol -> {date, open_price, target, signaled, approach_alerted, mdd_warned, eod_sell_sent}
_EOD_SELL_CUTOFF = dtime(15, 29)  # 이 시각 이후 시장가 EOD 매도 차단 (거래소 15:30 완전 종료)
_last_date: str = ""
_briefing_sent: str = ""
_pre_close_sent: str = ""

# live 모드 주문 클라이언트 (main.py가 주입)
_order_client = None


def set_order_client(client) -> None:
    global _order_client
    _order_client = client


def run_tick(watchlist: list[dict], market, risk: RiskManager,
             live_cfg: dict | None = None) -> None:
    """Called on every bot loop tick. Evaluates signals and fires paper/live orders."""
    global _last_date, _briefing_sent, _pre_close_sent

    s = state.get()
    if s["killed"] or s["paused"]:
        return

    live_cfg = live_cfg or {}
    live_enabled = live_cfg.get("enabled", False)
    live_symbols: set[str] = set(live_cfg.get("symbols", []))

    sess = session()
    today = today_str()
    state.set(market_session=sess)

    # Day rollover: flush yesterday's PnL, reset daily counters
    if _last_date and today != _last_date:
        _flush_daily(_last_date)
    _last_date = today

    if sess not in ("OPEN", "CLOSE"):
        if sess == "HOLIDAY":
            db.log("INFO", "LOOP", f"Holiday — skip trading ({today})")
        return

    # Fetch current prices for all watchlist symbols
    symbols = [w["symbol"] for w in watchlist]
    try:
        raw = market.prices(symbols)
        prices: dict[str, float] = {
            p["symbol"]: float(p["lastPrice"])
            for p in raw.get("result", [])
        }
    except Exception as e:
        db.log("ERROR", "LOOP", f"Price fetch failed: {e}")
        return

    snap = paper.snapshot()

    # ── 09:00 장 시작 브리핑 (OPEN 세션 최초 틱) ──
    if sess == "OPEN" and _briefing_sent != today:
        _send_briefing(watchlist, prices)
        _briefing_sent = today

    # ── 15:20 마감 전 포지션 알림 ──
    now = datetime.now()
    if (sess == "OPEN" and _pre_close_sent != today
            and now.hour == 15 and now.minute >= 20):
        _send_pre_close(snap, prices)
        _pre_close_sent = today

    for w in watchlist:
        sym = w["symbol"]
        name = w.get("name", sym)
        k = float(w.get("k", 0.5))
        price = prices.get(sym)
        if not price:
            continue

        # --- CLOSE session: end-of-day liquidation ---
        if sess == "CLOSE":
            is_live = live_enabled and sym in live_symbols
            d = _daily.get(sym, {})
            if is_live:
                # 이미 EOD 매도 시도했거나 15:29 지났으면 재시도 안 함
                if d.get("eod_sell_sent"):
                    continue
                now_t = datetime.now().time()
                if now_t > _EOD_SELL_CUTOFF:
                    db.log("WARN", "ORDER",
                           f"[LIVE] {sym} EOD 매도 시각 초과 ({now_t.strftime('%H:%M')}) — 내일 확인 필요")
                    d["eod_sell_sent"] = True  # 반복 경고 방지
                    continue
                if _has_live_position_today(sym):
                    if _order_client:
                        d["eod_sell_sent"] = True  # 시도 전 플래그 세팅 (재시도 방지)
                        result = _order_client.sell(sym, name, qty=_live_qty_held(sym, market), price=price, reason="End of day")
                        if result["ok"]:
                            db.add_signal(symbol=sym, name=name, side="SELL",
                                          strategy="VB", reason=f"EOD(live) @{price:,.0f}")
                        else:
                            db.log("ERROR", "ORDER",
                                   f"[LIVE] {sym} EOD 매도 실패 (재시도 안 함): {result['reason']}")
            else:
                if sym in snap["positions"] and not d.get("eod_sell_sent"):
                    d["eod_sell_sent"] = True
                    realized = paper.sell(sym, name, price, reason="End of day")
                    if realized is not None:
                        db.add_signal(symbol=sym, name=name, side="SELL",
                                      strategy="VB", reason=f"EOD @{price:,.0f}")
            continue

        # --- OPEN session: signal evaluation ---
        snap = paper.snapshot()  # refresh after possible sells

        # Initialize daily breakout target on first open tick of the day
        if sym not in _daily or _daily[sym]["date"] != today:
            open_price = _get_open_price(sym, market) or price
            candles = load_candles(sym, "1d")
            target = None
            if candles:
                # 오늘 미완성 캔들 제외 후 전일 기준으로 계산
                historical = [c for c in candles if c["ts"][:10] < today]
                if historical:
                    prev = historical[-1]
                    prev_range = prev["high"] - prev["low"]
                    target = open_price + k * prev_range
            _daily[sym] = {
                "date": today, "open_price": open_price,
                "target": target, "signaled": False,
                "approach_alerted": False, "mdd_warned": False,
                "eod_sell_sent": False,
            }
            t_str = f"{target:,.0f}" if target else "N/A (no prev candle)"
            tag = "LIVE" if (live_enabled and sym in live_symbols) else "PAPER"
            db.log("INFO", "SIGNAL",
                   f"[{tag}] {sym} day open≈{open_price:,.0f}  "
                   f"K={k}  target={t_str}")

        d = _daily[sym]

        # ── 목표가 90% 근접 경보 ──
        if (not d["approach_alerted"] and not d["signaled"]
                and sym not in snap["positions"]
                and d["target"] and d["open_price"]
                and d["target"] > d["open_price"]):
            progress = (price - d["open_price"]) / (d["target"] - d["open_price"]) * 100
            if 90 <= progress < 100:
                from monitor import notifier
                notifier.target_approaching(sym, name, price, d["target"], progress)
                d["approach_alerted"] = True

        # ── 보유 포지션 MDD -1.5% 경보 ──
        if sym in snap["positions"] and not d["mdd_warned"]:
            pos = snap["positions"][sym]
            avg = pos.get("avg_price", 0)
            if avg > 0:
                drop_pct = (price - avg) / avg * 100
                if drop_pct <= -1.5:
                    from monitor import notifier
                    notifier.position_warning(sym, name, price, avg, drop_pct)
                    d["mdd_warned"] = True

        # Stop-loss check
        is_live_sl = live_enabled and sym in live_symbols
        if is_live_sl and _has_live_position_today(sym):
            if risk.check_stop_loss(sym, price, snap):
                if _order_client:
                    qty_held = _live_qty_held(sym, market)
                    result = _order_client.sell(sym, name, qty=qty_held, price=price, reason="Stop loss")
                    if result["ok"]:
                        db.log("WARN", "RISK", f"[LIVE] Stop loss {sym} @{price:,.0f}")
                        d["signaled"] = False
                        d["mdd_warned"] = False
                continue
        elif sym in snap["positions"] and risk.check_stop_loss(sym, price, snap):
            realized = paper.sell(sym, name, price, reason="Stop loss")
            if realized is not None:
                db.log("WARN", "RISK",
                       f"[PAPER] Stop loss {sym} @{price:,.0f} PnL={realized:+,.0f}")
                d["signaled"] = False
                d["mdd_warned"] = False
            continue

        # Buy signal: breakout and not yet entered today
        is_live = live_enabled and sym in live_symbols
        already_in = sym in snap["positions"] if not is_live else _has_live_position_today(sym)

        if (not d["signaled"]
                and not already_in
                and d["target"] is not None
                and price >= d["target"]):

            if is_live and _order_client:
                # ── 실주문 분기: 실자산 기준 포지션 사이징, 실제 예수금이 최종 한도 ──
                asset = _real_total_asset()
                cash = _real_cash()
                live_stats = _live_daily_stats()
                qty = risk.calc_qty(price, asset, live_stats)
                live_snap = {**live_stats, "cash": cash, "initial_cash": asset, "today_realized": 0}
                ok, reason = risk.can_buy(sym, price, qty, live_snap,
                                          order_count=live_stats["order_count"])
                if not ok:
                    db.log("WARN", "RISK", f"[LIVE] {sym} buy rejected: {reason}")
                    continue
                result = _order_client.buy(sym, name, price, qty)
                if result["ok"]:
                    d["signaled"] = True
                    d["approach_alerted"] = True
                    db.add_signal(symbol=sym, name=name, side="BUY",
                                  strategy="VB",
                                  reason=f"Breakout(live) @{price:,.0f} target={d['target']:,.0f}")
                    from monitor import notifier
                    notifier.buy(sym, name, price, qty)
                else:
                    db.log("WARN", "ORDER", f"[LIVE] {sym} 매수 실패: {result['reason']}")
            else:
                # ── 페이퍼 분기 ──
                asset = paper.total_asset(prices)
                qty = risk.calc_qty(price, asset, snap)
                ok, reason = risk.can_buy(sym, price, qty, snap)
                if ok:
                    if paper.buy(sym, name, price, qty):
                        d["signaled"] = True
                        d["approach_alerted"] = True
                        db.add_signal(symbol=sym, name=name, side="BUY",
                                      strategy="VB",
                                      reason=f"Breakout @{price:,.0f} target={d['target']:,.0f}")
                else:
                    db.log("WARN", "RISK", f"[PAPER] {sym} buy rejected: {reason}")


def _has_live_position_today(symbol: str) -> bool:
    """오늘 live BUY 주문이 FILLED 됐는지 orders 테이블로 확인."""
    today = today_str()
    orders = db.recent("orders", 50)
    return any(
        o["symbol"] == symbol and o["side"] == "BUY"
        and o.get("mode") == "live" and o.get("status") in ("FILLED", "PENDING")
        and o["ts"].startswith(today)
        for o in orders
    )


def _live_qty_held(symbol: str, market) -> int:
    """실계좌 보유 수량 조회 (매도 시 사용)."""
    from webapp.server import runtime
    acct = runtime.get("account")
    if not acct:
        return 1
    try:
        data = acct.holdings()
        result = data.get("result", data) if isinstance(data, dict) else {}
        items = result.get("items", []) if isinstance(result, dict) else []
        for item in items:
            if item.get("symbol") == symbol:
                return int(item.get("quantity", 1))
    except Exception:
        pass
    return 1


def _real_total_asset() -> float:
    """실계좌 총자산(원화 보유종목 평가금액 + 예수금). 포지션 사이징(%) 기준.
    webapp.server의 60초 캐시를 재사용 — ACCOUNT API는 1 TPS라 종목별 직접 호출 금지."""
    from webapp.server import _get_acct
    c = _get_acct()
    return (c.get("market_value") or 0.0) + (c.get("cash_krw") or 0.0)


def _real_cash() -> float:
    """실계좌 예수금(주문가능금액). 최종 매수 가능 여부 판단 기준."""
    from webapp.server import _get_acct
    c = _get_acct()
    return c.get("cash_krw") or 0.0


def _live_daily_stats() -> dict:
    """오늘자 live 주문 기준 매수금액·건수 집계 (risk 체크용 — 페이퍼 카운터와 분리)."""
    today = today_str()
    orders_today = [o for o in db.recent("orders", 300)
                    if o.get("mode") == "live" and o["ts"].startswith(today)]
    buy_amount = sum((o.get("price") or 0) * (o.get("qty") or 0)
                     for o in orders_today if o.get("side") == "BUY")
    return {"today_buy_amount": buy_amount, "order_count": len(orders_today)}


def _send_briefing(watchlist: list[dict], prices: dict[str, float]) -> None:
    from monitor import notifier
    targets = []
    for w in watchlist:
        sym = w["symbol"]
        d = _daily.get(sym, {})
        current = prices.get(sym)
        reached = bool(d.get("target") and current and current >= d["target"])
        targets.append({
            "name": w.get("name", sym),
            "symbol": sym,
            "target": d.get("target"),
            "reached": reached,
        })
    notifier.market_open_briefing(targets)


def _send_pre_close(snap: dict, prices: dict[str, float]) -> None:
    from monitor import notifier
    positions = [
        {
            "name": pos.get("name", sym),
            "qty": pos.get("qty", 0),
            "avg_price": pos.get("avg_price", 0),
            "current": prices.get(sym, pos.get("avg_price", 0)),
        }
        for sym, pos in snap.get("positions", {}).items()
    ]
    notifier.pre_close_alert(positions)


def _get_open_price(sym: str, market) -> float | None:
    """Fetch today's opening price from the API (today's 1d candle open)."""
    try:
        resp = market.candles(sym, interval="1d", count=1)
        candles = resp.get("result", {}).get("candles", [])
        if candles:
            val = float(candles[0].get("openPrice", 0))
            return val if val > 0 else None
    except Exception:
        pass
    return None


def _flush_daily(date: str) -> None:
    """Record daily PnL to DB and reset intraday counters."""
    from monitor import notifier
    snap = paper.snapshot()
    realized = snap["today_realized"]
    asset = paper.total_asset()

    # 당일 매도 주문에서 수익/손실 건수 집계
    orders = db.recent("orders", 200)
    wins   = sum(1 for o in orders
                 if o["ts"].startswith(date) and o.get("side") == "SELL"
                 and o.get("status") == "FILLED")
    losses = 0   # paper.py 기록 방식상 단순 건수만 집계 (손익은 realized로 판단)

    db.upsert_daily_pnl(date, total_asset=asset, realized_pnl=realized)
    paper.reset_daily(today_str())
    db.log("INFO", "SYS",
           f"[PAPER] Day close {date}: realized={realized:+,.0f}  total_asset={asset:,.0f}")
    notifier.daily_report(date, realized, asset, wins=wins, losses=losses)
