#!/usr/bin/env python3
"""
EV Gap Strategy — Backtester
Fetches historical BTC 1-min candles from Binance and replays the EV Gap
strategy across 5-minute windows.

Usage:
    python3 backtest_ev.py                          # Default: 300 candles (5h)
    python3 backtest_ev.py --candles 600            # ~10 hours
    python3 backtest_ev.py --capital 100            # $100 starting capital
    python3 backtest_ev.py --capital 100 --candles 300 --output ev_results.csv
"""

import argparse
import csv
import os
import statistics
import sys
import time
from datetime import datetime, timedelta

import requests

import config
from ev_strategy import evaluate_ev_gap


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANSI Colors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    MAGENTA = "\033[95m"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Fetching (Pyth Benchmarks)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_pyth_klines(total_candles: int = 300) -> list:
    """Fetch historical 1-minute BTC/USD candles from Pyth Benchmarks."""
    print(f"  Fetching {total_candles} 1-min BTC/USD candles from Pyth...", end=" ", flush=True)

    end_time = int(time.time())
    start_time = end_time - int((total_candles + 60) * 60) # Fetch an extra hour buffer to ensure enough data
    
    url = f"https://benchmarks.pyth.network/v1/shims/tradingview/history?symbol=Crypto.BTC%2FUSD&resolution=1&from={start_time}&to={end_time}"
    
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"\\n  ❌ Pyth API error: {e}")
        return []

    if data.get("s") != "ok" or not data.get("t"):
        print(f"\\n  ❌ No data returned from Pyth.")
        return []

    candles = []
    for i in range(len(data["t"])):
        candles.append({
            "timestamp": data["t"][i] * 1000,
            "open": float(data["o"][i]),
            "high": float(data["h"][i]),
            "low": float(data["l"][i]),
            "close": float(data["c"][i]),
            "volume": float(data.get("v", [0]*len(data["t"]))[i]),
        })

    # Slice to exact requested amount 
    all_candles = candles[-total_candles:] if len(candles) >= total_candles else candles

    print(f"✓ {len(all_candles)} candles")
    if all_candles:
        start_dt = datetime.utcfromtimestamp(all_candles[0]["timestamp"] / 1000)
        end_dt = datetime.utcfromtimestamp(all_candles[-1]["timestamp"] / 1000)
        hours = (end_dt - start_dt).total_seconds() / 3600
        print(f"  Range: {start_dt.strftime('%Y-%m-%d %H:%M')} → {end_dt.strftime('%Y-%m-%d %H:%M')} UTC ({hours:.1f} hours)")

    return all_candles


