#!/usr/bin/env python3
"""
LEMA Trading Bot — Live Trader
Connects to real Polymarket 5-min BTC markets using:
  - Chainlink oracle (on-chain via Polygon RPC)
  - Polymarket Gamma API (market discovery)
  - Binance WebSocket (real-time BTC price)

Usage:
    python3 live_trader.py --capital 10        # Start with $10
    python3 live_trader.py --capital 50 --max-trades 5

⚠️  Requires: Polymarket wallet credentials (see SETUP section below)
"""

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone

import requests

import config
from data_feeds import BinanceFeed, PolymarketFeed
from edge_calculator import EdgeCalculator
from strategy import LEMAStrategy
from risk_manager import RiskManager
from trade_logger import TradeLogger, C

logger = logging.getLogger("lema.live")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Chainlink Oracle Reader (Polygon)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# BTC/USD Chainlink aggregator on Polygon
CHAINLINK_CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
POLYGON_RPC = "https://polygon-rpc.com"

# latestRoundData() selector
LATEST_ROUND_DATA = "0xfeaf968c"


def get_chainlink_price() -> dict | None:
    """
    Read BTC/USD price from the Chainlink oracle on Polygon.
    Returns: {'price': float, 'delay': float, 'round_id': int} or None
    """
    try:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{
                "to": CHAINLINK_CONTRACT,
                "data": LATEST_ROUND_DATA,
            }, "latest"],
            "id": 1,
        }
        resp = requests.post(POLYGON_RPC, json=payload, timeout=5)
        result = resp.json()

        if "result" in result and len(result["result"]) >= 194:
            raw = result["result"]
            round_id = int(raw[2:66], 16)
            price = int(raw[66:130], 16) / 1e8
            updated_at = int(raw[130:194], 16)

            delay = (
                datetime.now(timezone.utc)
                - datetime.fromtimestamp(updated_at, tz=timezone.utc)
            ).total_seconds()

            return {
                "price": price,
                "delay": delay,
                "round_id": round_id,
                "updated_at": updated_at,
            }
        return None
    except Exception as e:
        logger.warning(f"Chainlink read error: {e}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Polymarket Market Discovery
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def find_active_btc_5m_market() -> dict | None:
    """
    Query Polymarket Gamma API for the current active BTC 5-minute market.
    Returns the market dict or None.
    """
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": 50},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()

        btc_5m = []
        for m in markets:
            q = m.get("question", "").lower()
            if (
                ("bitcoin" in q or "btc" in q)
                and ("5" in q or "minute" in q or "5-min" in q)
                and ("up" in q or "down" in q)
            ):
                btc_5m.append(m)

        if btc_5m:
            # Return the one with the earliest end date (most imminent)
            btc_5m.sort(key=lambda m: m.get("endDate", ""))
            return btc_5m[0]

        return None
    except Exception as e:
        logger.warning(f"Polymarket API error: {e}")
        return None


def get_market_odds(market: dict) -> dict:
    """
    Extract current odds from a Polymarket market dict.
    Returns: {'up': float, 'down': float, 'condition_id': str}
    """
    tokens = market.get("tokens", [])
    up_price = 0.50
    down_price = 0.50
    condition_id = market.get("conditionId", "")

    for token in tokens:
        outcome = token.get("outcome", "").upper()
        price = float(token.get("price", 0.50))
        if outcome in ("YES", "UP"):
            up_price = price
        elif outcome in ("NO", "DOWN"):
            down_price = price

    return {
        "up": up_price,
        "down": down_price,
        "condition_id": condition_id,
        "market_id": market.get("id", ""),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Market Timing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_market_timing() -> tuple:
    """
    Calculate seconds elapsed and remaining in current 5-min window.
    Markets align to 5-minute UTC boundaries.
    """
    now = time.time()
    elapsed = now % config.WINDOW_DURATION
    remaining = config.WINDOW_DURATION - elapsed
    return elapsed, remaining


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trade Execution (Placeholder)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def execute_trade_live(
    direction: str,
    market_id: str,
    position_size: float,
    entry_price: float,
) -> bool:
    """
    Execute a live trade on Polymarket.

    ⚠️  PLACEHOLDER — Requires py-clob-client setup:

    1. Install: pip install py-clob-client
    2. Set environment variables:
         export POLYMARKET_API_KEY="your-api-key"
         export POLYMARKET_SECRET="your-secret"
         export POLYMARKET_PASSPHRASE="your-passphrase"
         export POLYMARKET_PRIVATE_KEY="your-eth-private-key"
    3. Ensure USDC balance on Polygon

    Once configured, replace this placeholder with:

        from py_clob_client.client import ClobClient
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.environ["POLYMARKET_API_KEY"],
            chain_id=137,
        )
        order = client.create_order(...)
        result = client.post_order(order)
    """
    print(
        f"\n  {C.BOLD}{C.YELLOW}⚠️  LIVE TRADE WOULD EXECUTE:{C.RESET}"
    )
    print(
        f"     Direction:  BUY_{direction}"
        f"\n     Market:     {market_id[:16]}..."
        f"\n     Entry:      ${entry_price:.4f}"
        f"\n     Size:       ${position_size:.2f}"
    )
    print(
        f"\n  {C.DIM}To enable, configure Polymarket API credentials.{C.RESET}"
        f"\n  {C.DIM}See live_trader.py → execute_trade_live() for setup.{C.RESET}"
    )
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Live Bot Loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LiveLEMABot:
    """
    Live trading mode:
      1. Real BTC prices from Binance
      2. Real market odds from Polymarket API
      3. Real oracle price from Chainlink on Polygon
      4. Trade execution via Polymarket CLOB (when configured)
    """

    def __init__(self, capital: float, max_trades: int = None):
        self.capital = capital
        self.max_trades = max_trades
        self._running = True

        self.binance = BinanceFeed()
        self.polymarket = PolymarketFeed(self.binance)
        self.edge_calc = EdgeCalculator()
        self.risk = RiskManager(capital)
        self.strategy = LEMAStrategy(
            self.binance, self.polymarket, self.edge_calc, capital
        )
        self.logger = TradeLogger(journal_path="live_trade_journal.csv")

        self._trades_executed = 0
        self._current_window_id = -1

    def run(self):
        self._setup_signals()

        print(f"\n{C.BOLD}{C.CYAN}{'═' * 72}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}  ⚡ LEMA Live Trader — Polymarket 5-Min BTC{C.RESET}")
        print(f"{C.CYAN}{'═' * 72}{C.RESET}\n")

        # Check components
        print("  [1/3] Connecting to Binance...", end=" ", flush=True)
        self.binance.start()
        time.sleep(1.5)
        if self.binance.is_connected():
            print(f"✓  BTC: ${self.binance.get_price():,.2f}")
        else:
            print("❌  Cannot connect")
            return

        print("  [2/3] Reading Chainlink oracle...", end=" ", flush=True)
        oracle = get_chainlink_price()
        if oracle:
            print(
                f"✓  Oracle: ${oracle['price']:,.2f} "
                f"(delay: {oracle['delay']:.0f}s)"
            )
        else:
            print("⚠️  Could not read (will retry)")

        print("  [3/3] Finding Polymarket market...", end=" ", flush=True)
        market = find_active_btc_5m_market()
        if market:
            print(f"✓  {market.get('question', 'BTC 5-min')[:50]}")
            odds = get_market_odds(market)
            print(f"         UP: {odds['up']:.0%}  DOWN: {odds['down']:.0%}")
        else:
            print("⚠️  No active market found (will poll)")

        print(f"\n  Capital:     ${self.capital:,.2f}")
        print(f"  Max trades:  {self.max_trades or 'unlimited'}")
        print(f"  Min edge:    {config.MIN_EDGE_THRESHOLD:.0%}")
        print(f"\n  {C.YELLOW}Press Ctrl+C to stop{C.RESET}\n")

        # Main loop
        try:
            while self._running:
                elapsed, remaining = get_market_timing()
                window_id = int(time.time()) // config.WINDOW_DURATION

                # New window
                if window_id != self._current_window_id:
                    self._current_window_id = window_id
                    self._on_new_window()

                # Print tick
                spot = self.binance.get_price()
                delta = self.polymarket.get_spot_delta()
                phase = "OBSERVE" if elapsed < 180 else (
                    "EVALUATE" if elapsed < 270 else "HOLD"
                )
                self.logger.print_tick(elapsed, remaining, spot, delta, phase)

                # Evaluate in the entry window
                if 180 <= elapsed <= 270 and self._can_enter():
                    self._evaluate_and_trade()

                # Window end: resolve
                if remaining < 1.5 and self._current_window_id == window_id:
                    self._on_window_end()

                time.sleep(1)

                # Max trades check
                if self.max_trades and self._trades_executed >= self.max_trades:
                    print(f"\n  Max trades ({self.max_trades}) reached.")
                    break

        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _on_new_window(self):
        """Start a new 5-minute window."""
        # Use Chainlink oracle price if available, else Binance price
        oracle = get_chainlink_price()
        if oracle and oracle["delay"] < 120:
            oracle_price = oracle["price"]
            source = f"Chainlink (delay: {oracle['delay']:.0f}s)"
        else:
            oracle_price = self.binance.get_price()
            source = "Binance (Chainlink unavailable)"

        # Set the oracle open on the polymarket feed
        self.polymarket._oracle_open = oracle_price
        self.polymarket._window_start = time.time()
        self.polymarket._window_id += 1

        self.strategy.reset_for_new_window(self.risk.current_capital)
        self._entered_this_window = False

        self.logger.print_window_start(
            self.polymarket.window_id, oracle_price
        )
        print(f"  {C.DIM}Oracle source: {source}{C.RESET}")

        # Fetch current market
        market = find_active_btc_5m_market()
        if market:
            odds = get_market_odds(market)
            self._current_market = market
            self._current_odds = odds
            print(
                f"  {C.DIM}Market odds: "
                f"UP {odds['up']:.0%} / DOWN {odds['down']:.0%}{C.RESET}"
            )
        else:
            self._current_market = None
            self._current_odds = None

    def _can_enter(self) -> bool:
        if getattr(self, "_entered_this_window", False):
            return False
        can, reason = self.risk.can_trade()
        if not can:
            return False
        return True

    def _evaluate_and_trade(self):
        """Run LEMA evaluation and execute if signal triggers."""
        signal = self.strategy.evaluate()

        if signal.action == "NO_TRADE":
            elapsed = self.polymarket.seconds_elapsed()
            if elapsed >= config.ENTRY_START:
                self.logger.print_no_trade(signal.reason)
                self._entered_this_window = True
            return

        # Trade signal!
        self.logger.print_evaluation(signal.details.get("edge", {}))
        self.logger.print_trade_entry(signal)
        self._entered_this_window = True

        # Try live execution
        market_id = ""
        if self._current_market:
            market_id = self._current_market.get("id", "")

        executed = execute_trade_live(
            direction=signal.direction,
            market_id=market_id,
            position_size=signal.position_size,
            entry_price=signal.entry_price,
        )

        if executed:
            self._trades_executed += 1
            self._pending_trade = signal
        else:
            # Log as paper trade even if not executed live
            self._pending_trade = signal
            print(f"  {C.DIM}(Logged as paper trade){C.RESET}")

    def _on_window_end(self):
        """Resolve window and record outcome."""
        result = self.polymarket.resolve_window()
        trade_record = None

        pending = getattr(self, "_pending_trade", None)
        if pending and pending.action != "NO_TRADE":
            won = (
                (pending.direction == "UP" and result["winner"] == "UP")
                or (pending.direction == "DOWN" and result["winner"] == "DOWN")
            )
            trade_record = self.risk.record_outcome(
                window_id=self.polymarket.window_id,
                direction=pending.direction,
                entry_price=pending.entry_price,
                position_size=pending.position_size,
                won=won,
            )

        self.logger.print_settlement(result, trade_record)
        self.logger.print_stats(self.risk.get_stats())
        self._pending_trade = None
        self._current_window_id = -1  # Force new window detection

    def _setup_signals(self):
        signal.signal(signal.SIGINT, self._sig_handler)
        signal.signal(signal.SIGTERM, self._sig_handler)

    def _sig_handler(self, signum, frame):
        print(f"\n\n  🛑 Shutting down...")
        self._running = False

    def _shutdown(self):
        self.binance.stop()
        stats = self.risk.get_stats()
        stats["initial_capital"] = self.capital
        self.logger.print_shutdown_summary(stats)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI Entry Point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(
        description="LEMA Live Trader — Polymarket 5-Min BTC"
    )
    parser.add_argument(
        "--capital", type=float, default=10.0,
        help="Trading capital in USDC (default: $10)"
    )
    parser.add_argument(
        "--max-trades", type=int, default=None,
        help="Max trades before stopping"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s │ %(name)s │ %(levelname)s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    bot = LiveLEMABot(capital=args.capital, max_trades=args.max_trades)
    bot.run()


if __name__ == "__main__":
    main()
