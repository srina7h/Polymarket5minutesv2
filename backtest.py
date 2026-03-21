#!/usr/bin/env python3
"""
LEMA Trading Bot — Backtester
Fetches historical BTC 1-min candles from Binance and replays the LEMA
strategy across hundreds of 5-minute windows.

Usage:
    python3 backtest.py                        # Default: 1000 candles (~200 windows)
    python3 backtest.py --candles 3000         # ~600 windows (~50 hours)
    python3 backtest.py --days 3               # 3 days of data
    python3 backtest.py --capital 5000         # Custom starting capital
"""

import argparse
import csv
import os
import statistics
import sys
import time
from datetime import datetime, timedelta

import requests

# Reuse config and edge calculator from the main bot
import config
from edge_calculator import EdgeCalculator


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Fetching
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_binance_klines(total_candles: int = 1000) -> list:
    """
    Fetch 1-minute BTC/USDT candles from Binance REST API.
    Chains requests in batches of 1000 to support arbitrary length.
    Returns list of dicts with: timestamp, open, high, low, close, volume.
    """
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []
    end_time = None
    remaining = total_candles

    print(f"  Fetching {total_candles} 1-min BTC candles from Binance...", end=" ", flush=True)

    while remaining > 0:
        batch_size = min(remaining, 1000)
        params = {
            "symbol": "BTCUSDT",
            "interval": "1m",
            "limit": batch_size,
        }
        if end_time:
            params["endTime"] = end_time - 1

        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"\n  ❌ Binance API error: {e}")
            break

        if not data:
            break

        candles = []
        for d in data:
            candles.append({
                "timestamp": d[0],  # open time in ms
                "open": float(d[1]),
                "high": float(d[2]),
                "low": float(d[3]),
                "close": float(d[4]),
                "volume": float(d[5]),
            })

        all_candles = candles + all_candles  # prepend (oldest first)
        end_time = data[0][0]  # earliest timestamp in this batch
        remaining -= len(data)

        if len(data) < batch_size:
            break  # No more data

        time.sleep(0.2)  # Rate limit courtesy

    print(f"✓ {len(all_candles)} candles")
    if all_candles:
        start_dt = datetime.utcfromtimestamp(all_candles[0]["timestamp"] / 1000)
        end_dt = datetime.utcfromtimestamp(all_candles[-1]["timestamp"] / 1000)
        hours = (end_dt - start_dt).total_seconds() / 3600
        print(f"  Range: {start_dt.strftime('%Y-%m-%d %H:%M')} → {end_dt.strftime('%Y-%m-%d %H:%M')} UTC ({hours:.1f} hours)")

    return all_candles


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Window Builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_windows(candles: list) -> list:
    """
    Group 1-min candles into 5-minute windows.
    Each window has: window_id, candles (5 entries), open_price, close_price, winner.
    """
    windows = []
    for i in range(0, len(candles) - 4, 5):
        window_candles = candles[i:i + 5]
        if len(window_candles) < 5:
            break

        open_price = window_candles[0]["open"]
        close_price = window_candles[-1]["close"]
        high = max(c["high"] for c in window_candles)
        low = min(c["low"] for c in window_candles)
        volume = sum(c["volume"] for c in window_candles)
        delta = close_price - open_price
        winner = "UP" if delta >= 0 else "DOWN"

        windows.append({
            "window_id": len(windows) + 1,
            "timestamp": window_candles[0]["timestamp"],
            "candles": window_candles,
            "open_price": open_price,
            "close_price": close_price,
            "high": high,
            "low": low,
            "volume": volume,
            "delta": delta,
            "winner": winner,
        })

    return windows


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LEMA Strategy Simulation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def simulate_lema(window: dict, edge_calc: EdgeCalculator) -> dict:
    """
    Simulate the LEMA strategy for one 5-minute window using candle data.

    Key simulation insight: The LEMA edge comes from the market probability
    LAGGING behind the real spot price. We simulate this by:
      - Real spot: evaluated at candle 4 close (T+240s)
      - Market odds: based on candle 2 close (T+120s) — simulating ~2min lag
    This creates the "informational gap" that LEMA exploits.
    """
    candles = window["candles"]
    oracle_open = window["open_price"]

    # Build simulated price history from candle data
    price_history = []
    base_ts = candles[0]["timestamp"] / 1000

    for idx, c in enumerate(candles):
        t0 = base_ts + (idx * 60)
        # Approximate sub-candle price path: open → high/low → close
        prices = [c["open"], c["high"], c["low"], c["close"]]
        for j, p in enumerate(prices):
            price_history.append((t0 + j * 15, p))

        # Add more intermediate points for smoother momentum calc
        for sec in range(4, 60, 10):
            interp = c["open"] + (c["close"] - c["open"]) * (sec / 60)
            price_history.append((t0 + sec, interp))

    price_history.sort(key=lambda x: x[0])

    # Evaluate at T+210s (3.5 min mark)
    eval_time = base_ts + 210

    # Real spot price at evaluation (candle 4 close, ~T+240s)
    if len(candles) >= 4:
        spot_at_eval = candles[3]["close"]
    else:
        spot_at_eval = candles[-1]["close"]

    spot_delta = spot_at_eval - oracle_open

    # ── Check entry criteria ──────────────

    # Criterion 1: Spot delta magnitude
    if abs(spot_delta) < config.MIN_SPOT_DELTA_USD:
        return {
            "action": "NO_TRADE",
            "reason": f"Delta ${spot_delta:+.2f} < ${config.MIN_SPOT_DELTA_USD}",
            "edge_score": 0,
        }

    # Criterion 2: Direction consistency
    # Use cumulative movement over intervals, not per-candle open/close
    # This better matches the live bot's sub-second price tracking
    cum_deltas = []
    for i, c in enumerate(candles[:4]):
        cum_deltas.append(c["close"] - oracle_open)

    # Check if cumulative movement has been trending in one direction
    if len(cum_deltas) >= 3:
        positive_moves = sum(1 for d in cum_deltas if d > 0)
        negative_moves = sum(1 for d in cum_deltas if d < 0)
        dominant = max(positive_moves, negative_moves)
        total_intervals = len(cum_deltas)
    else:
        dominant = 0
        total_intervals = 0

    if dominant < config.MIN_DIRECTION_CONSISTENCY:
        return {
            "action": "NO_TRADE",
            "reason": f"Direction inconsistent: {positive_moves}↑/{negative_moves}↓ (need ≥{config.MIN_DIRECTION_CONSISTENCY})",
            "edge_score": 0,
        }

    # Criterion 3: Volatility filter
    candle_returns = []
    for c in candles[:4]:
        if c["open"] > 0:
            candle_returns.append(abs(c["close"] - c["open"]) / c["open"])
    if len(candle_returns) >= 2:
        vol = statistics.stdev(candle_returns)
        avg_return = statistics.mean(candle_returns)
        if avg_return > 0 and vol / avg_return > config.MAX_VOLATILITY_MULTIPLIER:
            return {
                "action": "NO_TRADE",
                "reason": f"Volatility too high: {vol/avg_return:.1f}×",
                "edge_score": 0,
            }

    # ── Compute Edge Score ──────────────

    # CRITICAL: Simulate market probability LAG
    # Market odds are based on an OLDER price (candle 2 close, ~T+120s)
    # while the real spot has moved further (candle 4 close, ~T+240s)
    lagged_price = candles[1]["close"]  # 2-minute old price
    lagged_delta = lagged_price - oracle_open
    sim_up_odds = 0.50 + (lagged_delta * config.SIM_ODDS_SENSITIVITY)
    sim_up_odds = max(0.02, min(0.98, sim_up_odds))

    # Book imbalance based on current spot (informed traders)
    norm = max(-1.0, min(1.0, spot_delta / config.PRICE_NORMALIZATION_USD))
    bid_vol = 50_000 * (1 + norm * 0.5)
    ask_vol = 50_000 * (1 - norm * 0.5)
    book_imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)

    # Filter history to before eval time
    history_at_eval = [(t, p) for t, p in price_history if t <= eval_time]

    edge = edge_calc.calculate(
        spot_price=spot_at_eval,
        oracle_open=oracle_open,
        market_probability=sim_up_odds,
        price_history=history_at_eval,
        book_imbalance=book_imbalance,
    )

    # Criterion 4: Minimum edge
    if edge["edge_score"] < config.MIN_EDGE_THRESHOLD:
        return {
            "action": "NO_TRADE",
            "reason": f"Edge {edge['edge_score']:.1%} < {config.MIN_EDGE_THRESHOLD:.0%}",
            "edge_score": edge["edge_score"],
            "sub_scores": edge["sub_scores"],
        }

    # ── TRADE ──────────────────────────
    direction = edge["direction"]
    spread = config.SIM_SPREAD
    if direction == "UP":
        entry_price = sim_up_odds + spread / 2
    else:
        entry_price = (1 - sim_up_odds) + spread / 2

    entry_price = max(0.05, min(0.95, entry_price))

    return {
        "action": f"BUY_{direction}",
        "direction": direction,
        "entry_price": entry_price,
        "edge_score": edge["edge_score"],
        "confidence": edge["confidence"],
        "estimated_prob": edge["estimated_prob"],
        "market_prob": sim_up_odds,
        "sub_scores": edge["sub_scores"],
        "spot_delta": spot_delta,
        "reason": f"{direction} Edge={edge['edge_score']:.1%} Δ=${spot_delta:+.0f}",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Position Sizing (reused from strategy.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def calculate_position_size(
    capital: float, edge_score: float, estimated_prob: float, entry_price: float
) -> float:
    if entry_price <= 0 or entry_price >= 1:
        return 0.0

    effective_p = min(0.50 + edge_score, 0.95)
    effective_p = max(effective_p, estimated_prob)

    profit_per_share = 1.0 - entry_price
    b = profit_per_share / entry_price
    p = effective_p
    q = 1.0 - p

    if b <= 0:
        return 0.0

    kelly_full = (b * p - q) / b
    kelly_fraction = max(0, kelly_full * config.KELLY_FRACTION)

    max_position = capital * config.MAX_POSITION_PCT
    position = min(kelly_fraction * capital, max_position)

    if edge_score < config.HIGH_CONVICTION_THRESHOLD:
        position *= 0.5

    if edge_score >= config.MIN_EDGE_THRESHOLD and position < 10.0:
        position = min(max_position * 0.25, capital * 0.005)

    return round(max(0, position), 2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Backtest Runner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_backtest(windows: list, initial_capital: float) -> dict:
    """Run the LEMA strategy across all windows and return results."""

    edge_calc = EdgeCalculator()
    capital = initial_capital
    trades = []
    all_decisions = []
    consecutive_losses = 0
    max_consecutive_losses = 0
    peak_capital = capital
    max_drawdown = 0
    cooldown_until_window = 0

    for w in windows:
        wid = w["window_id"]

        # Risk check: cooldown
        if wid < cooldown_until_window:
            all_decisions.append({
                "window_id": wid, "action": "RISK_BLOCK",
                "reason": "Cooldown", "pnl": 0,
            })
            continue

        # Risk check: daily equivalent (every 100 windows ~ 8 hours)
        # Simplified: just check consecutive losses
        if consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            cooldown_until_window = wid + 6  # Skip 6 windows (30 min)
            consecutive_losses = 0
            all_decisions.append({
                "window_id": wid, "action": "RISK_BLOCK",
                "reason": f"{config.MAX_CONSECUTIVE_LOSSES} consecutive losses → cooldown",
                "pnl": 0,
            })
            continue

        # Run strategy
        result = simulate_lema(w, edge_calc)

        if result["action"] == "NO_TRADE":
            all_decisions.append({
                "window_id": wid,
                "action": "NO_TRADE",
                "reason": result["reason"],
                "edge_score": result.get("edge_score", 0),
                "pnl": 0,
            })
            continue

        # Calculate position
        direction = result["direction"]
        entry_price = result["entry_price"]
        position = calculate_position_size(
            capital, result["edge_score"],
            result.get("estimated_prob", 0.6), entry_price
        )

        if position <= 0:
            all_decisions.append({
                "window_id": wid, "action": "NO_TRADE",
                "reason": "Position size = 0", "pnl": 0,
            })
            continue

        # Determine outcome
        won = (
            (direction == "UP" and w["winner"] == "UP")
            or (direction == "DOWN" and w["winner"] == "DOWN")
        )

        if won:
            shares = position / entry_price
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
            "btc_delta": w["delta"],
            "direction": direction,
            "action": result["action"],
            "entry_price": round(entry_price, 4),
            "position": position,
            "edge_score": round(result["edge_score"], 4),
            "confidence": result.get("confidence", ""),
            "outcome": "WIN" if won else "LOSS",
            "pnl": round(pnl, 2),
            "capital": round(capital, 2),
            "reason": result["reason"],
        }
        trades.append(trade)
        all_decisions.append(trade)

    # Compute stats
    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    total = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = capital - initial_capital
    pnl_pct = total_pnl / initial_capital * 100

    avg_win = 0
    avg_loss = 0
    if wins > 0:
        avg_win = sum(t["pnl"] for t in trades if t["outcome"] == "WIN") / wins
    if losses > 0:
        avg_loss = sum(t["pnl"] for t in trades if t["outcome"] == "LOSS") / losses

    # Profit factor
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    no_trades = sum(1 for d in all_decisions if d.get("action") == "NO_TRADE")
    risk_blocks = sum(1 for d in all_decisions if d.get("action") == "RISK_BLOCK")

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
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Output & Reporting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def print_results(results: dict):
    """Print backtest results to terminal."""
    s = results["stats"]
    trades = results["trades"]
    pnl_color = C.GREEN if s["total_pnl"] >= 0 else C.RED

    print(f"\n{C.BOLD}{C.CYAN}{'═' * 72}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  📊 LEMA Backtest Results{C.RESET}")
    print(f"{C.CYAN}{'═' * 72}{C.RESET}")

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

    print(f"\n  {C.BOLD}Trade Statistics{C.RESET}")
    print(f"  {'─' * 50}")
    print(f"  Total Windows:       {s['total_windows']}")
    print(f"  Trades Entered:      {s['trades_entered']} ({s['trade_rate_pct']:.1f}% of windows)")
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

    # Print trade-by-trade table (show first 30 and last 10)
    if trades:
        print(f"\n  {C.BOLD}Trade Log{C.RESET}")
        print(f"  {'─' * 68}")
        header = (
            f"  {'#':>3}  {'Time':>16}  {'Dir':>4}  {'Edge':>6}  "
            f"{'Entry':>6}  {'Size':>7}  {'Result':>6}  {'P&L':>8}  {'Capital':>10}"
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
                ts = ts[5:16]  # MM-DD HH:MM

            line = (
                f"  {t['window_id']:>3}  {ts:>16}  {t['direction']:>4}  "
                f"{t['edge_score']:>5.1%}  ${t['entry_price']:>.4f}  "
                f"${t['position']:>6.2f}  "
                f"{outcome_color}{t['outcome']:>6}{C.RESET}  "
                f"{pnl_c}${t['pnl']:>+7.2f}{C.RESET}  "
                f"${t['capital']:>9,.2f}"
            )
            print(line)

            if truncated and i == 29:
                print(f"  {C.DIM}  ... ({len(trades) - 40} trades omitted) ...{C.RESET}")

    # P&L curve (simple ASCII)
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

    print(f"\n{C.CYAN}{'═' * 72}{C.RESET}\n")


def save_results(results: dict, filepath: str = "backtest_results.csv"):
    """Save trade-level results to CSV."""
    trades = results["trades"]
    if not trades:
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
        description="LEMA Backtester — Historical BTC 5-Min Windows"
    )
    parser.add_argument(
        "--candles", type=int, default=1000,
        help="Number of 1-min candles to fetch (default: 1000 = ~200 windows)"
    )
    parser.add_argument(
        "--days", type=float, default=None,
        help="Days of data to fetch (overrides --candles)"
    )
    parser.add_argument(
        "--capital", type=float, default=10000,
        help="Starting capital (default: $10,000)"
    )
    parser.add_argument(
        "--output", type=str, default="backtest_results.csv",
        help="Output CSV filename"
    )
    args = parser.parse_args()

    total_candles = args.candles
    if args.days:
        total_candles = int(args.days * 24 * 60)  # 1440 candles per day

    print(f"\n{C.BOLD}{C.CYAN}{'═' * 72}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  ⚡ LEMA Backtester — Polymarket 5-Min BTC{C.RESET}")
    print(f"{C.CYAN}{'═' * 72}{C.RESET}\n")
    print(f"  Capital:   ${args.capital:,.2f}")
    print(f"  Candles:   {total_candles} (~{total_candles // 5} windows)")
    print(f"  Min Edge:  {config.MIN_EDGE_THRESHOLD:.0%}")
    print()

    # Step 1: Fetch data
    candles = fetch_binance_klines(total_candles)
    if len(candles) < 10:
        print("  ❌ Not enough data to backtest.")
        sys.exit(1)

    # Step 2: Build windows
    windows = build_windows(candles)
    print(f"  Built {len(windows)} 5-minute windows")

    # Step 3: Run backtest
    print(f"\n  Running LEMA strategy simulation...", end=" ", flush=True)
    t0 = time.time()
    results = run_backtest(windows, args.capital)
    elapsed = time.time() - t0
    print(f"✓ ({elapsed:.1f}s)")

    # Step 4: Report
    print_results(results)
    save_results(results, args.output)


if __name__ == "__main__":
    main()
