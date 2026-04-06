"""
Confirmation Sniper — Signal Detector

Pure logic module. No I/O, no network calls.
Reads from TradingContext, emits Signal objects.
"""

import logging
import time

import config
from modules.context import Signal, ctx, PHASE_SNIPING

logger = logging.getLogger("SignalDetector")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# True Probability Model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_true_probability(btc_delta: float) -> float:
    """
    Map absolute BTC delta (in USD) to probability of direction persistence.

    Based on the empirical observation: large BTC moves in the first 2.5 min
    of a 5-min window almost always persist to settlement.

    Uses linear interpolation between table entries for smooth output.
    """
    abs_delta = abs(btc_delta)
    table = config.PROB_TABLE

    # Below minimum
    if abs_delta <= table[0][0]:
        return table[0][1]

    # Above maximum
    if abs_delta >= table[-1][0]:
        return table[-1][1]

    # Linear interpolation between entries
    for i in range(1, len(table)):
        if abs_delta <= table[i][0]:
            d_lo, p_lo = table[i - 1]
            d_hi, p_hi = table[i]
            t = (abs_delta - d_lo) / (d_hi - d_lo)
            return p_lo + t * (p_hi - p_lo)

    return table[-1][1]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Signal Evaluation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_last_gate_log = 0  # throttle gate logging


