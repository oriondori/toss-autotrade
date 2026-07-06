"""FastAPI 대시보드 서버 — 봇 상태 조회·제어 API + 화면 서빙."""
import hashlib
import secrets
import time
import yaml
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from data import db
from webapp import state

app = FastAPI(title="TossAutoTrade Dashboard")
STATIC = Path(__file__).parent / "static"

# 봇이 주입하는 실시간 컨텍스트 (main.py 에서 설정)
runtime: dict = {"account": None, "market": None}

# ── 인증 세션 ──────────────────────────────────────────────
_CFG_PATH = Path(__file__).parent.parent / "config.yaml"
_sessions: dict[str, dict] = {}   # token → {"exp": 만료 epoch, "role": "admin"|"viewer"}


def _load_dashboard_cfg() -> dict:
    try:
        return (yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}).get("dashboard", {})
    except Exception:
        return {}


def _check_password(pw: str) -> str | None:
    """비밀번호 확인 → 역할("admin"/"viewer") 반환, 불일치 시 None."""
    cfg = _load_dashboard_cfg()
    admin_pw = cfg.get("password", "")
    viewer_pw = cfg.get("viewer_password", "")
    if not admin_pw and not viewer_pw:
        return "admin"   # 비밀번호 미설정 시 개방
    pw = pw.strip()
    if admin_pw and secrets.compare_digest(pw, admin_pw.strip()):
        return "admin"
    if viewer_pw and secrets.compare_digest(pw, viewer_pw.strip()):
        return "viewer"
    return None


def _get_session(request: Request) -> dict | None:
    token = request.cookies.get("_tat_session")
    if not token:
        return None
    sess = _sessions.get(token)
    if not sess or time.time() > sess["exp"]:
        _sessions.pop(token, None)
        return None
    return sess


def _is_authenticated(request: Request) -> bool:
    return _get_session(request) is not None


def _get_role(request: Request) -> str | None:
    sess = _get_session(request)
    return sess["role"] if sess else None


def _require_admin(request: Request) -> None:
    """제어성 액션(정지/재개/분석실행/종목교체 등) — 조회 전용 계정 차단."""
    if _get_role(request) != "admin":
        raise HTTPException(status_code=403, detail="권한없음")


