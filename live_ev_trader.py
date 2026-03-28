#!/usr/bin/env python3
"""
Live EV Gap Trader for Polymarket 5-Minute BTC

Architecture:
1. Computes dynamic slug: btc-updown-5m-{epoch_ts} for the current 5-min window.
2. Fetches market + token IDs from Gamma API using the slug.
3. Gets real market odds from Polymarket CLOB (midpoint prices).
4. Gets real BTC spot data from Binance REST API (last 5 1-min candles).
5. Computes EV gap: Model Probability (from spot) vs Real Market Odds.
6. If gap >= EV_MIN_GAP, executes a Fill-Or-Kill order via py-clob-client.
"""

import os
import sys
import time
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

import config
from ev_strategy import estimate_true_probability

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
except ImportError:
    print("❌ py-clob-client not found. Run: source .venv/bin/activate && python live_ev_trader.py")
    sys.exit(1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANSI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
    CYAN = "\033[96m"; MAGENTA = "\033[95m"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Setup & Auth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
load_dotenv()

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIG_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def init_clob_client() -> ClobClient:
    """Initialize and authenticate the Polymarket CLOB client."""
    if not PRIVATE_KEY:
        print(f"{C.RED}❌ Missing POLYMARKET_PRIVATE_KEY in .env{C.RESET}")
        sys.exit(1)

    print(f"  {C.CYAN}Connecting to Polymarket CLOB...{C.RESET}", end=" ", flush=True)
    try:
        client = ClobClient(
            HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
            signature_type=SIG_TYPE,
            funder=FUNDER if FUNDER else None,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        ok = client.get_ok()
        if ok != "OK":
            raise Exception(f"Server returned: {ok}")
        print(f"✓ {C.GREEN}Connected{C.RESET}")
        return client
    except Exception as e:
        print(f"\n  {C.RED}❌ Auth failed: {e}{C.RESET}")
        sys.exit(1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dynamic Market Discovery
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_current_window_slug() -> tuple:
    """
    Computes the slug for the CURRENT 5-minute window.
    Polymarket 5-min BTC markets use: btc-updown-5m-{epoch_seconds}
    where epoch_seconds is the start of the 5-minute window (floored to 300s boundary).
    Returns (slug, window_start_ts, seconds_remaining).
    """
    now = int(time.time())
    window_start = (now // 300) * 300          # Floor to 5-min boundary
    seconds_elapsed = now - window_start
    seconds_remaining = 300 - seconds_elapsed
    slug = f"btc-updown-5m-{window_start}"
    return slug, window_start, seconds_remaining


def get_active_btc_market(client: ClobClient) -> dict:
    """
    Finds the currently active 5-Minute BTC market using dynamic slug.
    Falls back to searching the Gamma API if the slug-based lookup fails.
    """
    slug, window_start, secs_left = compute_current_window_slug()
    window_dt = datetime.fromtimestamp(window_start, tz=timezone.utc)

    print(f"  {C.DIM}Window: {window_dt:%H:%M:%S} UTC  |  Slug: {slug}  |  {secs_left}s left{C.RESET}")

    # Strategy 1: Direct slug lookup via Gamma API
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()

        if markets and len(markets) > 0:
            mkt = markets[0]
            tokens_raw = mkt.get("clobTokenIds", [])
            if isinstance(tokens_raw, str):
                import json
                tokens = json.loads(tokens_raw)
            else:
                tokens = tokens_raw
                
            if len(tokens) >= 2:
                return {
                    "event_title": mkt.get("question", slug),
                    "market_id": mkt.get("id", ""),
                    "condition_id": mkt.get("conditionId", ""),
                    "yes_token": tokens[0],     # UP / YES token
                    "no_token": tokens[1],       # DOWN / NO token
                    "slug": slug,
                    "window_start": window_start,
                    "secs_remaining": secs_left,
                }
    except Exception as e:
        print(f"  {C.DIM}Slug lookup failed: {e}{C.RESET}")

    # Strategy 2: Search all active markets for BTC 5-min keywords
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 100, "active": "true", "closed": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        all_markets = resp.json()

        for mkt in all_markets:
            q = mkt.get("question", "").lower()
            if ("btc" in q or "bitcoin" in q) and ("5" in q) and ("min" in q or "minute" in q):
                tokens_raw = mkt.get("clobTokenIds", [])
                if isinstance(tokens_raw, str):
                    import json
                    tokens = json.loads(tokens_raw)
                else:
                    tokens = tokens_raw
                    
                if len(tokens) >= 2:
                    return {
                        "event_title": mkt.get("question", "BTC 5-Min"),
                        "market_id": mkt.get("id", ""),
                        "condition_id": mkt.get("conditionId", ""),
                        "yes_token": tokens[0],
                        "no_token": tokens[1],
                        "slug": mkt.get("slug", slug),
                        "window_start": window_start,
                        "secs_remaining": secs_left,
                    }
    except Exception as e:
        print(f"  {C.DIM}Fallback search failed: {e}{C.RESET}")

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Real-Time Data
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_real_market_prob(client: ClobClient, token_id: str) -> float:
    """Fetches the real midpoint price (probability) from Polymarket CLOB."""
    try:
        mid = client.get_midpoint(token_id)
        return float(mid) if mid else 0.50
    except Exception:
        return 0.50


def fetch_btc_candles() -> list:
    """
    Fetches the last 10 one-minute BTC/USDC candles from Binance.
    We fetch 10 to ensure we have sufficient overlap with the current
    5-min Polymarket window, including the live streaming minute.
    """
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDC", "interval": "1m", "limit": 10},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()

        if not raw:
            return None

        candles = []
        for c in raw:
            candles.append({
                "timestamp": c[0],
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
        return candles
    except Exception as e:
        print(f"  {C.RED}  Binance API error: {e}{C.RESET}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Order Execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def execute_trade(client: ClobClient, market: dict, direction: str,
                  ev_gap: float, position_usd: float):
    """Executes or simulates a Fill-Or-Kill market order."""
    print(f"\n{C.BOLD}{C.MAGENTA}{'━' * 55}{C.RESET}")
    print(f"  ⚡ {C.BOLD}{'[DRY RUN] ' if config.LIVE_DRY_RUN else ''}BUY {direction}{C.RESET}")
    print(f"  Market:  {market['event_title']}")
    print(f"  EV Gap:  {ev_gap:.1%}")
    print(f"  Size:    ${position_usd:.2f}")

    # Hard cap
    position_usd = min(position_usd, config.LIVE_TRADE_AMOUNT_USD)
    token_id = market["yes_token"] if direction == "UP" else market["no_token"]

    if config.LIVE_DRY_RUN:
        print(f"  {C.YELLOW}[DRY RUN] Would BUY ${position_usd:.2f} of {direction} token{C.RESET}")
        print(f"  {C.DIM}Token: {token_id[:20]}...{C.RESET}")
        print(f"{C.MAGENTA}{'━' * 55}{C.RESET}")
        return True

    # --- LIVE ORDER ---
    try:
        print(f"  {C.DIM}Submitting FOK order...{C.RESET}", flush=True)
        mo = MarketOrderArgs(
            token_id=token_id,
            amount=position_usd,
            side=BUY,
            order_type=OrderType.FOK,
        )
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        print(f"  {C.GREEN}✓ ORDER FILLED{C.RESET}")
        print(f"  {C.DIM}{resp}{C.RESET}")
        print(f"{C.MAGENTA}{'━' * 55}{C.RESET}")
        return True
    except Exception as e:
        print(f"  {C.RED}❌ Order failed: {e}{C.RESET}")
        print(f"{C.MAGENTA}{'━' * 55}{C.RESET}")
        return False


def calculate_actual_payout(trade: dict) -> float:
    """
    Queries Pyth to see what the actual BTC price was at window_start and window_end.
    Returns the PnL of this trade if the market finished.
    Returns None if still active.
    """
    now = time.time()
    if now < trade["window_end"]:
        return None  # Still active

    try:
        url = f"https://benchmarks.pyth.network/v1/shims/tradingview/history?symbol=Crypto.BTC%2FUSD&resolution=1&from={trade['window_start']}&to={trade['window_end']}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("s") != "ok":
            return None
        
        oracle_open = float(data["o"][0])
        oracle_close = float(data["c"][-1])
        
        won = False
        if trade["direction"] == "UP" and oracle_close > oracle_open:
            won = True
        elif trade["direction"] == "DOWN" and oracle_close < oracle_open:
            won = True
            
        if won:
            # Polymarket YES/NO pays out $1.00 per share.
            shares = trade["size"] / trade["price"]
            return (shares * 1.0) - trade["size"]
        else:
            return -trade["size"]
    except Exception:
        return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main Loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    print(f"\n{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  🚀 Polymarket 5-Min BTC — LIVE EV GAP TRADER{C.RESET}")
    print(f"{C.CYAN}{'═' * 60}{C.RESET}\n")

    mode_str = f"{C.YELLOW}DRY RUN{C.RESET}" if config.LIVE_DRY_RUN else f"{C.RED}LIVE{C.RESET}"
    print(f"  Mode:          {mode_str}")
    print(f"  Min EV Gap:    {config.EV_MIN_GAP:.0%}")
    print(f"  Max Market:    {config.EV_MAX_MARKET_PROB:.0%}")
    print(f"  Trade Amount:  ${config.LIVE_TRADE_AMOUNT_USD:.2f} per trade\n")

    client = init_clob_client()

    # Track state
    last_window = None
    traded_this_window = False
    total_trades = 0
    successful_trades = []
    
    # Risk State
    settled_pnl = 0.0
    consecutive_losses = 0
    cooldown_until = 0

    print(f"\n  {C.BOLD}Monitoring 5-minute windows...{C.RESET} (Ctrl+C to stop)\n")

    try:
        while True:
            now_utc = datetime.now(timezone.utc)
            now_ts = time.time()

            # ── 1. Live Risk Management & Settlement Tracking ──
            for t in successful_trades:
                if not t.get("settled"):
                    payout = calculate_actual_payout(t)
                    if payout is not None:
                        t["settled"] = True
                        t["payout"] = payout
                        settled_pnl += payout
                        if payout < 0:
                            consecutive_losses += 1
                            print(f"\\n  {C.RED}Trade Settled: LOSS (${abs(payout):.2f}) | Consec: {consecutive_losses}{C.RESET}")
                        else:
                            consecutive_losses = 0
                            print(f"\\n  {C.GREEN}Trade Settled: WIN (+${payout:.2f}) | Consec: 0{C.RESET}")

            # Enforce constraints
            if consecutive_losses >= getattr(config, "MAX_CONSECUTIVE_LOSSES", 3):
                cd = getattr(config, "COOLDOWN_SECONDS", 1800)
                print(f"\\n  {C.RED}🛑 RISK LIMIT: {consecutive_losses} consecutive losses. Activating {cd//60}m cooldown.{C.RESET}")
                cooldown_until = now_ts + cd
                consecutive_losses = 0  # Reset so it doesn't loop infinitely
                
            if now_ts < cooldown_until:
                print(f"  {C.YELLOW}⏳ Cooldown active. {int(cooldown_until - now_ts)}s remaining...{C.RESET}")
                time.sleep(15)
                continue
                
            max_trades = getattr(config, "MAX_TRADES_PER_DAY", 15)
            if len(successful_trades) >= max_trades:
                print(f"\\n  {C.YELLOW}🛑 RISK LIMIT: Daily max trades ({max_trades}) reached. Bot shutting down.{C.RESET}\\n")
                break # Exit loop gracefully

            # ── 2. Discover current market ──
            market = get_active_btc_market(client)

            if not market:
                print(f"  {C.YELLOW}⏳ No active BTC 5-Min market. Waiting 15s...{C.RESET}")
                time.sleep(15)
                continue

            # Reset trade flag on new window
            if market["window_start"] != last_window:
                last_window = market["window_start"]
                traded_this_window = False
                print(f"\n  {C.CYAN}{'─' * 50}{C.RESET}")
                print(f"  {C.CYAN}📊 New Window: {market['event_title']}{C.RESET}")

            if traded_this_window:
                # Wait for this window to end
                wait = max(market["secs_remaining"], 5)
                print(f"  {C.DIM}Already traded this window. Sleeping {wait}s...{C.RESET}")
                time.sleep(wait)
                continue

            # 2. Only trade in the sweet spot: 60-180 seconds into the window
            elapsed = 300 - market["secs_remaining"]
            if elapsed < 60:
                wait = 60 - elapsed
                print(f"  {C.DIM}Too early ({elapsed}s in). Waiting {wait}s for data...{C.RESET}")
                time.sleep(wait)
                continue
            if market["secs_remaining"] < 30:
                print(f"  {C.DIM}Too late ({market['secs_remaining']}s left). Skipping window.{C.RESET}")
                traded_this_window = True
                continue

            # 3. Fetch real data
            raw_candles = fetch_btc_candles()
            if not raw_candles:
                print(f"  {C.RED}Failed to get BTC candles. Retrying in 10s.{C.RESET}")
                time.sleep(10)
                continue
                
            # Filter candles precisely to the Polymarket window bounds!
            target_ts_ms = market["window_start"] * 1000
            window_candles = [c for c in raw_candles if c["timestamp"] >= target_ts_ms]
            
            if not window_candles:
                print(f"  {C.DIM}Waiting for exact window open candle...{C.RESET}")
                time.sleep(5)
                continue

            # Use perfectly anchored first candle open as exact oracle price
            oracle_open = window_candles[0]["open"]
            spot_price = window_candles[-1]["close"]
            spot_delta = spot_price - oracle_open

            real_prob_up = fetch_real_market_prob(client, market["yes_token"])
            real_prob_down = fetch_real_market_prob(client, market["no_token"])

            # 4. Compute model probability using ev_strategy (needs recent history)
            model = estimate_true_probability(oracle_open, raw_candles)
            model_prob_up = model["prob_up"]
            model_prob_down = model["prob_down"]

            # 5. EV Gap
            ev_gap_up = model_prob_up - real_prob_up
            ev_gap_down = model_prob_down - real_prob_down

            print(f"  BTC: ${spot_price:,.0f}  Δ={spot_delta:+.0f}  Dir={model['direction']}")
            print(f"    UP   │ Model: {model_prob_up:.0%} │ Book: {real_prob_up:.0%} │ "
                  f"Gap: {C.GREEN if ev_gap_up >= config.EV_MIN_GAP else C.DIM}{ev_gap_up:+.1%}{C.RESET}")
            print(f"    DOWN │ Model: {model_prob_down:.0%} │ Book: {real_prob_down:.0%} │ "
                  f"Gap: {C.GREEN if ev_gap_down >= config.EV_MIN_GAP else C.DIM}{ev_gap_down:+.1%}{C.RESET}")

            # 6. Decision
            pos_usd = config.LIVE_TRADE_AMOUNT_USD

            if ev_gap_up >= config.EV_MIN_GAP and real_prob_up <= config.EV_MAX_MARKET_PROB:
                if execute_trade(client, market, "UP", ev_gap_up, pos_usd):
                    successful_trades.append({
                        "window_start": market["window_start"],
                        "window_end": market["window_start"] + 300,
                        "direction": "UP",
                        "size": pos_usd,
                        "price": real_prob_up
                    })
                traded_this_window = True
                total_trades += 1
            elif ev_gap_down >= config.EV_MIN_GAP and real_prob_down <= config.EV_MAX_MARKET_PROB:
                if execute_trade(client, market, "DOWN", ev_gap_down, pos_usd):
                    successful_trades.append({
                        "window_start": market["window_start"],
                        "window_end": market["window_start"] + 300,
                        "direction": "DOWN",
                        "size": pos_usd,
                        "price": real_prob_down
                    })
                traded_this_window = True
                total_trades += 1
            else:
                print(f"  {C.DIM}No edge. Rechecking in 15s...{C.RESET}")

            time.sleep(15)

    except KeyboardInterrupt:
        print(f"\n\n{C.YELLOW}{'═' * 45}{C.RESET}")
        print(f"  {C.YELLOW}Bot stopped. Total trades executed: {len(successful_trades)}{C.RESET}")
        
        if successful_trades:
            print(f"  {C.CYAN}Fetching settlements from Pyth Oracle...{C.RESET}")
            session_pnl = 0.0
            pending = 0
            for t in successful_trades:
                payout = calculate_actual_payout(t)
                if payout is None:
                    pending += 1
                else:
                    session_pnl += payout
            
            color = C.GREEN if session_pnl > 0 else C.RED if session_pnl < 0 else C.BOLD
            print(f"  {color}Session Settled PnL: ${session_pnl:+.2f}{C.RESET}")
            if pending > 0:
                print(f"  {C.DIM}({pending} trades actively awaiting window close){C.RESET}")
                
        print(f"{C.YELLOW}{'═' * 45}{C.RESET}\n")


if __name__ == "__main__":
    main()