def build_windows(candles: list) -> list:
    """Group 1-min candles into 5-minute windows."""
    windows = []
    for i in range(0, len(candles) - 4, 5):
        wc = candles[i:i + 5]
        if len(wc) < 5:
            break

        open_price = wc[0]["open"]
        close_price = wc[-1]["close"]
        delta = close_price - open_price
        winner = "UP" if delta >= 0 else "DOWN"

        windows.append({
            "window_id": len(windows) + 1,
            "timestamp": wc[0]["timestamp"],
            "candles": wc,
            "open_price": open_price,
            "close_price": close_price,
            "high": max(c["high"] for c in wc),
            "low": min(c["low"] for c in wc),
            "volume": sum(c["volume"] for c in wc),
            "delta": delta,
            "winner": winner,
        })

    return windows


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Backtest Runner (EV Gap)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_ev_backtest(windows: list, initial_capital: float) -> dict:
    """Run the EV Gap strategy across all windows."""

    capital = initial_capital
    trades = []
    all_decisions = []
    consecutive_losses = 0
    max_consecutive_losses = 0
    peak_capital = capital
    max_drawdown = 0
    cooldown_until_window = 0
    ev_gaps_seen = []

    for w in windows:
        wid = w["window_id"]

        # Risk: cooldown after consecutive losses
        if wid < cooldown_until_window:
            all_decisions.append({
                "window_id": wid, "action": "RISK_BLOCK",
                "reason": "Cooldown", "pnl": 0,
            })
            continue

        if consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            cooldown_until_window = wid + 6
            consecutive_losses = 0
            all_decisions.append({
                "window_id": wid, "action": "RISK_BLOCK",
                "reason": f"{config.MAX_CONSECUTIVE_LOSSES} consecutive losses → cooldown",
                "pnl": 0,
            })
            continue

        # ── Run EV Gap evaluation ──────────────
        result = evaluate_ev_gap(w)
        ev_gaps_seen.append(result.get("ev_gap", 0))

        if result["action"] == "NO_TRADE":
            all_decisions.append({
                "window_id": wid,
                "action": "NO_TRADE",
                "reason": result["reason"],
                "ev_gap": result.get("ev_gap", 0),
                "pnl": 0,
            })
            continue

        # ── Position sizing: flat % of capital ──
        position_pct = getattr(config, "EV_FLAT_POSITION_PCT", 0.05)
        position = round(capital * position_pct, 2)

        if position <= 0:
            all_decisions.append({
                "window_id": wid, "action": "NO_TRADE",
                "reason": "Position size = 0", "pnl": 0,
            })
            continue

        direction = result["direction"]
        entry_price = result["entry_price"]
        ev_gap = result["ev_gap"]

        # ── Outcome ─────────────────────────────
        won = (
            (direction == "UP" and w["winner"] == "UP")
            or (direction == "DOWN" and w["winner"] == "DOWN")
        )

        if won:
            shares = position / entry_price if entry_price > 0 else 0
            pnl = shares * (1.0 - entry_price)
            fee = position * config.SIM_FEE_MAX * 0.5
            pnl -= fee
            consecutive_losses = 0
        else:
            pnl = -position
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)

        capital += pnl
        peak_capital = max(peak_capital, capital)
        drawdown = (peak_capital - capital) / peak_capital if peak_capital > 0 else 0
        max_drawdown = max(max_drawdown, drawdown)

        trade = {
            "window_id": wid,
            "timestamp": datetime.utcfromtimestamp(w["timestamp"] / 1000).isoformat(),
            "btc_open": w["open_price"],
            "btc_close": w["close_price"],
            "btc_delta": round(w["delta"], 2),
            "direction": direction,
            "action": result["action"],
            "entry_price": round(entry_price, 4),
            "position": position,
            "ev_gap": round(ev_gap, 4),
            "model_prob": result.get("model_prob", 0),
            "market_prob": result.get("market_prob", 0),
            "confidence": result.get("confidence", 0),
            "outcome": "WIN" if won else "LOSS",
            "pnl": round(pnl, 2),
            "capital": round(capital, 2),
            "reason": result["reason"],
        }
        trades.append(trade)
        all_decisions.append(trade)

    # ── Stats ────────────────────────────────
    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    total = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = capital - initial_capital
    pnl_pct = total_pnl / initial_capital * 100

    avg_win = sum(t["pnl"] for t in trades if t["outcome"] == "WIN") / wins if wins > 0 else 0
    avg_loss = sum(t["pnl"] for t in trades if t["outcome"] == "LOSS") / losses if losses > 0 else 0

    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    no_trades = sum(1 for d in all_decisions if d.get("action") == "NO_TRADE")
    risk_blocks = sum(1 for d in all_decisions if d.get("action") == "RISK_BLOCK")

    avg_ev_gap = statistics.mean(ev_gaps_seen) if ev_gaps_seen else 0
    ev_gaps_positive = [g for g in ev_gaps_seen if g >= 0.10]

    return {
        "trades": trades,
        "all_decisions": all_decisions,
        "stats": {
            "initial_capital": initial_capital,
            "final_capital": round(capital, 2),
            "total_pnl": round(total_pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "total_windows": len(windows),
            "trades_entered": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown_pct": round(max_drawdown * 100, 2),
            "max_consecutive_losses": max_consecutive_losses,
            "no_trade_windows": no_trades,
            "risk_blocked": risk_blocks,
            "trade_rate_pct": round(total / len(windows) * 100, 1) if windows else 0,
            "avg_ev_gap_all": round(avg_ev_gap, 4),
            "windows_with_edge": len(ev_gaps_positive),
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Output & Reporting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def print_ev_results(results: dict):
    """Print EV Gap backtest results."""
    s = results["stats"]
    trades = results["trades"]
    pnl_color = C.GREEN if s["total_pnl"] >= 0 else C.RED

    print(f"\n{C.BOLD}{C.MAGENTA}{'═' * 72}{C.RESET}")
    print(f"{C.BOLD}{C.MAGENTA}  📊 EV Gap Strategy — Backtest Results{C.RESET}")
    print(f"{C.MAGENTA}{'═' * 72}{C.RESET}")

    print(f"\n  {C.BOLD}Performance{C.RESET}")
    print(f"  {'─' * 50}")
    print(f"  Starting Capital:    ${s['initial_capital']:,.2f}")
    print(f"  Final Capital:       ${s['final_capital']:,.2f}")
    print(
        f"  Total P&L:           {pnl_color}{C.BOLD}"
        f"${s['total_pnl']:+,.2f} ({s['pnl_pct']:+.2f}%){C.RESET}"
    )
    print(f"  Max Drawdown:        {s['max_drawdown_pct']:.2f}%")
    print(f"  Profit Factor:       {s['profit_factor']:.2f}")

    print(f"\n  {C.BOLD}EV Gap Stats{C.RESET}")
    print(f"  {'─' * 50}")
    print(f"  Avg EV Gap (all):    {s['avg_ev_gap_all']:.2%}")
    print(f"  Windows w/ Edge≥10%: {s['windows_with_edge']} / {s['total_windows']}")
    print(f"  Trade Rate:          {s['trade_rate_pct']:.1f}%")

    print(f"\n  {C.BOLD}Trade Statistics{C.RESET}")
    print(f"  {'─' * 50}")
    print(f"  Total Windows:       {s['total_windows']}")
    print(f"  Trades Entered:      {s['trades_entered']}")
    print(f"  No-Trade Windows:    {s['no_trade_windows']}")
    print(f"  Risk Blocked:        {s['risk_blocked']}")
    print(
        f"  Win / Loss:          "
        f"{C.GREEN}{s['wins']}W{C.RESET} / "
        f"{C.RED}{s['losses']}L{C.RESET}"
    )
    print(f"  Win Rate:            {C.BOLD}{s['win_rate']:.1f}%{C.RESET}")
    print(f"  Avg Win:             {C.GREEN}${s['avg_win']:+.2f}{C.RESET}")
    print(f"  Avg Loss:            {C.RED}${s['avg_loss']:+.2f}{C.RESET}")
    print(f"  Max Consec. Losses:  {s['max_consecutive_losses']}")

    # Trade log
    if trades:
        print(f"\n  {C.BOLD}Trade Log{C.RESET}")
        print(f"  {'─' * 80}")
        header = (
            f"  {'#':>3}  {'Time':>16}  {'Dir':>4}  {'EV Gap':>7}  "
            f"{'Model':>6}  {'Mkt':>5}  {'Entry':>6}  {'Size':>7}  "
            f"{'Result':>6}  {'P&L':>8}  {'Capital':>10}"
        )
        print(f"{C.DIM}{header}{C.RESET}")

        display_trades = trades
        truncated = False
        if len(trades) > 40:
            display_trades = trades[:30] + trades[-10:]
            truncated = True

        for i, t in enumerate(display_trades):
            outcome_color = C.GREEN if t["outcome"] == "WIN" else C.RED
            pnl_c = C.GREEN if t["pnl"] >= 0 else C.RED

            ts = t.get("timestamp", "")
            if len(ts) > 16:
                ts = ts[5:16]

            line = (
                f"  {t['window_id']:>3}  {ts:>16}  {t['direction']:>4}  "
                f"{t['ev_gap']:>6.1%}  "
                f"{t['model_prob']:>5.0%}  {t['market_prob']:>4.0%}  "
                f"${t['entry_price']:>.4f}  "
                f"${t['position']:>6.2f}  "
                f"{outcome_color}{t['outcome']:>6}{C.RESET}  "
                f"{pnl_c}${t['pnl']:>+7.2f}{C.RESET}  "
                f"${t['capital']:>9,.2f}"
            )
            print(line)

            if truncated and i == 29:
                print(f"  {C.DIM}  ... ({len(trades) - 40} trades omitted) ...{C.RESET}")

    # Equity curve
    if trades:
        print(f"\n  {C.BOLD}Equity Curve{C.RESET}")
        print(f"  {'─' * 68}")
        capitals = [s["initial_capital"]] + [t["capital"] for t in trades]
        min_cap = min(capitals)
        max_cap = max(capitals)
        cap_range = max_cap - min_cap if max_cap > min_cap else 1

        chart_width = 60
        num_points = min(len(capitals), chart_width)
        step = max(1, len(capitals) // num_points)
        sampled = [capitals[i] for i in range(0, len(capitals), step)]

        for y_level in range(5, -1, -1):
            threshold = min_cap + (cap_range * y_level / 5)
            line = f"  ${threshold:>9,.0f} │"
            for cap in sampled:
                if cap >= threshold:
                    line += "█"
                else:
                    line += " "
            print(line)
        print(f"  {'':>10} └{'─' * len(sampled)}")

    # EV Gap distribution
    all_decisions = results["all_decisions"]
    ev_gaps = [d.get("ev_gap", 0) for d in all_decisions if "ev_gap" in d]
    if ev_gaps:
        print(f"\n  {C.BOLD}EV Gap Distribution{C.RESET}")
        print(f"  {'─' * 50}")
        bins = [
            ("<0%", sum(1 for g in ev_gaps if g < 0)),
            ("0-5%", sum(1 for g in ev_gaps if 0 <= g < 0.05)),
            ("5-10%", sum(1 for g in ev_gaps if 0.05 <= g < 0.10)),
            ("10-15%", sum(1 for g in ev_gaps if 0.10 <= g < 0.15)),
            ("15-20%", sum(1 for g in ev_gaps if 0.15 <= g < 0.20)),
            ("20%+", sum(1 for g in ev_gaps if g >= 0.20)),
        ]
        max_count = max(b[1] for b in bins) if bins else 1
        for label, count in bins:
            bar_len = int(count / max_count * 30) if max_count > 0 else 0
            bar = "█" * bar_len
            marker = f" {C.GREEN}← TRADE ZONE{C.RESET}" if label in ("10-15%", "15-20%", "20%+") else ""
            print(f"  {label:>6}  {count:>3}  {bar}{marker}")

    print(f"\n{C.MAGENTA}{'═' * 72}{C.RESET}\n")


def save_results(results: dict, filepath: str = "ev_backtest_results.csv"):
    """Save trade-level results to CSV."""
    trades = results["trades"]
    if not trades:
        print("  ⚠️  No trades to save.")
        return

    fieldnames = list(trades[0].keys())
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(trades)
    print(f"  💾 Results saved to {filepath}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI Entry Point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(
        description="EV Gap Strategy Backtester — Polymarket 5-Min BTC"
    )
    parser.add_argument(
        "--candles", type=int, default=300,
        help="Number of 1-min candles to fetch (default: 300 = 5 hours)"
    )
    parser.add_argument(
        "--days", type=float, default=None,
        help="Days of data (overrides --candles)"
    )
    parser.add_argument(
        "--capital", type=float, default=100,
        help="Starting capital (default: $100)"
    )
    parser.add_argument(
        "--output", type=str, default="ev_backtest_results.csv",
        help="Output CSV filename"
    )
    parser.add_argument(
        "--min-gap", type=float, default=None,
        help="Override minimum EV gap (e.g., 0.08 for 8%%)"
    )
    args = parser.parse_args()

    total_candles = args.candles
    if args.days:
        total_candles = int(args.days * 24 * 60)

    if args.min_gap is not None:
        config.EV_MIN_GAP = args.min_gap

    print(f"\n{C.BOLD}{C.MAGENTA}{'═' * 72}{C.RESET}")
    print(f"{C.BOLD}{C.MAGENTA}  ⚡ EV Gap Backtester — Polymarket 5-Min BTC{C.RESET}")
    print(f"{C.MAGENTA}{'═' * 72}{C.RESET}\n")
    print(f"  Capital:     ${args.capital:,.2f}")
    print(f"  Candles:     {total_candles} (~{total_candles // 5} windows)")
    print(f"  Min EV Gap:  {config.EV_MIN_GAP:.0%}")
    print(f"  Position:    {config.EV_FLAT_POSITION_PCT:.0%} of capital per trade")
    print()

    # Step 1: Fetch data
    candles = fetch_pyth_klines(total_candles)
    if len(candles) < 10:
        print("  ❌ Not enough data.")
        sys.exit(1)

    # Step 2: Build windows
    windows = build_windows(candles)
    print(f"  Built {len(windows)} 5-minute windows")

    # Step 3: Run backtest
    print(f"\n  Running EV Gap strategy simulation...", end=" ", flush=True)
    t0 = time.time()
    results = run_ev_backtest(windows, args.capital)
    elapsed = time.time() - t0
    print(f"✓ ({elapsed:.1f}s)")

    # Step 4: Report
    print_ev_results(results)
    save_results(results, args.output)


if __name__ == "__main__":
    main()