# 인증 불필요 경로
_PUBLIC = {"/login", "/logout", "/favicon.ico"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _PUBLIC or path.startswith("/static/login"):
        return await call_next(request)
    if not _is_authenticated(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=302)
    return await call_next(request)


@app.post("/login")
async def login(request: Request):
    try:
        body = await request.json()
        pw = body.get("password", "")
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    role = _check_password(pw)
    if role is None:
        return JSONResponse({"error": "wrong password"}, status_code=401)

    cfg = _load_dashboard_cfg()
    ttl = int(cfg.get("session_hours", 72)) * 3600
    token = secrets.token_hex(32)
    _sessions[token] = {"exp": time.time() + ttl, "role": role}

    resp = JSONResponse({"ok": True, "role": role})
    resp.set_cookie(
        "_tat_session", token,
        max_age=ttl, httponly=True, samesite="lax"
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("_tat_session")
    return resp


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


# ── 실계좌 데이터 캐시 — API 실패 시 마지막 성공값 유지
_acct_cache: dict = {
    "market_value": None,
    "unrealized_pnl": None,
    "daily_pnl": None,
    "cash_krw": None,
    "items": [],
    "overview": None,
    "ts": 0.0,        # 마지막 성공 시각 (epoch)
}
_CACHE_TTL = 60       # 60초 이내면 캐시 재사용 (API 실패 시 무조건 재사용)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ---------- 조회 ----------
@app.get("/api/status")
def status(request: Request) -> dict:
    s = dict(state.get())
    try:
        import yaml
        from pathlib import Path as _P
        cfg = yaml.safe_load((_P(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")) or {}
        live = cfg.get("live", {})
        if live.get("enabled"):
            s["mode"] = "live"
            s["live_symbols"] = live.get("symbols", [])
    except Exception:
        pass
    s["role"] = _get_role(request) or "admin"
    return s


def _unwrap_krw(obj) -> float | None:
    """Toss API 중첩 금액 객체 → float. {amount:{krw:'xxx'}} 등 처리."""
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, str):
        return float(obj) if obj else None
    if isinstance(obj, dict):
        v = obj.get("amount", obj.get("krw", obj.get("value")))
        if isinstance(v, dict):
            v = v.get("krw", v.get("amount", v.get("value")))
        return float(v) if v is not None else None
    return None


def _refresh_acct_cache() -> bool:
    """실계좌 holdings + buying_power 조회 후 캐시 갱신. 성공 시 True."""
    acct = runtime.get("account")
    if not acct:
        return False
    try:
        data   = acct.holdings()
        result = data.get("result", data) if isinstance(data, dict) else {}
        if isinstance(result, dict):
            _acct_cache["market_value"]   = _unwrap_krw(result.get("marketValue"))
            _acct_cache["unrealized_pnl"] = _unwrap_krw(result.get("profitLoss"))
            _acct_cache["daily_pnl"]      = _unwrap_krw(result.get("dailyProfitLoss"))
            _acct_cache["overview"]       = {k: v for k, v in result.items() if k != "items"}
            _acct_cache["items"]          = result.get("items", [])
        cash = float(acct.buying_power("KRW"))
        _acct_cache["cash_krw"] = cash
        _acct_cache["ts"] = time.time()
        return True
    except Exception:
        return False


def _get_acct(force: bool = False) -> dict:
    """캐시가 TTL 이내면 재사용, 아니면 갱신 시도. 실패 시 캐시 그대로 반환."""
    age = time.time() - _acct_cache["ts"]
    if force or age > _CACHE_TTL:
        _refresh_acct_cache()
    return _acct_cache


@app.get("/api/summary")
def summary() -> dict:
    """대시보드 요약 — 실계좌 데이터 기준 (캐시 60초)."""
    today = db.now()[:10]
    orders_today = [o for o in db.recent("orders", 200) if o["ts"].startswith(today)]
    filled  = sum(1 for o in orders_today if o["status"] == "FILLED")
    pending = sum(1 for o in orders_today if o["status"] == "PENDING")
    pnl_hist = db.pnl_history(30)

    c = _get_acct()
    mv  = c["market_value"]
    csh = c["cash_krw"]
    total = (mv + csh) if (mv is not None and csh is not None) else None

    return {
        "total_asset":        total,
        "market_value":       mv,
        "cash_krw":           csh,
        "unrealized_pnl":     c["unrealized_pnl"],
        "today_realized_pnl": c.get("daily_pnl"),  # 브로커 실제 당일 손익(실현+평가 변동) — 하드코딩 0 제거
        "orders_today":       len(orders_today),
        "orders_filled":      filled,
        "orders_pending":     pending,
        "pnl_history":        pnl_hist,
    }


@app.get("/api/paper")
def paper_state() -> dict:
    """페이퍼 트레이딩 포트폴리오 현황."""
    try:
        from engine.paper import snapshot, total_asset
        snap = snapshot()
        asset = total_asset()
        initial = snap["initial_cash"]
        pnl_total = asset - initial if initial else 0
        return {
            "cash": snap["cash"],
            "initial_cash": initial,
            "total_asset": asset,
            "total_pnl": pnl_total,
            "total_pnl_pct": pnl_total / initial * 100 if initial else 0,
            "today_realized": snap["today_realized"],
            "today_orders": snap["today_orders"],
            "positions": [
                {
                    "symbol": sym,
                    "name": pos["name"],
                    "qty": pos["qty"],
                    "avg_price": pos["avg_price"],
                }
                for sym, pos in snap["positions"].items()
            ],
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


_last_pos_error = ""


@app.get("/api/positions")
def positions() -> dict:
    """보유 종목 — {overview, items, cash_krw}. 캐시 공유로 rate limit 방지."""
    global _last_pos_error
    if runtime.get("account") is None:
        return {"overview": None, "items": [], "cash_krw": None}
    try:
        c = _get_acct()
        _last_pos_error = ""
        return {
            "overview": c["overview"],
            "items":    c["items"],
            "cash_krw": c["cash_krw"],
        }
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if msg != _last_pos_error:
            db.log("ERROR", "SYS", f"holdings 조회 실패: {msg}")
            _last_pos_error = msg
        c = _acct_cache
        return {"overview": c["overview"], "items": c["items"], "cash_krw": c["cash_krw"]}


@app.get("/api/signals")
def signals(limit: int = 30) -> list:
    return db.recent("signals", limit)


@app.get("/api/orders")
def orders(limit: int = 50) -> list:
    return db.recent("orders", limit)


@app.get("/api/logs")
def logs(limit: int = 100, level: str = "") -> list:
    rows = db.recent("logs", 300)
    if level:
        rows = [r for r in rows if r["level"] == level]
    return rows[:limit]


# ---------- 오늘 목표가 ----------
@app.get("/api/targets")
def get_targets() -> dict:
    try:
        import yaml
        from pathlib import Path
        from datetime import date as _date
        from data.collector import load_candles

        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        watchlist = cfg.get("watchlist", [])
        risk = cfg.get("risk", {})
        stop_loss_rate = risk.get("stop_loss", -0.03)
        daily_buy_limit = risk.get("daily_buy_limit", 1_000_000)

        market = runtime.get("market")
        symbols = [w["symbol"] for w in watchlist]

        # 현재가 배치 조회
        prices: dict = {}
        if market and symbols:
            try:
                for p in market.prices(symbols).get("result", []):
                    try:
                        prices[p["symbol"]] = float(p["lastPrice"])
                    except (KeyError, ValueError):
                        pass
            except Exception:
                pass

        today_str = _date.today().isoformat()
        results = []

        for w in watchlist:
            sym = w["symbol"]
            k = float(w.get("k", 0.5))
            name = w.get("name", sym)

            candles = load_candles(sym, "1d")
            today_open = prev_range = None

            if candles:
                last = candles[-1]
                if last["ts"][:10] == today_str:
                    today_open = last["open"]
                    if len(candles) >= 2:
                        p = candles[-2]
                        prev_range = p["high"] - p["low"]
                else:
                    prev_range = last["high"] - last["low"]

            # DB에 오늘 시가 없으면 API에서 조회
            if today_open is None and market:
                try:
                    r = market.candles(sym, interval="1d", count=1)
                    clist = r.get("result", {}).get("candles", [])
                    if clist and clist[0]["timestamp"][:10] == today_str:
                        today_open = float(clist[0]["openPrice"])
                except Exception:
                    pass

            target = round(today_open + k * prev_range) if (today_open and prev_range) else None
            current = prices.get(sym)

            gap = gap_pct = progress = reached = stop_price = est_qty = None
            if target:
                stop_price = round(target * (1 + stop_loss_rate))
                est_qty = max(1, int(daily_buy_limit / target))
                if current:
                    gap = round(current - target)
                    gap_pct = round(gap / target * 100, 1)
                    reached = current >= target
                    if today_open and today_open < target:
                        progress = round(
                            min(100, max(0, (current - today_open) / (target - today_open) * 100)), 1
                        )

            results.append({
                "symbol": sym, "name": name, "k": k,
                "today_open": today_open,
                "prev_range": round(prev_range) if prev_range else None,
                "target": target,
                "current": current,
                "gap": gap, "gap_pct": gap_pct,
                "reached": reached, "progress": progress,
                "stop_price": stop_price, "est_qty": est_qty,
            })

        return {"targets": results, "updated": db.now()}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------- 설정 ----------
@app.get("/api/config")
def get_config() -> dict:
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------- 실시간 현재가 ----------
@app.get("/api/prices")
def live_prices() -> dict:
    """watchlist 전 종목 현재가 반환 (5초 폴링용 경량 엔드포인트)."""
    market = runtime.get("market")
    if not market:
        return {"prices": {}}
    try:
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8")) or {}
        symbols = [w["symbol"] for w in cfg.get("watchlist", [])]
        if not symbols:
            return {"prices": {}}
        raw = market.prices(symbols)
        return {"prices": {p["symbol"]: float(p["lastPrice"]) for p in raw.get("result", [])}}
    except Exception:
        return {"prices": {}}


# ---------- 스캐너 ----------
@app.get("/api/scanner")
def scanner_results() -> dict:
    from scanner.screener import get_state
    return get_state()


@app.post("/api/scanner/run")
def scanner_run(request: Request) -> dict:
    _require_admin(request)
    import threading
    from scanner.screener import run_scan
    market = runtime.get("market")
    threading.Thread(target=run_scan, args=(market,), daemon=True).start()
    return {"status": "started"}


# ---------- 당일 분봉 (보유종목 미니차트) — 서버 캐시로 rate limit 방지 ----------
_intraday_cache: dict = {}   # symbol → {prices, times, ts}
_INTRADAY_TTL = 60           # 60초 캐시


def _fetch_intraday(symbol: str) -> dict:
    """단일 종목 분봉 조회 + 캐시."""
    now = time.time()
    cached = _intraday_cache.get(symbol)
    if cached and now - cached["ts"] < _INTRADAY_TTL:
        return cached

    from engine.scheduler import today_str
    today = today_str()   # KST 기준 오늘 날짜
    try:
        raw = runtime["market"].candles(symbol, interval="1m", count=200)
        candles = (raw.get("result") or {}).get("candles", [])
        today_c = [c for c in candles if str(c.get("timestamp", "")).startswith(today)]
        if not today_c:
            today_c = candles
        today_c.sort(key=lambda c: c.get("timestamp", ""))
        prices = [float(c["closePrice"]) for c in today_c if c.get("closePrice")]
        times  = [str(c.get("timestamp", ""))[11:16] for c in today_c]
        open_p = float(today_c[0]["openPrice"]) if today_c and today_c[0].get("openPrice") else (prices[0] if prices else None)
        result = {"prices": prices, "times": times, "open": open_p, "ts": now}
        _intraday_cache[symbol] = result
        return result
    except Exception:
        return _intraday_cache.get(symbol, {"prices": [], "times": [], "ts": 0})


@app.get("/api/intraday/{symbol}")
def intraday(symbol: str) -> dict:
    if runtime["market"] is None:
        return {"symbol": symbol, "prices": [], "times": []}
    d = _fetch_intraday(symbol)
    return {"symbol": symbol, "prices": d["prices"], "times": d["times"]}


@app.get("/api/intraday-all")
def intraday_all() -> dict:
    """보유 종목 전체 분봉을 한 번에 반환 — 클라이언트 병렬 호출 대신 사용."""
    if runtime["market"] is None:
        return {"charts": {}}
    from engine.scheduler import session as _sess
    if _sess() in ("WEEKEND", "HOLIDAY"):
        return {"charts": {}}
    # 실계좌 보유 KR 종목 + config.yaml watchlist 합산
    items = _acct_cache.get("items") or []
    symbols: list[str] = [it["symbol"] for it in items if it.get("marketCountry", "KR") == "KR"]
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
        for w in cfg.get("watchlist", []):
            s = w.get("symbol", "")
            if s and s not in symbols:
                symbols.append(s)
    except Exception:
        pass
    result = {}
    for sym in symbols:
        d = _fetch_intraday(sym)
        if d["prices"]:
            result[sym] = {"prices": d["prices"], "times": d["times"], "open": d.get("open")}
        time.sleep(0.3)   # 종목간 0.3초 간격으로 rate limit 회피
    return {"charts": result}


# ---------- 스파크라인 (일봉 20일, 스캐너용) ----------
@app.get("/api/sparkline/{symbol}")
def sparkline(symbol: str, days: int = 30) -> dict:
    from data.collector import load_candles
    candles = load_candles(symbol, "1d", limit=days + 5)
    recent = candles[-(days):]
    return {
        "symbol": symbol,
        "prices": [c["close"] for c in recent],
        "dates":  [c["ts"][:10] for c in recent],
        "open":   recent[0]["close"] if recent else None,
        "close":  recent[-1]["close"] if recent else None,
    }


@app.post("/api/sparklines")
async def sparklines_batch(req: Request) -> dict:
    """여러 종목 스파크라인 일괄 조회."""
    body = await req.json()
    symbols = body.get("symbols", [])
    days    = body.get("days", 20)
    from data.collector import load_candles
    result = {}
    for sym in symbols:
        candles = load_candles(sym, "1d", limit=days + 5)
        recent = candles[-(days):]
        result[sym] = {
            "prices": [c["close"] for c in recent],
            "open":   recent[0]["close"] if recent else None,
            "close":  recent[-1]["close"] if recent else None,
        }
    return result


# ---------- 일별 매매현황 ----------
@app.get("/api/daily-history")
def daily_history(days: int = 90) -> dict:
    from collections import defaultdict
    pnl_hist = db.pnl_history(days)
    orders   = db.recent("orders", 1000)

    initial_cash = 10_000_000
    try:
        import yaml
        from pathlib import Path as _Path
        with open(_Path(__file__).parent.parent / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        initial_cash = cfg.get("paper", {}).get("initial_cash", 10_000_000)
    except Exception:
        pass

    # daily_pnl 레코드를 날짜 기준 dict로
    pnl_by_date: dict[str, dict] = {r["date"]: r for r in pnl_hist}

    # orders를 날짜별로 그룹핑
    orders_by_date: dict[str, list] = defaultdict(list)
    for o in orders:
        orders_by_date[o["ts"][:10]].append(o)

    # daily_pnl 날짜 + orders 날짜 합산 (중복 제거, 최근 days일 이내)
    all_dates = sorted(set(pnl_by_date.keys()) | set(orders_by_date.keys()), reverse=False)

    history = []
    cumulative = 0.0
    for date in all_dates:
        record     = pnl_by_date.get(date, {})
        pnl        = record.get("realized_pnl") or 0
        cumulative += pnl
        day_orders = orders_by_date.get(date, [])
        # FILLED 주문 + 오늘(아직 flush 전) PENDING BUY도 표시
        filled     = [o for o in day_orders if o.get("status") in ("FILLED", "PENDING")]
        buys       = [o for o in filled if o.get("side") == "BUY"]
        sells      = [o for o in filled if o.get("side") == "SELL"]
        history.append({
            "date":           date,
            "realized_pnl":   pnl,
            "total_asset":    record.get("total_asset"),
            "cumulative_pnl": round(cumulative),
            "return_pct":     round(cumulative / initial_cash * 100, 2),
            "buys":           len(buys),
            "sells":          len(sells),
            "trades": [
                {
                    "ts":     o["ts"][11:16],
                    "symbol": o.get("symbol", ""),
                    "name":   o.get("name", ""),
                    "side":   o.get("side", ""),
                    "status": o.get("status", ""),
                    "mode":   o.get("mode", "paper"),
                    "price":  o.get("fill_price") or o.get("price"),
                    "qty":    o.get("qty"),
                    "reason": o.get("reason", ""),
                }
                for o in sorted(filled, key=lambda x: x["ts"])
            ],
        })

    total_days   = len(history)
    winning_days = sum(1 for h in history if h["realized_pnl"] > 0)
    losing_days  = sum(1 for h in history if h["realized_pnl"] < 0)
    total_pnl    = sum(h["realized_pnl"] for h in history)

    return {
        "history": history,
        "summary": {
            "total_days":   total_days,
            "winning_days": winning_days,
            "losing_days":  losing_days,
            "win_rate":     round(winning_days / total_days * 100, 1) if total_days else 0,
            "total_pnl":    round(total_pnl),
            "return_pct":   round(total_pnl / initial_cash * 100, 2),
            "initial_cash": initial_cash,
        }
    }


# ---------- 종목 추천 ----------
@app.get("/api/recommendations")
def recommendations() -> dict:
    from scanner.recommender import get_state
    return get_state()


@app.post("/api/recommendations/run")
def recommendations_run(request: Request) -> dict:
    _require_admin(request)
    import threading
    from scanner.recommender import run_analysis
    market = runtime.get("market")
    cfg = None
    try:
        import yaml
        from pathlib import Path
        with open(Path(__file__).parent.parent / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        pass
    threading.Thread(target=run_analysis, args=(market, cfg), daemon=True).start()
    return {"status": "started"}


@app.post("/api/watchlist/replace")
async def watchlist_replace(req: Request) -> dict:
    """기존 종목을 새 종목으로 교체 (config.yaml 업데이트)."""
    _require_admin(req)
    body = await req.json()
    remove_sym = body.get("remove", "").strip()
    add_sym    = body.get("add", "").strip()
    add_name   = body.get("name", add_sym)
    add_k      = float(body.get("k", 0.5))
    if not remove_sym or not add_sym:
        raise HTTPException(400, "remove and add required")
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        wl = cfg.get("watchlist", [])
        cfg["watchlist"] = [w for w in wl if w["symbol"] != remove_sym]
        if not any(w["symbol"] == add_sym for w in cfg["watchlist"]):
            cfg["watchlist"].append({"symbol": add_sym, "name": add_name, "k": add_k})
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        db.log("INFO", "SYS", f"종목 교체: {remove_sym} -> {add_sym} {add_name} K={add_k}")
        return {"status": "ok", "removed": remove_sym, "added": add_sym}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------- 캔들 수집 ----------
_collecting: set[str] = set()


@app.post("/api/collect/{symbol}")
def collect_symbol(symbol: str, request: Request) -> dict:
    """단일 종목 캔들 수집 (백그라운드). 서버 실행 중 market 없어도 자체 생성."""
    _require_admin(request)
    import threading
    from data.collector import collect

    if symbol in _collecting:
        return {"status": "already_collecting", "symbol": symbol}

    def _get_market():
        m = runtime.get("market")
        if m:
            return m
        from core.http import TossClient
        from core.market import MarketApi
        return MarketApi(TossClient())

    def _run():
        _collecting.add(symbol)
        try:
            market_api = _get_market()
            n = collect(market_api, symbol, "1d", 1095)
            db.log("INFO", "SYS", f"수동 수집 완료: {symbol} {n}봉")
        except Exception as e:
            db.log("ERROR", "SYS", f"수동 수집 실패: {symbol} {e}")
        finally:
            _collecting.discard(symbol)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "collecting", "symbol": symbol}


@app.get("/api/collect/status")
def collect_status() -> dict:
    """현재 수집 중인 종목 목록."""
    from data.collector import load_candles
    statuses = {}
    for sym in list(_collecting):
        candles = load_candles(sym, "1d")
        statuses[sym] = {"collecting": True, "candle_count": len(candles)}
    return {"collecting": list(_collecting), "statuses": statuses}


@app.post("/api/scanner/add")
async def scanner_add(req: Request) -> dict:
    """종목을 config.yaml 워치리스트에 추가."""
    _require_admin(req)
    body = await req.json()
    symbol = body.get("symbol", "").strip()
    name = body.get("name", symbol)
    k = float(body.get("k", 0.5))
    if not symbol:
        raise HTTPException(400, "symbol required")
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        watchlist = cfg.setdefault("watchlist", [])
        if any(w["symbol"] == symbol for w in watchlist):
            return {"status": "already_exists"}
        watchlist.append({"symbol": symbol, "name": name, "k": k})
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        db.log("INFO", "SYS", f"워치리스트 추가: {symbol} {name} K={k}")
        return {"status": "added"}
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------- 리스크 게이지 ----------
@app.get("/api/risk-gauge")
def risk_gauge() -> dict:
    """일 매수 한도·CB 소진율·현금 비중·마감 카운트다운."""
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        risk_cfg = cfg.get("risk", {})
        daily_buy_limit = risk_cfg.get("daily_buy_limit", 1_000_000)
        daily_loss_cb   = abs(risk_cfg.get("daily_loss_cb", 0.02))
        max_orders      = risk_cfg.get("max_daily_orders", 20)

        from engine.paper import snapshot, total_asset
        snap  = snapshot()
        asset = total_asset()
        initial = snap["initial_cash"]

        today_buy    = snap.get("today_buy_amount", 0)
        today_real   = snap.get("today_realized", 0)
        cash         = snap.get("cash", initial)
        today_orders = snap.get("today_orders", 0)

        buy_used_pct = round(today_buy / daily_buy_limit * 100, 1) if daily_buy_limit else 0
        cb_threshold = initial * daily_loss_cb
        cb_used_pct  = round(min(100, abs(today_real) / cb_threshold * 100), 1) \
                       if (cb_threshold and today_real < 0) else 0
        cash_pct     = round(cash / asset * 100, 1) if asset else 100

        # 마감 카운트다운 (초)
        from engine.scheduler import session as get_session
        sess = get_session()
        close_seconds = None
        if sess == "OPEN":
            from datetime import datetime as _dt
            now = _dt.now()
            close = now.replace(hour=15, minute=30, second=0, microsecond=0)
            close_seconds = max(0, int((close - now).total_seconds()))

        return {
            "daily_buy_limit": daily_buy_limit,
            "today_buy_amount": today_buy,
            "buy_used_pct": buy_used_pct,
            "cb_threshold_amount": round(cb_threshold),
            "today_realized": today_real,
            "cb_used_pct": cb_used_pct,
            "cash": cash,
            "total_asset": asset,
            "cash_pct": cash_pct,
            "invest_pct": round(100 - cash_pct, 1),
            "today_orders": today_orders,
            "max_daily_orders": max_orders,
            "order_used_pct": round(today_orders / max_orders * 100, 1) if max_orders else 0,
            "session": sess,
            "close_seconds": close_seconds,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------- 누적 성과 분석 ----------
@app.get("/api/performance")
def performance() -> dict:
    """페이퍼 트레이딩 누적 성과 통계."""
    try:
        from engine.paper import snapshot, total_asset
        snap    = snapshot()
        initial = snap["initial_cash"]
        asset   = total_asset()

        pnl_hist = db.pnl_history(365)
        if not pnl_hist:
            return {
                "no_data": True,
                "total_trading_days": 0,
                "message": "첫 거래일 마감 후 통계가 생성됩니다.",
            }

        daily_pnls = [p["realized_pnl"] for p in pnl_hist]
        winning_days = sum(1 for p in daily_pnls if p > 0)
        losing_days  = sum(1 for p in daily_pnls if p < 0)
        flat_days    = len(daily_pnls) - winning_days - losing_days

        wins   = [p for p in daily_pnls if p > 0]
        losses = [p for p in daily_pnls if p < 0]
        total_win  = sum(wins)
        total_loss = abs(sum(losses))

        profit_factor = round(total_win / total_loss, 2) if total_loss else None
        avg_win  = round(total_win  / len(wins))   if wins   else 0
        avg_loss = round(total_loss / len(losses)) if losses else 0

        # 최대 연속 손실
        max_consec = cur = 0
        for p in daily_pnls:
            cur = cur + 1 if p < 0 else 0
            max_consec = max(max_consec, cur)

        # 최대 낙폭 (MDD)
        running = initial
        peak = initial
        max_dd = 0.0
        for p in daily_pnls:
            running += p
            peak = max(peak, running)
            dd = (running - peak) / peak * 100
            max_dd = min(max_dd, dd)

        # 종목별 거래 건수 (주문 테이블 기반)
        orders = db.recent("orders", 500)
        sym_counts: dict[str, dict] = {}
        for o in orders:
            if o.get("status") != "FILLED":
                continue
            sym = o.get("symbol", "?")
            name = o.get("name", sym)
            if sym not in sym_counts:
                sym_counts[sym] = {"name": name, "buy": 0, "sell": 0}
            if o.get("side") == "BUY":
                sym_counts[sym]["buy"] += 1
            else:
                sym_counts[sym]["sell"] += 1

        by_symbol = sorted(
            [{"symbol": k, "name": v["name"],
              "trades": v["sell"], "buys": v["buy"]}
             for k, v in sym_counts.items()],
            key=lambda x: -x["trades"],
        )

        return {
            "no_data": False,
            "total_trading_days": len(daily_pnls),
            "winning_days": winning_days,
            "losing_days": losing_days,
            "flat_days": flat_days,
            "win_rate": round(winning_days / len(daily_pnls) * 100, 1),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "max_consecutive_losses": max_consec,
            "max_drawdown_pct": round(max_dd, 2),
            "total_realized": round(sum(daily_pnls)),
            "total_return_pct": round(sum(daily_pnls) / initial * 100, 2),
            "current_asset": asset,
            "by_symbol": by_symbol,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------- 제어 ----------
@app.get("/api/time")
def server_time() -> dict:
    """서버(NTP 동기화) 현재 시각을 ms epoch로 반환."""
    return {"epoch_ms": int(time.time() * 1000)}


@app.post("/api/control/{action}")
def control(action: str, request: Request) -> dict:
    _require_admin(request)
    if action == "pause":
        db.log("WARN", "SYS", "사용자 요청: 일시정지")
        return state.set(paused=True)
    if action == "resume":
        db.log("INFO", "SYS", "사용자 요청: 재개")
        return state.set(paused=False)
    if action == "kill":
        db.log("ERROR", "SYS", "🛑 킬 스위치 발동 — 신규 주문 차단·루프 정지")
        return state.set(killed=True, paused=True, running=False)
    raise HTTPException(400, f"unknown action: {action}")
