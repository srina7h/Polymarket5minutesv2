"""
LEMA Trading Bot — Risk Manager
Enforces all risk rules from the trading framework.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config

logger = logging.getLogger("lema.risk")


@dataclass
class TradeRecord:
    """Record of a completed trade."""
    window_id: int
    direction: str
    entry_price: float
    position_size: float
    outcome: str        # "WIN" or "LOSS"
    pnl: float
    timestamp: float


class RiskManager:
    """
    Enforces:
      - Max 2% capital per trade
      - Max 6% daily loss → stop for day
      - Max 3 consecutive losses → 30m cooldown
      - Max 15 trades per day
      - Cooldown after consecutive losses
    """

    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.trades_today = 0
        self.total_wins = 0
        self.total_losses = 0
        self.trade_history: list[TradeRecord] = []
        self._cooldown_until = 0.0
        self._stopped = False
        self._stop_reason = ""

    def can_trade(self) -> tuple[bool, str]:
        """
        Pre-trade gate. Returns (allowed, reason).
        Must pass ALL conditions.
        """
        # Already stopped for the day
        if self._stopped:
            return False, f"STOPPED: {self._stop_reason}"

        # Daily loss limit
        if self.daily_pnl <= -(self.initial_capital * config.MAX_DAILY_LOSS_PCT):
            self._stopped = True
            self._stop_reason = (
                f"Daily loss limit hit: ${self.daily_pnl:+.2f} "
                f"(max -${self.initial_capital * config.MAX_DAILY_LOSS_PCT:.2f})"
            )
            return False, self._stop_reason

        # Trade count limit
        if self.trades_today >= config.MAX_TRADES_PER_DAY:
            return False, (
                f"Max trades reached: {self.trades_today}/{config.MAX_TRADES_PER_DAY}"
            )

        # Consecutive loss cooldown
        if time.time() < self._cooldown_until:
            remaining = self._cooldown_until - time.time()
            return False, (
                f"Cooldown active: {remaining:.0f}s remaining "
                f"(after {config.MAX_CONSECUTIVE_LOSSES} consecutive losses)"
            )

        # Consecutive losses (trigger cooldown)
        if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            self._cooldown_until = time.time() + config.COOLDOWN_SECONDS
            self.consecutive_losses = 0  # Reset after starting cooldown
            return False, (
                f"Entering {config.COOLDOWN_SECONDS}s cooldown after "
                f"{config.MAX_CONSECUTIVE_LOSSES} consecutive losses"
            )

        # Capital check
        min_trade = self.current_capital * config.MAX_POSITION_PCT * 0.1
        if self.current_capital < min_trade:
            self._stopped = True
            self._stop_reason = "Insufficient capital"
            return False, self._stop_reason

        return True, "All risk checks passed ✓"

    def record_outcome(
        self,
        window_id: int,
        direction: str,
        entry_price: float,
        position_size: float,
        won: bool,
    ) -> TradeRecord:
        """
        Record a trade outcome and update all running stats.
        """
        if won:
            # Profit: shares × (1 - entry_price)
            shares = position_size / entry_price if entry_price > 0 else 0
            pnl = shares * (1.0 - entry_price)
            # Subtract fee
            fee = position_size * config.SIM_FEE_MAX * 0.5  # Avg fee
            pnl -= fee
            outcome = "WIN"
            self.total_wins += 1
            self.consecutive_losses = 0
        else:
            # Loss: entire position
            pnl = -position_size
            outcome = "LOSS"
            self.total_losses += 1
            self.consecutive_losses += 1

        self.daily_pnl += pnl
        self.current_capital += pnl
        self.trades_today += 1

        record = TradeRecord(
            window_id=window_id,
            direction=direction,
            entry_price=entry_price,
            position_size=position_size,
            outcome=outcome,
            pnl=round(pnl, 2),
            timestamp=time.time(),
        )
        self.trade_history.append(record)

        logger.info(
            f"Trade #{self.trades_today}: {outcome} {direction} → "
            f"${pnl:+.2f} | Capital: ${self.current_capital:,.2f} | "
            f"Daily P&L: ${self.daily_pnl:+.2f}"
        )

        return record

    def get_stats(self) -> dict:
        """Return current session statistics."""
        total = self.total_wins + self.total_losses
        win_rate = self.total_wins / total if total > 0 else 0.0

        return {
            "capital": round(self.current_capital, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_pnl_pct": round(
                self.daily_pnl / self.initial_capital * 100, 2
            ),
            "trades_today": self.trades_today,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "win_rate": round(win_rate * 100, 1),
            "consecutive_losses": self.consecutive_losses,
            "is_stopped": self._stopped,
            "stop_reason": self._stop_reason,
        }

    def reset_daily(self):
        """Reset daily counters (call at start of trading day)."""
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self._stopped = False
        self._stop_reason = ""
        self._cooldown_until = 0.0
