"""
EV Gap Trading Strategy for Polymarket 5-Min BTC Markets

Core logic:
  Model Probability  = derived from current spot data (candles 3-4)
  Market Probability = lagged odds (candles 1-2, simulating market delay)
  EV Gap             = Model Prob - Market Prob
  Trade if           EV Gap ≥ 10%

Strict filters:
  - No trade if EV Gap < 10%
  - No trade if market already moved significantly (>75%)
  - No trade if direction is inconsistent
  - No trade if signal is late (too little time remaining)
"""

import statistics
import config


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Model Probability Estimator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def estimate_true_probability(oracle_open: float, candles: list) -> dict:
    """
    Estimate the TRUE probability that BTC will close above oracle_open,
    based on current spot data trailing up to the last known candle.

    Uses:
      1. Spot delta magnitude → base probability shift
      2. Momentum (cumulative direction) → confidence boost
      3. Volatility → uncertainty penalty

    Returns: {'prob_up': float, 'prob_down': float, 'direction': str, ...}
    """
    if len(candles) < 4:
        return {
            "prob_up": 0.50, "prob_down": 0.50, "direction": "FLAT",
            "spot_delta": 0.0, "momentum_score": 0.0,
        }

    # Current spot (absolute latest candle close)
    spot_price = candles[-1]["close"]
    spot_delta = spot_price - oracle_open

    # ── 1. Base probability from spot delta ──────────
    magnitude = min(abs(spot_delta) / config.PRICE_NORMALIZATION_USD, 1.0)
    base_prob = 0.50 + (0.45 * magnitude)  # 0.50 → 0.95

    # ── 2. Momentum: cumulative direction consistency ──
    cum_deltas = []
    # Trailing 4 minutes leading up to now
    for c in candles[-4:]:
        cum_deltas.append(c["close"] - oracle_open)

    positive = sum(1 for d in cum_deltas if d > 0)
    negative = sum(1 for d in cum_deltas if d < 0)
    dominant = max(positive, negative)
    total = len(cum_deltas)

    if total > 0:
        consistency_ratio = dominant / total
        momentum_boost = max(0, (consistency_ratio - 0.50) * 0.10)
    else:
        momentum_boost = 0.0
        consistency_ratio = 0.0

    # ── 3. Volatility penalty ─────────────────────────
    candle_ranges = []
    for c in candles[-4:]:
        if c["open"] > 0:
            candle_ranges.append(abs(c["close"] - c["open"]) / c["open"])

    vol_penalty = 0.0
    if len(candle_ranges) >= 2:
        vol = statistics.stdev(candle_ranges)
        avg = statistics.mean(candle_ranges)
        if avg > 0 and vol / avg > config.MAX_VOLATILITY_MULTIPLIER:
            vol_penalty = 0.05  # High volatility reduces confidence

    # ── Combine ───────────────────────────────────────
    model_prob = base_prob + momentum_boost - vol_penalty
    model_prob = max(0.02, min(0.98, model_prob))

    direction = "UP" if spot_delta >= 0 else "DOWN"
    prob_up = model_prob if direction == "UP" else (1.0 - model_prob)
    prob_down = 1.0 - prob_up

    return {
        "prob_up": round(prob_up, 4),
        "prob_down": round(prob_down, 4),
        "direction": direction,
        "spot_delta": round(spot_delta, 2),
        "momentum_score": round(consistency_ratio, 2),
        "momentum_boost": round(momentum_boost, 4),
        "vol_penalty": round(vol_penalty, 4),
        "base_prob": round(base_prob, 4),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Market Implied Probability (lagged)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_market_implied_probability(oracle_open: float, lagged_price: float) -> dict:
    """
    Derive the market's implied UP probability from a LAGGED price.
    This simulates the ~2-minute delay in Polymarket odds updating.

    The lagged_price is candle 2 close (~T+120s), while our model
    uses candle 4 close (~T+240s).

    Returns: {'market_prob_up': float, 'market_prob_down': float}
    """
    lagged_delta = lagged_price - oracle_open
    market_prob_up = 0.50 + (lagged_delta * config.SIM_ODDS_SENSITIVITY)
    market_prob_up = max(0.02, min(0.98, market_prob_up))

    return {
        "market_prob_up": round(market_prob_up, 4),
        "market_prob_down": round(1.0 - market_prob_up, 4),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EV Gap Decision Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def evaluate_ev_gap(window: dict) -> dict:
    """
    Full EV gap evaluation for one 5-minute window.

    Steps:
      1. Convert market price → implied probability (lagged)
      2. Compute model probability (current spot)
      3. Calculate EV Gap = |Model Prob - Market Prob|
      4. Apply strict filters
      5. Return decision

    Returns dict with:
      action, direction, ev_gap, confidence, model_prob, market_prob,
      entry_price, reason
    """
    candles = window["candles"]
    oracle_open = window["open_price"]

    # ── Step 1: Model probability (simulate entry at T+240s) ─────
    # We pass only the first 4 candles to prevent the backtester from seeing the final wrap
    model = estimate_true_probability(oracle_open, candles[:4])

    # ── Step 2: Market probability (lagged data) ─────
    if len(candles) >= 2:
        lagged_price = candles[1]["close"]  # Candle 2 close (~T+120s)
    else:
        lagged_price = oracle_open

    market = get_market_implied_probability(oracle_open, lagged_price)

    # ── Step 3: Determine direction and calculate gap ─
    direction = model["direction"]

    if direction == "UP":
        model_prob = model["prob_up"]
        market_prob = market["market_prob_up"]
    else:
        model_prob = model["prob_down"]
        market_prob = market["market_prob_down"]

    ev_gap = model_prob - market_prob

    # ── Step 4: Strict filters ───────────────────────

    # Filter 1: Minimum spot delta
    if abs(model["spot_delta"]) < config.MIN_SPOT_DELTA_USD:
        return _no_trade(
            ev_gap, model_prob, market_prob, direction,
            f"Delta ${model['spot_delta']:+.2f} < ${config.MIN_SPOT_DELTA_USD} minimum"
        )

    # Filter 2: Direction consistency (need ≥3/4)
    cum_deltas = [c["close"] - oracle_open for c in candles[:4]]
    if direction == "UP":
        consistent = sum(1 for d in cum_deltas if d > 0)
    else:
        consistent = sum(1 for d in cum_deltas if d < 0)

    if consistent < config.MIN_DIRECTION_CONSISTENCY:
        return _no_trade(
            ev_gap, model_prob, market_prob, direction,
            f"Direction inconsistent: {consistent}/4 (need ≥{config.MIN_DIRECTION_CONSISTENCY})"
        )

    # Filter 3: EV Gap minimum (THE KEY RULE)
    min_gap = getattr(config, "EV_MIN_GAP", 0.10)
    if ev_gap < min_gap:
        return _no_trade(
            ev_gap, model_prob, market_prob, direction,
            f"EV Gap {ev_gap:.1%} < {min_gap:.0%} minimum"
        )

    # Filter 4: Market already moved too much (late signal)
    max_market = getattr(config, "EV_MAX_MARKET_PROB", 0.75)
    if market_prob > max_market:
        return _no_trade(
            ev_gap, model_prob, market_prob, direction,
            f"Market already at {market_prob:.0%} > {max_market:.0%} (late signal)"
        )

    # Filter 5: Volatility (high uncertainty)
    candle_returns = []
    for c in candles[:4]:
        if c["open"] > 0:
            candle_returns.append(abs(c["close"] - c["open"]) / c["open"])
    if len(candle_returns) >= 2:
        vol = statistics.stdev(candle_returns)
        avg_ret = statistics.mean(candle_returns)
        if avg_ret > 0 and vol / avg_ret > config.MAX_VOLATILITY_MULTIPLIER:
            return _no_trade(
                ev_gap, model_prob, market_prob, direction,
                f"High uncertainty: vol/avg = {vol/avg_ret:.1f}×"
            )

    # ── Step 5: TRADE ────────────────────────────────
    # Confidence: scale 0-100 based on gap size
    confidence = min(100, int(ev_gap * 500))  # 10% gap → 50, 20% → 100

    # Entry price: market odds + half spread
    spread = config.SIM_SPREAD
    if direction == "UP":
        entry_price = market["market_prob_up"] + spread / 2
    else:
        entry_price = market["market_prob_down"] + spread / 2
    entry_price = max(0.05, min(0.95, entry_price))

    return {
        "action": f"BUY_{direction}",
        "direction": direction,
        "ev_gap": round(ev_gap, 4),
        "model_prob": round(model_prob, 4),
        "market_prob": round(market_prob, 4),
        "confidence": confidence,
        "entry_price": round(entry_price, 4),
        "spot_delta": model["spot_delta"],
        "model_details": model,
        "market_details": market,
        "reason": (
            f"ENTER {direction} | EV Gap={ev_gap:.1%} | "
            f"Model={model_prob:.0%} vs Market={market_prob:.0%} | "
            f"Δ=${model['spot_delta']:+.0f} | Conf={confidence}%"
        ),
    }


def _no_trade(ev_gap, model_prob, market_prob, direction, reason):
    """Helper for NO_TRADE decisions."""
    return {
        "action": "NO_TRADE",
        "direction": direction,
        "ev_gap": round(ev_gap, 4),
        "model_prob": round(model_prob, 4),
        "market_prob": round(market_prob, 4),
        "confidence": 0,
        "entry_price": 0.0,
        "spot_delta": 0.0,
        "reason": reason,
    }
