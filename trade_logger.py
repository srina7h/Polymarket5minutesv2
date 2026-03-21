"""
LEMA Trading Bot — Trade Logger
CSV trade journal + real-time terminal dashboard.
"""

import csv
import logging
import os
import time
from datetime import datetime

import config

logger = logging.getLogger("lema.logger")

# ANSI color codes
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    BG_RED = "\033[41m"
    BG_GRN = "\033[42m"


JOURNAL_COLUMNS = [
    "timestamp",
    "window_id",
    "btc_open",
    "btc_close",
    "spot_delta",
    "direction",
    "action",
    "entry_price",
    "position_size",
    "edge_score",
    "price_edge",
    "momentum",
    "sentiment",
    "book_imbalance",
    "confidence",
    "outcome",
    "pnl",
    "capital_after",
    "reason",
]


class TradeLogger:
    """Logs trades to CSV and prints a real-time terminal dashboard."""

    def __init__(self, journal_path: str = None):
        self.journal_path = journal_path or config.TRADE_JOURNAL_FILE
        self._ensure_header()

    def _ensure_header(self):
        if not os.path.exists(self.journal_path):
            with open(self.journal_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(JOURNAL_COLUMNS)

    def log_decision(self, row: dict):
        """Append a trade decision (trade or no-trade) to the CSV."""
        with open(self.journal_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([row.get(col, "") for col in JOURNAL_COLUMNS])

    # ── Terminal Dashboard ──────────────────

    def print_header(self):
        print(f"\n{C.BOLD}{C.CYAN}{'═' * 72}{C.RESET}")
        print(
            f"{C.BOLD}{C.CYAN}  ⚡ LEMA Trading Bot — "
            f"Polymarket 5-Min BTC Dry Run{C.RESET}"
        )
        print(f"{C.BOLD}{C.CYAN}{'═' * 72}{C.RESET}\n")

    def print_window_start(self, window_id: int, oracle_open: float):
        print(f"\n{C.BOLD}{C.BLUE}{'─' * 72}{C.RESET}")
        print(
            f"  {C.BOLD}Window #{window_id}{C.RESET}  │  "
            f"Oracle Open: {C.BOLD}${oracle_open:,.2f}{C.RESET}  │  "
            f"{datetime.now().strftime('%H:%M:%S')}"
        )
        print(f"{C.BLUE}{'─' * 72}{C.RESET}")

    def print_tick(
        self,
        elapsed: float,
        remaining: float,
        btc_price: float,
        spot_delta: float,
        phase: str,
    ):
        """Print a compact one-line tick update."""
        # Color delta
        if spot_delta > 0:
            delta_str = f"{C.GREEN}+${spot_delta:,.2f}{C.RESET}"
        elif spot_delta < 0:
            delta_str = f"{C.RED}-${abs(spot_delta):,.2f}{C.RESET}"
        else:
            delta_str = f"${spot_delta:,.2f}"

        bar_len = 30
        filled = int(elapsed / config.WINDOW_DURATION * bar_len)
        bar = f"{'█' * filled}{'░' * (bar_len - filled)}"

        print(
            f"\r  {C.DIM}[{bar}]{C.RESET} "
            f"{elapsed:3.0f}s/{config.WINDOW_DURATION}s  │  "
            f"BTC: {C.BOLD}${btc_price:,.2f}{C.RESET}  │  "
            f"Δ: {delta_str}  │  "
            f"{C.DIM}{phase}{C.RESET}   ",
            end="",
            flush=True,
        )

    def print_evaluation(self, edge_result: dict):
        """Print edge score evaluation details."""
        es = edge_result.get("edge_score", 0)
        sub = edge_result.get("sub_scores", {})
        direction = edge_result.get("direction", "?")
        confidence = edge_result.get("confidence", "?")

        color = C.GREEN if es >= config.MIN_EDGE_THRESHOLD else C.YELLOW
        if es < 0.05:
            color = C.RED

        print(f"\n\n  {C.BOLD}📊 Edge Score Evaluation:{C.RESET}")
        print(
            f"     Edge Score:  {color}{C.BOLD}{es:.1%}{C.RESET}  "
            f"({confidence})"
        )
        print(
            f"     Direction:   {C.BOLD}{direction}{C.RESET}  │  "
            f"EstP: {edge_result.get('estimated_prob', 0):.0%}  │  "
            f"MktP: {edge_result.get('market_prob', 0):.0%}"
        )
        print(
            f"     Sub-scores:  "
            f"PE={sub.get('price_edge', 0):.3f}  "
            f"MO={sub.get('momentum', 0):.3f}  "
            f"SE={sub.get('sentiment', 0):.3f}  "
            f"BI={sub.get('book_imbalance', 0):.3f}"
        )

    def print_trade_entry(self, signal):
        """Print trade entry notification."""
        print(
            f"\n  {C.BG_GRN}{C.BOLD} 🚀 TRADE ENTERED {C.RESET}  "
            f"{signal.action} @ ${signal.entry_price:.4f}  │  "
            f"Size: ${signal.position_size:.2f}  │  "
            f"Edge: {signal.edge_score:.1%}"
        )

    def print_no_trade(self, reason: str):
        """Print no-trade decision (compact)."""
        print(f"\n  {C.DIM}  ⏭  NO TRADE: {reason}{C.RESET}")

    def print_settlement(self, result: dict, trade_record=None):
        """Print window settlement result."""
        winner = result["winner"]
        delta = result["delta"]
        color = C.GREEN if delta >= 0 else C.RED

        print(f"\n\n  {C.BOLD}🏁 Settlement:{C.RESET}")
        print(
            f"     Close: ${result['close_price']:,.2f}  │  "
            f"Δ: {color}${delta:+,.2f}{C.RESET}  │  "
            f"Winner: {C.BOLD}{winner}{C.RESET}"
        )

        if trade_record:
            pnl_color = C.GREEN if trade_record.pnl >= 0 else C.RED
            print(
                f"     Trade: {trade_record.direction} → "
                f"{trade_record.outcome}  │  "
                f"P&L: {pnl_color}{C.BOLD}${trade_record.pnl:+.2f}{C.RESET}"
            )

    def print_stats(self, stats: dict):
        """Print current session statistics."""
        pnl_color = C.GREEN if stats["daily_pnl"] >= 0 else C.RED

        print(f"\n  {C.BOLD}📈 Session Stats:{C.RESET}")
        print(
            f"     Capital: ${stats['capital']:,.2f}  │  "
            f"Daily P&L: {pnl_color}${stats['daily_pnl']:+.2f} "
            f"({stats['daily_pnl_pct']:+.1f}%){C.RESET}"
        )
        print(
            f"     Trades: {stats['trades_today']}  │  "
            f"W/L: {stats['total_wins']}/{stats['total_losses']}  │  "
            f"Win Rate: {stats['win_rate']:.1f}%"
        )
        if stats.get("is_stopped"):
            print(f"     {C.RED}⛔ {stats['stop_reason']}{C.RESET}")

    def print_shutdown_summary(self, stats: dict):
        """Print final summary on shutdown."""
        pnl_color = C.GREEN if stats["daily_pnl"] >= 0 else C.RED

        print(f"\n\n{C.BOLD}{C.CYAN}{'═' * 72}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}  📋 Session Summary{C.RESET}")
        print(f"{C.CYAN}{'═' * 72}{C.RESET}")
        print(f"  Starting Capital:  ${stats.get('initial_capital', config.INITIAL_CAPITAL):,.2f}")
        print(f"  Final Capital:     ${stats['capital']:,.2f}")
        print(
            f"  Session P&L:       {pnl_color}{C.BOLD}"
            f"${stats['daily_pnl']:+.2f} "
            f"({stats['daily_pnl_pct']:+.1f}%){C.RESET}"
        )
        print(f"  Total Trades:      {stats['trades_today']}")
        print(
            f"  Win / Loss:        "
            f"{C.GREEN}{stats['total_wins']}W{C.RESET} / "
            f"{C.RED}{stats['total_losses']}L{C.RESET}"
        )
        print(f"  Win Rate:          {stats['win_rate']:.1f}%")
        print(f"  Journal:           {self.journal_path}")
        print(f"{C.CYAN}{'═' * 72}{C.RESET}\n")

    def print_risk_blocked(self, reason: str):
        """Print risk block notification."""
        print(f"\n  {C.BG_RED}{C.BOLD} ⛔ RISK BLOCK {C.RESET}  {reason}")
