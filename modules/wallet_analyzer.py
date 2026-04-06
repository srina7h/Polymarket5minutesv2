"""
Confirmation Sniper — Wallet Analyzer

Offline analysis tool for studying Polymarket wallet trading patterns.
Queries the Data API for trade history and correlates with BTC price data.

Usage:
    python -m modules.wallet_analyzer --address 0x... --days 7
    python -m modules.wallet_analyzer --username rwo --days 30
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

import requests

import config

logger = logging.getLogger("WalletAnalyzer")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data API Queries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_wallet_trades(address: str, days: int = 30, limit: int = 500) -> list:
    """Fetch trades from Polymarket Data API."""
    end_ts = int(time.time())
    start_ts = end_ts - (days * 86400)

    all_trades = []
    offset = 0
    batch = min(limit, 100)

    print(f"  Fetching trades for {address[:10]}...{address[-6:]} (last {days} days)")

    while offset < limit:
        try:
            resp = requests.get(
                f"{config.DATA_API}/trades",
                params={
                    "user": address,
                    "limit": batch,
                    "offset": offset,
                },
                timeout=15,
            )
            resp.raise_for_status()
            trades = resp.json()

            if not trades:
                break

            all_trades.extend(trades)
            offset += len(trades)

            if len(trades) < batch:
                break

            time.sleep(0.3)  # Rate limiting

        except Exception as e:
            print(f"  ❌ API error: {e}")
            break

    print(f"  ✓ Fetched {len(all_trades)} trades")
    return all_trades


def fetch_wallet_activity(address: str, limit: int = 100) -> list:
    """Fetch activity (trades, redeems, etc.) from Data API."""
    try:
        resp = requests.get(
            f"{config.DATA_API}/activity",
            params={"user": address, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  ❌ Activity fetch error: {e}")
        return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BTC Price at Timestamp (Pyth)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_btc_price_at(timestamp: int) -> float | None:
    """Get BTC price at a specific unix timestamp from Pyth oracle."""
    try:
        from_ts = timestamp - 60
        to_ts = timestamp + 60
        url = (
            f"https://benchmarks.pyth.network/v1/shims/tradingview/history"
            f"?symbol=Crypto.BTC%2FUSD&resolution=1&from={from_ts}&to={to_ts}"
        )
        resp = requests.get(url, timeout=10)
        data = resp.json()

        if data.get("s") == "ok" and data.get("c"):
            # Find the closest candle
            times = data["t"]
            closes = data["c"]
            closest_idx = min(range(len(times)), key=lambda i: abs(times[i] - timestamp))
            return float(closes[closest_idx])
    except Exception:
        pass
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Analysis Functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def classify_market_condition(btc_delta: float, vol_pct: float) -> str:
    """Classify BTC market condition at time of trade."""
    abs_delta = abs(btc_delta)
    if abs_delta < 20:
        return "consolidation"
    elif abs_delta >= 100:
        return "breakout"
    elif vol_pct > 0.005:
        return "momentum"
    else:
        return "reversal"


def analyze_trades(trades: list) -> dict:
    """Perform comprehensive trade analysis."""
    if not trades:
        return {"error": "No trades to analyze"}

    # Filter for BTC 5-min markets
    btc_trades = []
    for t in trades:
        market = t.get("market", "") or t.get("conditionId", "")
        title = t.get("title", "") or t.get("question", "")
        title_lower = title.lower() if title else ""

        if "btc" in title_lower or "bitcoin" in title_lower:
            btc_trades.append(t)

    total_trades = len(trades)
    btc_count = len(btc_trades)

    # Basic stats
    sides = {}
    prices = []
    sizes = []
    timestamps = []

    for t in trades:
        side = t.get("side", "unknown")
        sides[side] = sides.get(side, 0) + 1

        price = float(t.get("price", 0))
        if price > 0:
            prices.append(price)

        size = float(t.get("size", 0))
        if size > 0:
            sizes.append(size)

        ts = t.get("timestamp", t.get("createdAt", ""))
        if ts:
            timestamps.append(ts)

    # Price distribution
    price_buckets = {
        "0-30¢": len([p for p in prices if p < 0.30]),
        "30-50¢": len([p for p in prices if 0.30 <= p < 0.50]),
        "50-70¢": len([p for p in prices if 0.50 <= p < 0.70]),
        "70-90¢": len([p for p in prices if 0.70 <= p < 0.90]),
        "90¢+": len([p for p in prices if p >= 0.90]),
    }

    # Trade timing patterns
    hours = []
    for ts in timestamps:
        try:
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                continue
            hours.append(dt.hour)
        except Exception:
            pass

    hour_dist = {}
    for h in hours:
        hour_dist[h] = hour_dist.get(h, 0) + 1

    return {
        "total_trades": total_trades,
        "btc_5min_trades": btc_count,
        "sides": sides,
        "avg_entry_price": round(sum(prices) / len(prices), 4) if prices else 0,
        "median_entry_price": round(sorted(prices)[len(prices) // 2], 4) if prices else 0,
        "avg_size_usd": round(sum(sizes) / len(sizes), 2) if sizes else 0,
        "total_volume": round(sum(s * p for s, p in zip(sizes, prices)), 2) if sizes and prices else 0,
        "price_distribution": price_buckets,
        "peak_trading_hours_utc": dict(sorted(hour_dist.items(), key=lambda x: -x[1])[:5]),
        "trades_per_day": round(total_trades / max(1, len(set(hours))), 1),
    }


def extract_patterns(analysis: dict) -> list:
    """Extract actionable trading patterns from analysis."""
    patterns = []

    if analysis.get("avg_entry_price", 0) > 0.60:
        patterns.append({
            "pattern": "HIGH_CONVICTION_ENTRY",
            "description": "Avg entry above 60¢ — trader waits for confirmation",
            "implication": "Enter only when direction is established, not predictively",
        })

    price_dist = analysis.get("price_distribution", {})
    high_price = price_dist.get("70-90¢", 0) + price_dist.get("90¢+", 0)
    low_price = price_dist.get("0-30¢", 0) + price_dist.get("30-50¢", 0)
    if high_price > low_price:
        patterns.append({
            "pattern": "CONFIRMATION_BIAS",
            "description": "More trades at high prices than low — confirmation strategy",
            "implication": "Priority on certainty over potential return",
        })

    if analysis.get("btc_5min_trades", 0) > analysis.get("total_trades", 1) * 0.5:
        patterns.append({
            "pattern": "BTC_5MIN_SPECIALIST",
            "description": "Majority of trades are BTC 5-min markets",
            "implication": "Focused strategy, not diversified",
        })

    return patterns


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(description="Polymarket Wallet Analyzer")
    parser.add_argument("--address", type=str, help="Polygon wallet address")
    parser.add_argument("--days", type=int, default=30, help="Days of history (default: 30)")
    parser.add_argument("--output", type=str, default="wallet_analysis.json", help="Output file")
    args = parser.parse_args()

    if not args.address:
        print("❌ Please provide --address")
        sys.exit(1)

    print(f"\n{'═' * 60}")
    print(f"  🔍 Polymarket Wallet Analyzer")
    print(f"{'═' * 60}\n")

    # Fetch data
    trades = fetch_wallet_trades(args.address, args.days)
    activity = fetch_wallet_activity(args.address)

    # Analyze
    analysis = analyze_trades(trades)
    patterns = extract_patterns(analysis)

    # Print results
    print(f"\n{'─' * 50}")
    print(f"  📊 Analysis Results")
    print(f"{'─' * 50}")
    print(f"  Total Trades:      {analysis.get('total_trades', 0)}")
    print(f"  BTC 5-Min Trades:  {analysis.get('btc_5min_trades', 0)}")
    print(f"  Avg Entry Price:   {analysis.get('avg_entry_price', 0):.2f}")
    print(f"  Median Entry:      {analysis.get('median_entry_price', 0):.2f}")
    print(f"  Avg Size (USD):    ${analysis.get('avg_size_usd', 0):.2f}")
    print(f"  Total Volume:      ${analysis.get('total_volume', 0):,.2f}")

    print(f"\n  Price Distribution:")
    for bucket, count in analysis.get("price_distribution", {}).items():
        bar = "█" * min(count, 40)
        print(f"    {bucket:>8}  {count:>4}  {bar}")

    print(f"\n  Patterns Detected:")
    for p in patterns:
        print(f"    ✦ {p['pattern']}: {p['description']}")

    # Save
    output = {
        "address": args.address,
        "days": args.days,
        "analysis": analysis,
        "patterns": patterns,
        "raw_trade_count": len(trades),
        "activity_count": len(activity),
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  💾 Saved to {args.output}")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    main()
