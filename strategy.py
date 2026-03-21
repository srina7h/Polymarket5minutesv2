"""
LEMA Trading Bot — Strategy Engine
Implements the Late-Entry Momentum Arbitrage decision logic.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from data_feeds import BinanceFeed, PolymarketFeed
from edge_calculator import EdgeCalculator

logger = logging.getLogger("lema.strategy")


@dataclass
class TradeSignal:
    """Output of strategy evaluation."""
    action: str           # "BUY_UP", "BUY_DOWN", "NO_TRADE"
    direction: str        # "UP" or "DOWN"
    edge_score: float     # Composite ES
    confidence: str       # LOW / MEDIUM / HIGH / VERY_HIGH
    entry_price: float    # Simulated share price
    position_size: float  # USDC to risk
    reason: str           # Human-readable justification
    details: dict = field(default_factory=dict)


class LEMAStrategy:
    """
    Late-Entry Momentum Arbitrage for Polymarket 5-minute BTC markets.

    Phases:
        0–180s:   OBSERVE — collect price data, do nothing
        180–240s: EVALUATE — compute edge, check criteria
        240–270s: EXECUTE — enter if edge ≥ threshold
        270–300s: HOLD — wait for settlement
    """

    def __init__(
        self,
        binance: BinanceFeed,
        polymarket: PolymarketFeed,
        edge_calc: EdgeCalculator,
        capital: float,
    ):
        self.binance = binance
        self.polymarket = polymarket
        self.edge_calc = edge_calc
        self.capital = capital

        self._trade_entered = False
        self._last_signal: Optional[TradeSignal] = None

    def reset_for_new_window(self, capital: float):
        """Call at the start of each 5-minute window."""
        self._trade_entered = False
        self._last_signal = None
        self.capital = capital

    def evaluate(self) -> TradeSignal:
        """
        Run the LEMA evaluation pipeline.
        Returns a TradeSignal with action and reasoning.
        """
        elapsed = self.polymarket.seconds_elapsed()
        remaining = self.polymarket.seconds_remaining()

        # ── Phase 1: OBSERVE (0–180s) ──────────
        if elapsed < config.OBSERVATION_END:
            return self._no_trade(
                f"OBSERVING — {remaining:.0f}s remaining "
                f"(need to wait until {config.OBSERVATION_END}s)"
            )

        # Already entered this window
        if self._trade_entered:
            return self._no_trade("Already entered a trade this window")

        # ── Phase 4: HOLD (270–300s) ───────────
        if elapsed > config.ENTRY_END:
            return self._no_trade(
                f"HOLD phase — too late to enter ({remaining:.0f}s left)"
            )

        # ── Phase 2 & 3: EVALUATE + EXECUTE ────
        # Check minimum time remaining
        if remaining < config.MIN_TIME_REMAINING:
            return self._no_trade(
                f"Insufficient time: {remaining:.0f}s < "
                f"{config.MIN_TIME_REMAINING}s minimum"
            )

        # Gather data
        spot_price = self.binance.get_price()
        oracle_open = self.polymarket.oracle_open
        spot_delta = spot_price - oracle_open
        odds = self.polymarket.get_simulated_odds()
        book = self.polymarket.get_simulated_book()
        history = self.binance.get_price_history(seconds=300)

        # ── Criterion 1: Spot Delta Magnitude ──
        if abs(spot_delta) < config.MIN_SPOT_DELTA_USD:
            return self._no_trade(
                f"Spot delta ${spot_delta:+.2f} < "
                f"${config.MIN_SPOT_DELTA_USD} minimum"
            )

        # ── Criterion 2: Direction Consistency ──
        dir_check = self._check_direction_consistency(history, oracle_open)
        if not dir_check["consistent"]:
            return self._no_trade(
                f"Direction inconsistent: {dir_check['up_mins']}/{dir_check['total_mins']} "
                f"UP (need ≥{config.MIN_DIRECTION_CONSISTENCY})"
            )

        # ── Criterion 3: Volatility Filter ─────
        vol_check = self._check_volatility(history)
        if vol_check["too_volatile"]:
            return self._no_trade(
                f"Volatility too high: {vol_check['ratio']:.1f}× "
                f"(max {config.MAX_VOLATILITY_MULTIPLIER}×)"
            )

        # ── Criterion 4: Spread Check ──────────
        if odds["spread"] > 0.10:
            return self._no_trade(
                f"Spread too wide: {odds['spread']:.2f} > $0.10"
            )

        # ── Compute Edge Score ─────────────────
        market_prob_up = odds["up"]
        edge = self.edge_calc.calculate(
            spot_price=spot_price,
            oracle_open=oracle_open,
            market_probability=market_prob_up,
            price_history=history,
            book_imbalance=book["imbalance"],
        )

        # ── Criterion 5: Minimum Edge ──────────
        if edge["edge_score"] < config.MIN_EDGE_THRESHOLD:
            return self._no_trade(
                f"Edge {edge['edge_score']:.1%} < "
                f"{config.MIN_EDGE_THRESHOLD:.0%} threshold | "
                f"PE={edge['sub_scores']['price_edge']:.3f} "
                f"MO={edge['sub_scores']['momentum']:.3f} "
                f"BI={edge['sub_scores']['book_imbalance']:.3f}"
            )

        # ── All criteria passed → TRADE ────────
        direction = edge["direction"]
        entry_price = self._calculate_entry_price(odds, direction)
        position_size = self._calculate_position_size(
            edge["edge_score"], edge["estimated_prob"], entry_price
        )

        action = f"BUY_{direction}"
        confidence = edge["confidence"]

        signal = TradeSignal(
            action=action,
            direction=direction,
            edge_score=edge["edge_score"],
            confidence=confidence,
            entry_price=entry_price,
            position_size=position_size,
            reason=(
                f"ENTER {direction} @ ${entry_price:.2f} | "
                f"Edge={edge['edge_score']:.1%} ({confidence}) | "
                f"Δ=${spot_delta:+.2f} | "
                f"EstP={edge['estimated_prob']:.0%} vs MktP={market_prob_up:.0%}"
            ),
            details={
                "edge": edge,
                "odds": odds,
                "book": book,
                "spot_delta": spot_delta,
                "spot_price": spot_price,
                "oracle_open": oracle_open,
            },
        )

        self._trade_entered = True
        self._last_signal = signal
        return signal

    # ── Internal helpers ────────────────────

    def _no_trade(self, reason: str) -> TradeSignal:
        return TradeSignal(
            action="NO_TRADE",
            direction="",
            edge_score=0.0,
            confidence="LOW",
            entry_price=0.0,
            position_size=0.0,
            reason=reason,
        )

    def _check_direction_consistency(
        self, history: list, oracle_open: float
    ) -> dict:
        """Check if ≥3 of 4 one-minute intervals moved in same direction."""
        if len(history) < 20:
            return {"consistent": False, "up_mins": 0, "total_mins": 0}

        now = time.time()
        up_minutes = 0
        down_minutes = 0
        total = 0

        for i in range(4):
            end_t = now - (i * 60)
            start_t = end_t - 60
            bucket = [p for t, p in history if start_t <= t < end_t]
            if len(bucket) >= 2:
                total += 1
                if bucket[-1] > bucket[0]:
                    up_minutes += 1
                elif bucket[-1] < bucket[0]:
                    down_minutes += 1

        dominant = max(up_minutes, down_minutes)
        consistent = dominant >= config.MIN_DIRECTION_CONSISTENCY and total >= 3

        return {
            "consistent": consistent,
            "up_mins": up_minutes,
            "down_mins": down_minutes,
            "total_mins": total,
        }

    def _check_volatility(self, history: list) -> dict:
        """Check if current volatility is within bounds."""
        if len(history) < 60:
            return {"too_volatile": False, "ratio": 1.0}

        now = time.time()
        # Current 5-min volatility (stdev of returns)
        recent = [p for t, p in history if t >= now - 300]
        if len(recent) < 10:
            return {"too_volatile": False, "ratio": 1.0}

        returns = [
            (recent[i] - recent[i - 1]) / recent[i - 1]
            for i in range(1, len(recent))
            if recent[i - 1] > 0
        ]
        if not returns:
            return {"too_volatile": False, "ratio": 1.0}

        import statistics
        current_vol = statistics.stdev(returns) if len(returns) > 1 else 0

        # Rolling 1-hour average volatility (simplified: use all history)
        all_prices = [p for _, p in history]
        all_returns = [
            (all_prices[i] - all_prices[i - 1]) / all_prices[i - 1]
            for i in range(1, len(all_prices))
            if all_prices[i - 1] > 0
        ]
        avg_vol = statistics.stdev(all_returns) if len(all_returns) > 1 else current_vol

        ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

        return {
            "too_volatile": ratio > config.MAX_VOLATILITY_MULTIPLIER,
            "ratio": ratio,
        }

    def _calculate_entry_price(self, odds: dict, direction: str) -> float:
        """Simulated entry price (odds + half spread)."""
        if direction == "UP":
            return odds["up"] + odds["spread"] / 2
        else:
            return odds["down"] + odds["spread"] / 2

    def _calculate_position_size(
        self, edge_score: float, estimated_prob: float, entry_price: float
    ) -> float:
        """
        Position size using modified Kelly (25% of full Kelly),
        capped at MAX_POSITION_PCT of capital.

        Uses the composite edge score to derive an effective win probability
        rather than relying solely on the price-edge estimated probability.
        """
        if entry_price <= 0 or entry_price >= 1:
            return 0.0

        # Derive effective win probability from edge score:
        # If edge_score = 0.08, we estimate ~58% win probability
        # If edge_score = 0.15, we estimate ~65% win probability
        # Scale: effective_p = 0.50 + edge_score
        effective_p = min(0.50 + edge_score, 0.95)
        effective_p = max(effective_p, estimated_prob)  # Use whichever is higher

        # Kelly: f* = (bp - q) / b
        # b = payout ratio: if price is 0.60, profit is $0.40 → b = 0.40/0.60
        profit_per_share = 1.0 - entry_price
        b = profit_per_share / entry_price
        p = effective_p
        q = 1.0 - p

        if b <= 0:
            return 0.0

        kelly_full = (b * p - q) / b
        kelly_fraction = max(0, kelly_full * config.KELLY_FRACTION)

        # Cap at MAX_POSITION_PCT
        max_position = self.capital * config.MAX_POSITION_PCT
        position = min(kelly_fraction * self.capital, max_position)

        # Apply conviction scaling
        if edge_score < config.HIGH_CONVICTION_THRESHOLD:
            position *= 0.5  # Half size for non-high-conviction

        # Ensure minimum position when edge passes threshold
        if edge_score >= config.MIN_EDGE_THRESHOLD and position < 10.0:
            position = min(max_position * 0.25, self.capital * 0.005)

        return round(max(0, position), 2)

