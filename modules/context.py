"""
Confirmation Sniper — Shared Trading Context

Central state container. Every module reads/writes to this single object.
No I/O in this file — pure data structures.
"""

import collections
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Signal:
    """Entry signal emitted by signal_detector."""
    direction: str           # "UP" or "DOWN"
    true_prob: float         # Model probability (0.0 - 1.0)
    market_odds: float       # Current Polymarket midpoint
    odds_lag: float          # true_prob - market_odds
    btc_delta: float         # BTC price change from window open
    entry_price: float       # Target entry price
    timestamp: float = 0.0   # When signal was generated

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class Position:
    """Active trading position."""
    direction: str           # "UP" or "DOWN"
    token_id: str            # CLOB token ID
    entry_price: float       # Avg fill price
    shares: float            # Number of shares
    usd_cost: float          # Total USD spent
    entry_time: float        # Timestamp of entry
    order_id: str = ""       # CLOB order ID
    peak_unrealized: float = 0.0  # Highest unrealized PnL seen

    @property
    def current_value(self):
        return self.shares  # At settlement, winning shares = $1.00 each


@dataclass
class TradeRecord:
    """Completed trade for history/analytics."""
    trade_id: int
    timestamp: str           # ISO format
    direction: str
    entry_price: float
    exit_price: float
    shares: float
    usd_cost: float
    pnl: float
    outcome: str             # "WIN" / "LOSS" / "EXIT"
    btc_delta: float
    odds_lag: float
    window_start: int
    hold_duration: float     # seconds held
    exit_reason: str         # "settlement" / "reversal" / "timeout" / "stop_loss"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Window Phases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE_WATCHING = "WATCHING"
