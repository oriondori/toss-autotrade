"""TossAutoTrade entry point — bot loop + web dashboard.

Run: python main.py  ->  http://localhost:8000
Requires .env with TOSS_CLIENT_ID / TOSS_CLIENT_SECRET.
"""
import threading
import time
from pathlib import Path

import uvicorn
import yaml

from data import db
from webapp import state
from webapp.server import app, runtime

_CFG_FILE = Path(__file__).parent / "config.yaml"
POLL_SEC = 30


def load_config() -> dict:
    if _CFG_FILE.exists():
        with open(_CFG_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def connect_api() -> bool:
    """Connect to Toss Securities API. Returns False if credentials missing."""
    try:
        from core.account import AccountApi, first_account_seq
        from core.http import TossClient
        from core.market import MarketApi

        client = TossClient()
        client.tm.get_token()
        market = MarketApi(client)
        account = AccountApi(client)

        try:
            accts = account.accounts()
            db.log("INFO", "AUTH", f"Accounts received: {str(accts)[:300]}")
            client.account_seq = first_account_seq(accts)
            db.log("INFO", "AUTH", f"Account selected: account_seq={client.account_seq}")
            try:
                h = account.holdings()
                db.log("INFO", "AUTH", f"Holdings response: {str(h)[:500]}")
            except Exception as e:  # noqa: BLE001
                db.log("WARN", "AUTH", f"Holdings fetch failed: {e}")
        except Exception as e:  # noqa: BLE001
            db.log("WARN", "AUTH", f"Account auto-select failed (market data still usable): {e}")

        runtime["market"] = market
        runtime["account"] = account
        state.set(api_ok=True)
        db.log("INFO", "AUTH", "Toss API connected")

        # 공휴일 캘린더 로드
        from engine.scheduler import load_holidays
        load_holidays(market)

        # 실주문 클라이언트 주입
        from core.order import OrderClient
        from engine.executor import set_order_client
        set_order_client(OrderClient(client))
        return True
    except Exception as e:  # noqa: BLE001
        state.set(api_ok=False)
        db.log("WARN", "AUTH", f"API not connected — dashboard only: {e}")
        return False


def bot_loop(initial_cfg: dict) -> None:
    """Main bot loop — strategy evaluation + price polling every 30s."""
    from engine import paper
    from engine.executor import run_tick
    from risk.manager import RiskManager

    paper_cfg = initial_cfg.get("paper", {})
    paper.init(paper_cfg.get("initial_cash", 10_000_000))

    db.log("INFO", "SYS", "Bot loop started — mode=paper")

    while True:
        s = state.get()
        if s["killed"]:
            db.log("ERROR", "SYS", "Kill switch active — bot loop stopped")
            break

        if not s["paused"]:
            # Reload config every tick — picks up watchlist changes without restart
            cfg = load_config()
            watchlist = cfg.get("watchlist", [])
            risk = RiskManager(cfg.get("risk", {}))

            market = runtime.get("market")
            if market is not None:
                try:
                    live_cfg = cfg.get("live", {})
                    run_tick(watchlist, market, risk, live_cfg=live_cfg)
                except Exception as e:  # noqa: BLE001
                    db.log("ERROR", "LOOP", f"Executor error: {e}")
            else:
                db.log("INFO", "LOOP", "Heartbeat (API not connected)")

        time.sleep(POLL_SEC)


def main() -> None:
    db.init()
    db.log("INFO", "SYS", "=== TossAutoTrade starting ===")

    cfg = load_config()
    connect_api()

    t = threading.Thread(target=bot_loop, args=(cfg,), daemon=True)
    t.start()

    print("Dashboard: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
