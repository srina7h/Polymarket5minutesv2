#!/usr/bin/env python3
"""
EV Gap Parameter Sweep — Find the optimal configuration.

Tests all combinations of:
  - EV Gap thresholds: 3%, 5%, 6%, 7%, 8%, 9%, 10%, 12%, 15%
  - Position sizes: 2%, 3%, 5%, 7%, 10%
  - Over 3 days of data (~864 windows)

Outputs a ranked table of the best configurations by risk-adjusted return.
"""

import csv
import sys
import time
from datetime import datetime
from itertools import product

import requests

import config
from ev_strategy import evaluate_ev_gap


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANSI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
    CYAN = "\033[96m"; MAGENTA = "\033[95m"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_binance_klines(total_candles: int) -> list:
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []
    end_time = None
    remaining = total_candles
    print(f"  Fetching {total_candles} candles...", end=" ", flush=True)

    while remaining > 0:
        batch = min(remaining, 1000)
        params = {"symbol": "BTCUSDC", "interval": "1m", "limit": batch}
        if end_time:
            params["endTime"] = end_time - 1
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"\n  ❌ {e}")
            break
        if not data:
            break
        candles = [{"timestamp": d[0], "open": float(d[1]), "high": float(d[2]),
                     "low": float(d[3]), "close": float(d[4]), "volume": float(d[5])} for d in data]
        all_candles = candles + all_candles
        end_time = data[0][0]
        remaining -= len(data)
        if len(data) < batch:
            break
        time.sleep(0.2)

    print(f"✓ {len(all_candles)}")
    if all_candles:
        s = datetime.utcfromtimestamp(all_candles[0]["timestamp"] / 1000)
        e = datetime.utcfromtimestamp(all_candles[-1]["timestamp"] / 1000)
        print(f"  Range: {s:%Y-%m-%d %H:%M} → {e:%Y-%m-%d %H:%M} UTC ({(e-s).total_seconds()/3600:.0f}h)")
    return all_candles