PHASE_CONFIRMING = "CONFIRMING"
PHASE_SNIPING = "SNIPING"
PHASE_HOLDING = "HOLDING"
PHASE_EXITING = "EXITING"
PHASE_SETTLED = "SETTLED"
PHASE_IDLE = "IDLE"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trading Context (Singleton)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TradingContext:
    """
    Central state for the entire trading system.
    Thread-safe reads/writes via lock for critical sections.
    """

    def __init__(self):
        self.lock = threading.Lock()

        # ── BTC Price Feed ──
        self.current_btc_price: float = 0.0
        self.window_open_price: float = 0.0
        self.btc_delta: float = 0.0
        self.btc_ticks: collections.deque = collections.deque(maxlen=600)  # (ts, price)
        self.momentum_pct: float = 0.0

        # ── Derived Signals ──
        self.direction: str = "FLAT"          # "UP" / "DOWN" / "FLAT"
        self.direction_consistency: float = 0.0  # 0.0–1.0
        self.volatility_ratio: float = 0.0
        self.tick_count_up: int = 0
        self.tick_count_down: int = 0

        # ── Indicators (3-min confirmation) ──
        self.cvd: float = 0.0                 # Cumulative Volume Delta (normalized -1 to +1)
        self.cvd_raw: float = 0.0             # Raw CVD (buy_vol - sell_vol)
        self.buy_volume: float = 0.0          # Total buy-initiated volume this window
        self.sell_volume: float = 0.0         # Total sell-initiated volume this window
        self.ema_fast: float = 0.0            # Fast EMA on tick prices
        self.ema_slow: float = 0.0            # Slow EMA on tick prices
        self.ema_tick_count: int = 0          # Ticks processed for EMA
        self.vwap: float = 0.0               # Volume-Weighted Average Price
        self.vwap_numerator: float = 0.0      # sum(price * volume)
        self.vwap_denominator: float = 0.0    # sum(volume)
        self.indicator_score: int = 0         # Current consensus score (0-5)
        self.indicator_direction: str = "FLAT" # Consensus direction from indicators

        # ── Polymarket State ──
        self.market: Optional[dict] = None    # {yes_token, no_token, title, secs_remaining, window_start}
        self.yes_midpoint: float = 0.50
        self.no_midpoint: float = 0.50
        self.last_window_start: int = 0

        # ── Window Phase ──
        self.phase: str = PHASE_IDLE
        self.traded_this_window: bool = False

        # ── Position ──
        self.active_position: Optional[Position] = None
        self.pending_signal: Optional[Signal] = None

        # ── Risk / Session ──
        self.session_pnl: float = 0.0
        self.session_trades: list = []        # List[TradeRecord]
        self.daily_loss: float = 0.0
        self.trade_count_today: int = 0
        self.last_trade_time: float = 0.0
        self.cooldown_until: float = 0.0
        self.kill_switch_active: bool = False

        # ── System ──
        self.binance_connected: bool = False
        self.clob_connected: bool = False
        self.telegram_connected: bool = False
        self.bot_start_time: float = time.time()
        self.last_error: str = ""

        # ── Dashboard WS clients ──
        self.ws_clients: list = []

    def update_chainlink_price(self, price: float, ts: float):
        """
        Update BTC price from the Chainlink on-chain oracle.
        This is the AUTHORITATIVE price source — Polymarket settles on this.
        Updates: delta, EMA, VWAP, momentum, direction, volatility, consensus.
        """
        import config as cfg

        self.current_btc_price = price
        self.btc_ticks.append((ts, price))
        self._chainlink_last_ts = ts

        # Delta from window open
        if self.window_open_price > 0:
            self.btc_delta = price - self.window_open_price

        # ── Indicator 2: Tick EMA (Exponential Moving Average) ──
        self.ema_tick_count += 1
        if self.ema_tick_count == 1:
            self.ema_fast = price
            self.ema_slow = price
        else:
            k_fast = 2.0 / (cfg.EMA_FAST_PERIOD + 1)
            k_slow = 2.0 / (cfg.EMA_SLOW_PERIOD + 1)
            self.ema_fast = price * k_fast + self.ema_fast * (1 - k_fast)
            self.ema_slow = price * k_slow + self.ema_slow * (1 - k_slow)

        # ── Indicator 3: VWAP (use price as weight when no volume) ──
        # When Binance volume arrives, it will update CVD separately
        # For VWAP, we also accumulate with Chainlink prices using a unit weight
        self.vwap_numerator += price * price  # price-weighted
        self.vwap_denominator += price
        if self.vwap_denominator > 0:
            self.vwap = self.vwap_numerator / self.vwap_denominator

        # ── Direction consistency ──
        if len(self.btc_ticks) >= 10:
            recent = list(self.btc_ticks)[-60:]
            if len(recent) >= 2:
                ups = sum(1 for i in range(1, len(recent)) if recent[i][1] > recent[i-1][1])
                downs = sum(1 for i in range(1, len(recent)) if recent[i][1] < recent[i-1][1])
                total_moves = ups + downs
                if total_moves > 0:
                    self.tick_count_up = ups
                    self.tick_count_down = downs
                    dominant = max(ups, downs)
                    self.direction_consistency = dominant / total_moves
                    self.direction = "UP" if self.btc_delta > 0 else "DOWN" if self.btc_delta < 0 else "FLAT"

        # ── Momentum % (price change over last 10 seconds) ──
        cutoff = ts - 10
        old_ticks = [(t, p) for t, p in self.btc_ticks if t >= cutoff]
        if old_ticks:
            old_price = old_ticks[0][1]
            if old_price > 0:
                self.momentum_pct = (price - old_price) / old_price

        # ── Volatility ──
        if len(self.btc_ticks) >= 30:
            recent_prices = [p for _, p in list(self.btc_ticks)[-30:]]
            returns = []
            for i in range(1, len(recent_prices)):
                if recent_prices[i-1] > 0:
                    returns.append(abs(recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1])
            if returns:
                import statistics
                avg_ret = statistics.mean(returns)
                if avg_ret > 0 and len(returns) >= 3:
                    vol = statistics.stdev(returns)
                    self.volatility_ratio = vol / avg_ret
                else:
                    self.volatility_ratio = 0.0

        # ── Compute Indicator Consensus ──
        self._compute_indicator_consensus()

    def update_binance_volume(self, price: float, qty: float, is_buyer: bool, ts: float):
        """
        Update volume-based indicators from Binance aggTrades.
        Only updates CVD (buy/sell volume classification).
        Price is NOT used for delta — that comes from Chainlink.
        """
        # ── Indicator 1: Cumulative Volume Delta (CVD) ──
        usd_volume = price * qty
        if is_buyer:
            self.buy_volume += usd_volume
        else:
            self.sell_volume += usd_volume
        self.cvd_raw = self.buy_volume - self.sell_volume
        total_vol = self.buy_volume + self.sell_volume
        self.cvd = self.cvd_raw / total_vol if total_vol > 0 else 0.0

        # Re-compute consensus since CVD changed
        self._compute_indicator_consensus()

    def update_pyth_price(self, price: float, ts: float):
        """
        Update from Pyth Network BTC/USD feed (PRIMARY real-time price).
        Pyth updates sub-second, so this drives the live chart and indicators.
        Always updates the price pipeline (EMA, VWAP, momentum, delta).
        """
        self.pyth_price = price
        self.pyth_last_ts = ts

        # Pyth is the primary real-time price — always update indicators
        if price > 1000:
            self.current_btc_price = price
            self.btc_ticks.append((ts, price))

            import config as cfg

            # Delta from window open
            if self.window_open_price > 0:
                self.btc_delta = price - self.window_open_price

            # EMA
            self.ema_tick_count += 1
            if self.ema_tick_count <= 1:
                self.ema_fast = price
                self.ema_slow = price
            else:
                k_fast = 2.0 / (cfg.EMA_FAST_PERIOD + 1)
                k_slow = 2.0 / (cfg.EMA_SLOW_PERIOD + 1)
                self.ema_fast = price * k_fast + self.ema_fast * (1 - k_fast)
                self.ema_slow = price * k_slow + self.ema_slow * (1 - k_slow)

            # VWAP (price-weighted)
            self.vwap_numerator += price * price
            self.vwap_denominator += price
            if self.vwap_denominator > 0:
                self.vwap = self.vwap_numerator / self.vwap_denominator

            # Direction consistency
            if len(self.btc_ticks) >= 10:
                recent = list(self.btc_ticks)[-60:]
                if len(recent) >= 2:
                    ups = sum(1 for i in range(1, len(recent)) if recent[i][1] > recent[i-1][1])
                    downs = sum(1 for i in range(1, len(recent)) if recent[i][1] < recent[i-1][1])
                    total_moves = ups + downs
                    if total_moves > 0:
                        self.tick_count_up = ups
                        self.tick_count_down = downs
                        self.direction_consistency = max(ups, downs) / total_moves
                        self.direction = "UP" if self.btc_delta > 0 else "DOWN" if self.btc_delta < 0 else "FLAT"

            # Momentum
            cutoff = ts - 10
            old_ticks = [(t, p) for t, p in self.btc_ticks if t >= cutoff]
            if old_ticks and old_ticks[0][1] > 0:
                self.momentum_pct = (price - old_ticks[0][1]) / old_ticks[0][1]

            # Volatility
            if len(self.btc_ticks) >= 30:
                recent_prices = [p for _, p in list(self.btc_ticks)[-30:]]
                returns = []
                for i in range(1, len(recent_prices)):
                    if recent_prices[i-1] > 0:
                        returns.append(abs(recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1])
                if returns:
                    import statistics
                    avg_ret = statistics.mean(returns)
                    if avg_ret > 0 and len(returns) >= 3:
                        self.volatility_ratio = statistics.stdev(returns) / avg_ret
                    else:
                        self.volatility_ratio = 0.0

            self._compute_indicator_consensus()

    def update_btc_tick(self, price: float, qty: float, is_buyer: bool, ts: float):
        """Backward-compatible wrapper — updates both price and volume."""
        self.update_chainlink_price(price, ts)
        self.update_binance_volume(price, qty, is_buyer, ts)


    def _compute_indicator_consensus(self):
        """
        Score directional agreement across all indicators.
        CVD = 2pts, EMA = 1pt, VWAP = 1pt, Momentum = 1pt.
        Total possible = 5 points.
        """
        import config as cfg

        up_score = 0
        down_score = 0

        # CVD (2 points) — net buy/sell pressure
        if self.cvd > cfg.MIN_CVD_THRESHOLD:
            up_score += 2
        elif self.cvd < -cfg.MIN_CVD_THRESHOLD:
            down_score += 2

        # EMA crossover (1 point)
        if self.ema_tick_count >= cfg.EMA_SLOW_PERIOD:
            if self.ema_fast > self.ema_slow:
                up_score += 1
            elif self.ema_fast < self.ema_slow:
                down_score += 1

        # VWAP deviation (1 point) — price above/below VWAP
        if self.vwap > 0 and self.current_btc_price > 0:
            vwap_dev = (self.current_btc_price - self.vwap) / self.vwap
            if vwap_dev > cfg.MIN_VWAP_DEVIATION:
                up_score += 1
            elif vwap_dev < -cfg.MIN_VWAP_DEVIATION:
                down_score += 1

        # Momentum (1 point)
        if self.momentum_pct > 0.0001:  # > 0.01%
            up_score += 1
        elif self.momentum_pct < -0.0001:
            down_score += 1

        # Set consensus
        if up_score > down_score:
            self.indicator_score = up_score
            self.indicator_direction = "UP"
        elif down_score > up_score:
            self.indicator_score = down_score
            self.indicator_direction = "DOWN"
        else:
            self.indicator_score = max(up_score, down_score)
            self.indicator_direction = "FLAT"

    def new_window(self, window_start: int):
        """Reset state for a new 5-minute window."""
        self.last_window_start = window_start
        self.window_open_price = self.current_btc_price if self.current_btc_price > 0 else 0.0
        self.btc_delta = 0.0
        self.direction = "FLAT"
        self.direction_consistency = 0.0
        self.tick_count_up = 0
        self.tick_count_down = 0
        self.traded_this_window = False
        self.pending_signal = None
        self.phase = PHASE_WATCHING

        # Reset indicators
        self.cvd = 0.0
        self.cvd_raw = 0.0
        self.buy_volume = 0.0
        self.sell_volume = 0.0
        self.ema_fast = 0.0
        self.ema_slow = 0.0
        self.ema_tick_count = 0
        self.vwap = 0.0
        self.vwap_numerator = 0.0
        self.vwap_denominator = 0.0
        self.indicator_score = 0
        self.indicator_direction = "FLAT"

    def compute_phase(self, secs_remaining: int) -> str:
        """
        Determine current window phase based on timing.

        Phases (3-min confirmation + 2-min execution):
        WATCHING:    0–60s    — Collecting initial BTC data
        CONFIRMING:  60–180s  — Building indicator picture
        SNIPING:     180–280s — Execute if indicators confirm
        EXITING:     280–290s — Force-close before settlement
        SETTLED:     290–300s — Window ending
        """
        import config as cfg
        elapsed = 300 - secs_remaining

        if self.active_position:
            if elapsed >= cfg.EXIT_DEADLINE_SECS:
                return PHASE_EXITING
            return PHASE_HOLDING

        if self.traded_this_window:
            return PHASE_SETTLED

        if elapsed < 60:
            return PHASE_WATCHING
        elif elapsed < cfg.ENTRY_WINDOW_START:  # < 180s
            return PHASE_CONFIRMING
        elif elapsed <= cfg.ENTRY_WINDOW_END:   # 180–280s
            return PHASE_SNIPING
        elif elapsed <= cfg.EXIT_DEADLINE_SECS: # 280–290s
            return PHASE_EXITING
        else:
            return PHASE_SETTLED

    def get_snapshot(self) -> dict:
        """Return a json-serializable snapshot for dashboard/API."""
        pos_data = None
        if self.active_position:
            p = self.active_position
            unrealized = 0.0
            current_mid = self.yes_midpoint if p.direction == "UP" else self.no_midpoint
            if current_mid > 0 and p.entry_price > 0:
                unrealized = (current_mid - p.entry_price) * p.shares
            pos_data = {
                "direction": p.direction,
                "entry_price": p.entry_price,
                "shares": p.shares,
                "usd_cost": p.usd_cost,
                "unrealized_pnl": round(unrealized, 2),
                "current_mid": current_mid,
                "hold_secs": round(time.time() - p.entry_time, 1),
            }

        signal_data = None
        if self.pending_signal:
            s = self.pending_signal
            signal_data = {
                "direction": s.direction,
                "true_prob": s.true_prob,
                "market_odds": s.market_odds,
                "odds_lag": s.odds_lag,
                "btc_delta": s.btc_delta,
            }

        return {
            "btc_price": self.current_btc_price,
            "btc_delta": round(self.btc_delta, 2),
            "momentum_pct": round(self.momentum_pct, 6),
            "direction": self.direction,
            "consistency": round(self.direction_consistency, 2),
            "volatility_ratio": round(self.volatility_ratio, 2),
            "phase": self.phase,
            "yes_mid": self.yes_midpoint,
            "no_mid": self.no_midpoint,
            "indicators": {
                "cvd": round(self.cvd, 4),
                "cvd_raw": round(self.cvd_raw, 2),
                "buy_volume": round(self.buy_volume, 2),
                "sell_volume": round(self.sell_volume, 2),
                "ema_fast": round(self.ema_fast, 2),
                "ema_slow": round(self.ema_slow, 2),
                "vwap": round(self.vwap, 2),
                "vwap_deviation": round(
                    (self.current_btc_price - self.vwap) / self.vwap, 6
                ) if self.vwap > 0 else 0.0,
                "score": self.indicator_score,
                "direction": self.indicator_direction,
            },
            "position": pos_data,
            "signal": signal_data,
            "session_pnl": round(self.session_pnl, 2),
            "trade_count": self.trade_count_today,
            "daily_loss": round(self.daily_loss, 2),
            "kill_switch": self.kill_switch_active,
            "market": {
                "title": self.market.get("title", "—") if self.market else "No Market",
                "secs_remaining": self.market.get("secs_remaining", 0) if self.market else 0,
                "window_start": self.last_window_start,
            },
            "system": {
                "binance": self.binance_connected,
                "clob": self.clob_connected,
                "telegram": self.telegram_connected,
                "uptime": round(time.time() - self.bot_start_time, 0),
                "last_error": self.last_error,
            },
            "trades": [
                {
                    "id": t.trade_id,
                    "ts": t.timestamp,
                    "dir": t.direction,
                    "entry": t.entry_price,
                    "pnl": t.pnl,
                    "outcome": t.outcome,
                    "reason": t.exit_reason,
                }
                for t in self.session_trades[-50:]  # Last 50 trades
            ],
        }


# Global singleton
ctx = TradingContext()
