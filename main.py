#!/usr/bin/env python3
"""
LEMA Trading Bot — Main Orchestrator
Late-Entry Momentum Arbitrage for Polymarket 5-Minute BTC Markets

Usage:
    python main.py --dry-run                   # Default: 5-min windows, unlimited
    python main.py --dry-run --duration 15     # Run for 15 minutes (3 windows)
    python main.py --dry-run --windows 5       # Run exactly 5 windows
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime

import config
from data_feeds import BinanceFeed, PolymarketFeed
from edge_calculator import EdgeCalculator
from strategy import LEMAStrategy
from risk_manager import RiskManager
from trade_logger import TradeLogger


# ── Logging Setup ──────────────────────────

def setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL),
        format="%(asctime)s │ %(name)-12s │ %(levelname)-5s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy websocket logs
    logging.getLogger("websocket").setLevel(logging.WARNING)


# ── Main Bot ───────────────────────────────

class LEMABot:
    """
    Orchestrates the LEMA dry-run trading loop:
      1. Connect to Binance for real BTC prices
      2. Simulate 5-minute Polymarket windows
      3. Run strategy evaluation each tick
      4. Log all decisions and outcomes
    """

    def __init__(self, max_duration: float = None, max_windows: int = None):
        self.max_duration = max_duration
        self.max_windows = max_windows
        self._running = True
        self._start_time = None

        # Initialize components
        self.binance = BinanceFeed()
        self.polymarket = PolymarketFeed(self.binance)
        self.edge_calc = EdgeCalculator()
        self.risk = RiskManager(config.INITIAL_CAPITAL)
        self.strategy = LEMAStrategy(
            self.binance, self.polymarket, self.edge_calc,
            config.INITIAL_CAPITAL
        )
        self.logger = TradeLogger()

        # Track current window state
        self._current_trade = None
        self._windows_completed = 0
        self._evaluation_printed = False

    def run(self):
        """Main entry point."""
        self._start_time = time.time()
        self._setup_signal_handlers()

        self.logger.print_header()
        print(f"  Mode:     {'DRY RUN (paper trading)' if config.DRY_RUN else 'LIVE'}")
        print(f"  Capital:  ${config.INITIAL_CAPITAL:,.2f}")
        print(f"  Duration: {self._format_duration()}")
        print(f"  Min Edge: {config.MIN_EDGE_THRESHOLD:.0%}")
        print(f"  Max Risk: {config.MAX_POSITION_PCT:.0%} per trade")
        print()

        # Start Binance feed
        print("  Connecting to Binance WebSocket...", end=" ", flush=True)
        self.binance.start()
        time.sleep(1.5)  # Wait for first price tick
        if self.binance.is_connected():
            price = self.binance.get_price()
            print(f"✓  BTC: ${price:,.2f}")
        else:
            print("⚠  Waiting for data...")
            time.sleep(3)
            if not self.binance.is_connected():
                print("  ❌ Could not connect to Binance. Check your network.")
                return

        # Wait for next window boundary (aligned to 5-min clock)
        self._wait_for_window_boundary()

        # Main loop
        try:
            while self._running and not self._should_stop():
                self._run_window()
                self._windows_completed += 1

                if self._should_stop():
                    break

                # Brief pause between windows
                time.sleep(0.5)
                self._wait_for_window_boundary()

        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _run_window(self):
        """Execute one complete 5-minute window."""
        # Start new window
        oracle_open = self.polymarket.start_new_window()
        self.strategy.reset_for_new_window(self.risk.current_capital)
        self._current_trade = None
        self._evaluation_printed = False

        self.logger.print_window_start(
            self.polymarket.window_id, oracle_open
        )

        # Check risk before starting
        can_trade, risk_reason = self.risk.can_trade()
        if not can_trade:
            self.logger.print_risk_blocked(risk_reason)

        # Tick loop for this window
        while self.polymarket.seconds_remaining() > 0 and self._running:
            elapsed = self.polymarket.seconds_elapsed()
            remaining = self.polymarket.seconds_remaining()
            spot_price = self.binance.get_price()
            spot_delta = self.polymarket.get_spot_delta()

            # Determine phase label
            if elapsed < config.OBSERVATION_END:
                phase = "OBSERVE"
            elif elapsed < config.ENTRY_END:
                phase = "EVALUATE"
            else:
                phase = "HOLD"

            # Print tick
            self.logger.print_tick(
                elapsed, remaining, spot_price, spot_delta, phase
            )

            # Run strategy evaluation (only in EVALUATE/EXECUTE phase)
            if (config.OBSERVATION_END <= elapsed <= config.ENTRY_END
                    and self._current_trade is None):

                can_trade, risk_reason = self.risk.can_trade()
                if not can_trade:
                    if not self._evaluation_printed:
                        self.logger.print_risk_blocked(risk_reason)
                        self._evaluation_printed = True
                else:
                    signal = self.strategy.evaluate()

                    if signal.action != "NO_TRADE" and not self._evaluation_printed:
                        # Print the edge evaluation
                        self.logger.print_evaluation(signal.details.get("edge", {}))
                        self.logger.print_trade_entry(signal)
                        self._current_trade = signal
                        self._evaluation_printed = True

                    elif (signal.action == "NO_TRADE"
                          and elapsed >= config.ENTRY_START
                          and not self._evaluation_printed):
                        # Final no-trade decision at entry window
                        self.logger.print_no_trade(signal.reason)
                        self._evaluation_printed = True

            time.sleep(config.TICK_INTERVAL)

        # ── Settlement ─────────────────────
        result = self.polymarket.resolve_window()
        trade_record = None

        if self._current_trade is not None:
            # Determine if we won
            won = (
                (self._current_trade.direction == "UP" and result["winner"] == "UP")
                or (self._current_trade.direction == "DOWN" and result["winner"] == "DOWN")
            )
            trade_record = self.risk.record_outcome(
                window_id=self.polymarket.window_id,
                direction=self._current_trade.direction,
                entry_price=self._current_trade.entry_price,
                position_size=self._current_trade.position_size,
                won=won,
            )

            # Log to CSV
            edge = self._current_trade.details.get("edge", {})
            sub = edge.get("sub_scores", {})
            self.logger.log_decision({
                "timestamp": datetime.now().isoformat(),
                "window_id": self.polymarket.window_id,
                "btc_open": result["oracle_open"],
                "btc_close": result["close_price"],
                "spot_delta": result["delta"],
                "direction": self._current_trade.direction,
                "action": self._current_trade.action,
                "entry_price": self._current_trade.entry_price,
                "position_size": self._current_trade.position_size,
                "edge_score": self._current_trade.edge_score,
                "price_edge": sub.get("price_edge", 0),
                "momentum": sub.get("momentum", 0),
                "sentiment": sub.get("sentiment", 0),
                "book_imbalance": sub.get("book_imbalance", 0),
                "confidence": self._current_trade.confidence,
                "outcome": trade_record.outcome,
                "pnl": trade_record.pnl,
                "capital_after": self.risk.current_capital,
                "reason": self._current_trade.reason,
            })
        else:
            # Log NO_TRADE window
            self.logger.log_decision({
                "timestamp": datetime.now().isoformat(),
                "window_id": self.polymarket.window_id,
                "btc_open": result["oracle_open"],
                "btc_close": result["close_price"],
                "spot_delta": result["delta"],
                "direction": "",
                "action": "NO_TRADE",
                "entry_price": 0,
                "position_size": 0,
                "edge_score": 0,
                "price_edge": 0,
                "momentum": 0,
                "sentiment": 0,
                "book_imbalance": 0,
                "confidence": "LOW",
                "outcome": "SKIP",
                "pnl": 0,
                "capital_after": self.risk.current_capital,
                "reason": "No entry criteria met",
            })

        self.logger.print_settlement(result, trade_record)
        self.logger.print_stats(self.risk.get_stats())

    # ── Helpers ─────────────────────────────

    def _wait_for_window_boundary(self):
        """Wait until the next 5-minute clock boundary."""
        now = time.time()
        # Align to 5-minute boundary
        seconds_into_period = now % config.WINDOW_DURATION
        wait = config.WINDOW_DURATION - seconds_into_period

        if wait > 2:  # Only wait if more than 2 seconds away
            mins = int(wait // 60)
            secs = int(wait % 60)
            print(
                f"\n  ⏳ Next window in {mins}m {secs}s "
                f"(aligned to 5-min boundary)..."
            )
            while wait > 0.5 and self._running:
                chunk = min(wait, 1.0)
                time.sleep(chunk)
                wait -= chunk

    def _should_stop(self) -> bool:
        if not self._running:
            return True
        if self.max_windows and self._windows_completed >= self.max_windows:
            return True
        if self.max_duration:
            elapsed = time.time() - self._start_time
            if elapsed >= self.max_duration:
                return True
        if self.risk.get_stats()["is_stopped"]:
            return True
        return False

    def _format_duration(self) -> str:
        if self.max_windows:
            return f"{self.max_windows} windows"
        if self.max_duration:
            mins = int(self.max_duration // 60)
            return f"{mins} minutes"
        return "Unlimited (Ctrl+C to stop)"

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print(f"\n\n  🛑 Received shutdown signal...")
        self._running = False

    def _shutdown(self):
        """Clean shutdown with summary."""
        self.binance.stop()
        stats = self.risk.get_stats()
        stats["initial_capital"] = config.INITIAL_CAPITAL
        self.logger.print_shutdown_summary(stats)


# ── CLI Entry Point ────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LEMA Trading Bot — Polymarket 5-Min BTC Dry Run"
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Run in paper trading mode (default)"
    )
    parser.add_argument(
        "--duration", type=int, default=None,
        help="Max duration in minutes (e.g., 15 for 3 windows)"
    )
    parser.add_argument(
        "--windows", type=int, default=None,
        help="Max number of windows to trade"
    )
    parser.add_argument(
        "--capital", type=float, default=None,
        help=f"Starting capital (default: ${config.INITIAL_CAPITAL:,.0f})"
    )

    args = parser.parse_args()

    if args.capital:
        config.INITIAL_CAPITAL = args.capital

    setup_logging()

    bot = LEMABot(
        max_duration=args.duration * 60 if args.duration else None,
        max_windows=args.windows,
    )
    bot.run()


if __name__ == "__main__":
    main()