def build_windows(candles):
    windows = []
    for i in range(0, len(candles) - 4, 5):
        wc = candles[i:i+5]
        if len(wc) < 5:
            break
        op = wc[0]["open"]; cp = wc[-1]["close"]; d = cp - op
        windows.append({
            "window_id": len(windows)+1, "timestamp": wc[0]["timestamp"],
            "candles": wc, "open_price": op, "close_price": cp,
            "high": max(c["high"] for c in wc), "low": min(c["low"] for c in wc),
            "volume": sum(c["volume"] for c in wc), "delta": d,
            "winner": "UP" if d >= 0 else "DOWN",
        })
    return windows


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Backtest core (fast, no printing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_fast_backtest(windows, capital, min_gap, pos_pct):
    """Run backtest with given params. Returns stats dict."""
    config.EV_MIN_GAP = min_gap
    config.EV_FLAT_POSITION_PCT = pos_pct

    cap = capital
    wins = losses = 0
    consec_loss = 0; max_consec = 0
    peak = cap; max_dd = 0
    total_pnl_win = 0; total_pnl_loss = 0
    cooldown = 0
    trades_list = []

    for w in windows:
        wid = w["window_id"]
        if wid < cooldown:
            continue
        if consec_loss >= config.MAX_CONSECUTIVE_LOSSES:
            cooldown = wid + 6
            consec_loss = 0
            continue

        result = evaluate_ev_gap(w)
        if result["action"] == "NO_TRADE":
            continue

        position = round(cap * pos_pct, 2)
        if position <= 0:
            continue

        direction = result["direction"]
        entry = result["entry_price"]
        won = (direction == "UP" and w["winner"] == "UP") or \
              (direction == "DOWN" and w["winner"] == "DOWN")

        if won:
            shares = position / entry if entry > 0 else 0
            pnl = shares * (1.0 - entry) - position * config.SIM_FEE_MAX * 0.5
            consec_loss = 0; wins += 1; total_pnl_win += pnl
        else:
            pnl = -position
            consec_loss += 1; max_consec = max(max_consec, consec_loss)
            losses += 1; total_pnl_loss += abs(pnl)

        cap += pnl
        peak = max(peak, cap)
        dd = (peak - cap) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
        trades_list.append({"pnl": pnl, "won": won, "ev_gap": result["ev_gap"]})

    total = wins + losses
    total_pnl = cap - capital
    win_rate = wins / total * 100 if total > 0 else 0
    pf = total_pnl_win / total_pnl_loss if total_pnl_loss > 0 else float("inf")
    avg_win = total_pnl_win / wins if wins > 0 else 0
    avg_loss = -total_pnl_loss / losses if losses > 0 else 0

    # Sharpe-like: mean trade pnl / stdev of trade pnls
    if trades_list and len(trades_list) > 1:
        pnls = [t["pnl"] for t in trades_list]
        import statistics
        mean_pnl = statistics.mean(pnls)
        std_pnl = statistics.stdev(pnls)
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0
    else:
        sharpe = 0

    return {
        "min_gap": min_gap, "pos_pct": pos_pct,
        "final_cap": round(cap, 2), "total_pnl": round(total_pnl, 2),
        "pnl_pct": round(total_pnl / capital * 100, 2),
        "trades": total, "wins": wins, "losses": losses,
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "max_consec_loss": max_consec,
        "sharpe": round(sharpe, 3),
        "trade_rate": round(total / len(windows) * 100, 1),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main Sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    CAPITAL = 100
    DAYS = 5
    total_candles = int(DAYS * 24 * 60)

    # Parameter grid
    gaps = [0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25]
    positions = [0.05]  # Static size equivalent to our $5 flat live sizing
    combos = list(product(gaps, positions))

    print(f"\n{C.BOLD}{C.MAGENTA}{'═' * 72}{C.RESET}")
    print(f"{C.BOLD}{C.MAGENTA}  🔬 EV Gap Parameter Sweep{C.RESET}")
    print(f"{C.MAGENTA}{'═' * 72}{C.RESET}\n")
    print(f"  Capital:      ${CAPITAL}")
    print(f"  Data:         {DAYS} days ({total_candles} candles)")
    print(f"  Gap values:   {[f'{g:.0%}' for g in gaps]}")
    print(f"  Position %:   {[f'{p:.0%}' for p in positions]}")
    print(f"  Total combos: {len(combos)}")
    print()

    # Fetch data once
    candles = fetch_binance_klines(total_candles)
    if len(candles) < 100:
        print("  ❌ Not enough data")
        sys.exit(1)
    windows = build_windows(candles)
    print(f"  Built {len(windows)} windows\n")

    # Run all combinations
    results = []
    print(f"  Running {len(combos)} backtests...", flush=True)
    t0 = time.time()

    for i, (gap, pos) in enumerate(combos):
        r = run_fast_backtest(windows, CAPITAL, gap, pos)
        results.append(r)
        if (i + 1) % 9 == 0 or i == len(combos) - 1:
            pct = (i + 1) / len(combos) * 100
            print(f"    [{i+1}/{len(combos)}] {pct:.0f}%", flush=True)

    elapsed = time.time() - t0
    print(f"  ✓ Done in {elapsed:.1f}s\n")

    # Sort by composite score: balance P&L, win rate, and risk
    # Score = pnl_pct * (win_rate/100) / max(max_dd_pct, 1) * profit_factor^0.5
    for r in results:
        dd = max(r["max_dd_pct"], 1)
        pf_adj = min(r["profit_factor"], 50) ** 0.5  # cap pf to avoid inf
        r["score"] = round(r["pnl_pct"] * (r["win_rate"] / 100) / dd * pf_adj, 2)

    results.sort(key=lambda x: x["score"], reverse=True)

    # Print top 20
    print(f"{C.BOLD}{C.CYAN}{'═' * 100}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  🏆 TOP 20 CONFIGURATIONS (ranked by risk-adjusted score){C.RESET}")
    print(f"{C.CYAN}{'═' * 100}{C.RESET}\n")

    header = (
        f"  {'Rank':>4}  {'Gap':>5}  {'Pos%':>5}  {'P&L%':>8}  "
        f"{'Final$':>8}  {'Trades':>6}  {'WinR%':>6}  {'AvgW':>7}  "
        f"{'AvgL':>7}  {'PF':>6}  {'MaxDD%':>7}  {'Sharpe':>7}  {'Score':>7}"
    )
    print(f"{C.DIM}{header}{C.RESET}")
    print(f"  {'─' * 96}")

    for i, r in enumerate(results[:20]):
        pnl_c = C.GREEN if r["total_pnl"] > 0 else C.RED

        # Highlight the #1 pick
        if i == 0:
            pre = f"{C.BOLD}{C.GREEN}  "
            post = C.RESET
        elif i < 3:
            pre = f"{C.GREEN}  "
            post = C.RESET
        else:
            pre = "  "
            post = ""

        line = (
            f"{pre}{i+1:>4}  {r['min_gap']:>4.0%}  {r['pos_pct']:>4.0%}  "
            f"{pnl_c}{r['pnl_pct']:>+7.1f}%{C.RESET}  "
            f"${r['final_cap']:>7,.0f}  {r['trades']:>6}  {r['win_rate']:>5.1f}%  "
            f"{C.GREEN}${r['avg_win']:>6.2f}{C.RESET}  "
            f"{C.RED}${r['avg_loss']:>6.2f}{C.RESET}  "
            f"{r['profit_factor']:>5.1f}  {r['max_dd_pct']:>6.1f}%  "
            f"{r['sharpe']:>6.3f}  {r['score']:>6.1f}{post}"
        )
        print(line)

    # Print worst 5 for context
    print(f"\n  {C.DIM}... {len(results) - 20} more configurations ...{C.RESET}")
    print(f"\n  {C.BOLD}Bottom 5:{C.RESET}")
    for r in results[-5:]:
        pnl_c = C.GREEN if r["total_pnl"] > 0 else C.RED
        print(
            f"        Gap={r['min_gap']:.0%}  Pos={r['pos_pct']:.0%}  "
            f"{pnl_c}P&L={r['pnl_pct']:+.1f}%{C.RESET}  "
            f"WR={r['win_rate']:.0f}%  Trades={r['trades']}  DD={r['max_dd_pct']:.1f}%"
        )

    # Best pick
    best = results[0]
    print(f"\n{C.BOLD}{C.GREEN}{'═' * 72}{C.RESET}")
    print(f"{C.BOLD}{C.GREEN}  🏆 BEST CONFIGURATION{C.RESET}")
    print(f"{C.GREEN}{'═' * 72}{C.RESET}")
    print(f"  EV Min Gap:      {best['min_gap']:.0%}")
    print(f"  Position Size:   {best['pos_pct']:.0%} of capital")
    print(f"  P&L:             {C.GREEN}${best['total_pnl']:+,.2f} ({best['pnl_pct']:+.1f}%){C.RESET}")
    print(f"  Final Capital:   ${best['final_cap']:,.2f}")
    print(f"  Trades:          {best['trades']} ({best['trade_rate']:.0f}% rate)")
    print(f"  Win Rate:        {best['win_rate']:.1f}%")
    print(f"  Profit Factor:   {best['profit_factor']:.1f}")
    print(f"  Max Drawdown:    {best['max_dd_pct']:.1f}%")
    print(f"  Sharpe:          {best['sharpe']:.3f}")
    print(f"  Score:           {best['score']:.1f}")
    print(f"{C.GREEN}{'═' * 72}{C.RESET}\n")

    # Save all results to CSV
    csv_path = "ev_sweep_results.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(f"  💾 All {len(results)} results saved to {csv_path}")

    # Print recommended config.py changes
    print(f"\n  {C.BOLD}Recommended config.py update:{C.RESET}")
    print(f"  {C.CYAN}EV_MIN_GAP = {best['min_gap']:.2f}              "
          f"# {best['min_gap']:.0%} minimum EV gap{C.RESET}")
    print(f"  {C.CYAN}EV_FLAT_POSITION_PCT = {best['pos_pct']:.2f}    "
          f"# {best['pos_pct']:.0%} of capital per trade{C.RESET}")
    print()


if __name__ == "__main__":
    main()
