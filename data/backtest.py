"""백테스트 엔진 — 수집된 일봉으로 전략 성과 시뮬레이션.

사용:
  python -m data.backtest 005930                 # K=0.5 단일 테스트
  python -m data.backtest 005930 --sweep         # K 0.1~0.9 전체 비교
  python -m data.backtest 005930 000660 --k 0.6
"""
import argparse
import math

from data.collector import load_candles
from strategy.volatility_breakout import VolatilityBreakout

# 비용 가정 (국내 주식)
FEE_RATE = 0.00015    # 매매 수수료 0.015% (편도)
TAX_RATE = 0.0018     # 매도 시 증권거래세 0.18%
SLIPPAGE = 0.0005     # 슬리피지 0.05% (편도)


def run(candles: list[dict], strategy) -> dict:
    """일봉 리스트로 전략 시뮬레이션. 성과 지표 dict 반환."""
    trades: list[float] = []   # 매매별 수익률
    equity = [1.0]             # 누적 자산 곡선

    for i in range(1, len(candles)):
        prev, today = candles[i - 1], candles[i]
        entry = strategy.entry_price(prev, today)
        if entry is None:
            equity.append(equity[-1])
            continue
        buy = entry * (1 + SLIPPAGE) * (1 + FEE_RATE)
        sell = today["close"] * (1 - SLIPPAGE) * (1 - FEE_RATE - TAX_RATE)
        r = sell / buy - 1
        trades.append(r)
        equity.append(equity[-1] * (1 + r))

    return _metrics(trades, equity, len(candles))


def _metrics(trades: list[float], equity: list[float], n_days: int) -> dict:
    total_return = equity[-1] - 1
    years = max(n_days / 250, 0.1)
    cagr = equity[-1] ** (1 / years) - 1 if equity[-1] > 0 else -1

    # MDD
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)

    wins = [t for t in trades if t > 0]
    daily_rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    mean = sum(daily_rets) / len(daily_rets) if daily_rets else 0
    std = math.sqrt(sum((r - mean) ** 2 for r in daily_rets) / len(daily_rets)) \
        if daily_rets else 0
    sharpe = mean / std * math.sqrt(250) if std > 0 else 0

    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "avg_trade": sum(trades) / len(trades) if trades else 0,
        "total_return": total_return,
        "cagr": cagr,
        "mdd": mdd,
        "sharpe": sharpe,
    }


def report(symbol: str, k: float, m: dict) -> str:
    return (f"  K={k:.1f} | 매매 {m['trades']:4d}회 | 승률 {m['win_rate']*100:5.1f}% | "
            f"누적 {m['total_return']*100:+7.1f}% | CAGR {m['cagr']*100:+6.1f}% | "
            f"MDD {m['mdd']*100:6.1f}% | 샤프 {m['sharpe']:5.2f}")


def main() -> None:
    p = argparse.ArgumentParser(description="변동성 돌파 백테스트")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--k", type=float, default=0.5)
    p.add_argument("--sweep", action="store_true", help="K 0.1~0.9 비교")
    args = p.parse_args()

    for sym in args.symbols:
        candles = load_candles(sym, "1d")
        if len(candles) < 100:
            print(f"❌ {sym}: 캔들 부족 ({len(candles)}봉). 수집.bat 먼저 실행하세요.")
            continue
        period = f"{candles[0]['ts'][:10]} ~ {candles[-1]['ts'][:10]}"
        print(f"\n▶ {sym} ({len(candles)}봉, {period})")
        print("  [참고] 단순 보유 수익률: "
              f"{(candles[-1]['close']/candles[0]['close']-1)*100:+.1f}%")

        ks = [round(0.1 * i, 1) for i in range(1, 10)] if args.sweep else [args.k]
        results = []
        for k in ks:
            m = run(candles, VolatilityBreakout(k))
            results.append((k, m))
            print(report(sym, k, m))

        if args.sweep:
            best = max(results, key=lambda x: x[1]["sharpe"])
            print(f"  ★ 샤프 기준 최적: K={best[0]:.1f}")


if __name__ == "__main__":
    main()