def evaluate_signal() -> Signal | None:
    """
    Evaluate whether current market conditions warrant an entry signal.

    Strategy: 3-min confirmation + 2-min execution.
    - First 180s: Build directional picture using indicators (CVD, EMA, VWAP, Momentum)
    - 180-280s: Execute if indicators confirm direction with sufficient consensus

    Returns a Signal object if all gates pass, or None.
    """
    global _last_gate_log

    # No market discovered
    if not ctx.market:
        return None

    secs_remaining = ctx.market.get("secs_remaining", 300)
    elapsed = 300 - secs_remaining

    # ── Gate 1: Timing (last 2 minutes only) ──
    if elapsed < config.ENTRY_WINDOW_START or elapsed > config.ENTRY_WINDOW_END:
        return None

    # ── Gate 2: Already traded this window ──
    if ctx.traded_this_window:
        return None

    # Compute all values for logging
    delta = ctx.btc_delta
    abs_delta = abs(delta)
    consistency = ctx.direction_consistency
    vol_ratio = ctx.volatility_ratio

    # Indicator values
    cvd = ctx.cvd
    ema_fast = ctx.ema_fast
    ema_slow = ctx.ema_slow
    vwap = ctx.vwap
    vwap_dev = (ctx.current_btc_price - vwap) / vwap if vwap > 0 else 0.0
    momentum = ctx.momentum_pct
    ind_score = ctx.indicator_score
    ind_dir = ctx.indicator_direction

    # Use indicator direction for trade direction
    direction = ind_dir if ind_dir != "FLAT" else ("UP" if delta > 0 else "DOWN")
    market_odds = ctx.yes_midpoint if direction == "UP" else ctx.no_midpoint
    true_prob = compute_true_probability(delta)
    odds_lag = true_prob - market_odds

    # Verbose gate log every 5 seconds
    now = time.time()
    if now - _last_gate_log >= 5:
        _last_gate_log = now
        g3 = "✓" if abs_delta >= config.MIN_DELTA_USD else "✗"
        g4 = "✓" if consistency >= config.MIN_DIRECTION_CONSISTENCY else "✗"
        g5 = "✓" if vol_ratio <= config.MAX_VOL_RATIO else "✗"
        g6 = "✓" if ind_score >= config.MIN_INDICATOR_SCORE else "✗"
        g8 = "✓" if odds_lag >= config.MIN_ODDS_LAG else "✗"
        g9 = "✓" if config.MIN_ENTRY_PRICE <= market_odds <= config.MAX_ENTRY_PRICE else "✗"

        ema_arrow = "↑" if ema_fast > ema_slow else "↓"

        logger.info(
            f"📋 Gates [{elapsed:.0f}s] {direction} | "
            f"Δ${delta:+.0f} {g3} | "
            f"Cons:{consistency:.0%} {g4} | "
            f"Vol:{vol_ratio:.1f} {g5} | "
            f"CVD:{cvd:+.3f} EMA:{ema_arrow} VWAP:{vwap_dev:+.4%} Mom:{momentum:+.4%} "
            f"Score:{ind_score}/5→{ind_dir} {g6} | "
            f"Lag:{odds_lag:.0%} {g8} | "
            f"Price:{market_odds:.2f} {g9}"
        )

    # ── Gate 3: BTC delta magnitude (lowered to $5) ──
    if abs_delta < config.MIN_DELTA_USD:
        return None

    # ── Gate 4: Direction consistency ──
    if consistency < config.MIN_DIRECTION_CONSISTENCY:
        return None

    # ── Gate 5: Volatility filter ──
    if vol_ratio > config.MAX_VOL_RATIO:
        return None

    # ── Gate 6: Indicator consensus (NEW — core of the strategy) ──
    if ind_score < config.MIN_INDICATOR_SCORE:
        return None

    # ── Gate 7: Indicator direction must be clear ──
    if ind_dir == "FLAT":
        return None

    # Use indicator-confirmed direction
    direction = ind_dir
    market_odds = ctx.yes_midpoint if direction == "UP" else ctx.no_midpoint
    true_prob = compute_true_probability(delta)
    odds_lag = true_prob - market_odds

    # ── Gate 8: Odds lag detection ──
    if odds_lag < config.MIN_ODDS_LAG:
        return None

    # ── Gate 9: Entry price bounds ──
    if market_odds > config.MAX_ENTRY_PRICE:
        return None
    if market_odds < config.MIN_ENTRY_PRICE:
        return None

    # ── All gates passed — emit signal ──
    signal = Signal(
        direction=direction,
        true_prob=round(true_prob, 4),
        market_odds=round(market_odds, 4),
        odds_lag=round(odds_lag, 4),
        btc_delta=round(ctx.btc_delta, 2),
        entry_price=round(market_odds, 4),
    )

    logger.info(
        f"🎯 SIGNAL TRIGGERED: {direction} | "
        f"BTC Δ=${ctx.btc_delta:+.0f} | "
        f"Indicators: CVD={cvd:+.3f} EMA={'↑' if ema_fast > ema_slow else '↓'} "
        f"VWAP={vwap_dev:+.4%} Mom={momentum:+.4%} → Score {ind_score}/5 | "
        f"TrueProb={true_prob:.0%} vs Market={market_odds:.0%} Lag={odds_lag:.0%} | "
        f"Entry={market_odds:.2f}¢"
    )

    return signal


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Reversal Detection (for active positions)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_reversal() -> bool:
    """
    Check if BTC has reversed against our active position.
    Returns True if we should emergency exit.
    """
    if not ctx.active_position:
        return False

    pos = ctx.active_position

    # Check momentum reversal
    if pos.direction == "UP" and ctx.momentum_pct < -config.REVERSAL_THRESHOLD:
        logger.warning(f"📉 REVERSAL detected! Momentum {ctx.momentum_pct:.4%} against UP position")
        return True

    if pos.direction == "DOWN" and ctx.momentum_pct > config.REVERSAL_THRESHOLD:
        logger.warning(f"📈 REVERSAL detected! Momentum {ctx.momentum_pct:.4%} against DOWN position")
        return True

    return False


def check_time_exit() -> bool:
    """Check if we need to exit based on time deadline."""
    if not ctx.active_position or not ctx.market:
        return False

    secs_remaining = ctx.market.get("secs_remaining", 300)
    elapsed = 300 - secs_remaining

    if elapsed >= config.EXIT_DEADLINE_SECS:
        logger.warning(f"⏰ TIME EXIT: {secs_remaining}s remaining, closing position")
        return True

    return False
