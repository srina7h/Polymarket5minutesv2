"""
Confirmation Sniper — Risk Manager

Hard rules enforced before and during every trade.
No overrides — if risk says no, no trade happens.
"""

import logging
import time
from datetime import datetime, timezone

import config
from modules.context import ctx

logger = logging.getLogger("RiskManager")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pre-Trade Risk Checks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def can_trade() -> tuple[bool, str]:
    """
    Check all risk conditions before allowing a trade.
    Returns (allowed: bool, reason: str).
    """

    # Kill switch
    if ctx.kill_switch_active:
        return False, "Kill switch active — daily loss limit hit"

    # Max positions
    if ctx.active_position is not None:
        return False, "Already have an active position"

    # Already traded this window
    if ctx.traded_this_window:
        return False, "Already traded this window"

    # Max daily trades
    if ctx.trade_count_today >= config.MAX_TRADES_PER_DAY:
        return False, f"Daily trade limit ({config.MAX_TRADES_PER_DAY}) reached"

    # Cooldown
    now = time.time()
    if now < ctx.cooldown_until:
        remaining = int(ctx.cooldown_until - now)
        return False, f"Cooldown active ({remaining}s remaining)"

    # Daily loss check
    if ctx.daily_loss >= config.MAX_DAILY_LOSS:
        ctx.kill_switch_active = True
        logger.error(f"🛑 KILL SWITCH: Daily loss ${ctx.daily_loss:.2f} >= ${config.MAX_DAILY_LOSS}")
        return False, "Kill switch activated — max daily loss exceeded"

    # Consecutive losses
    recent_trades = ctx.session_trades[-config.MAX_CONSECUTIVE_LOSSES:]
    if len(recent_trades) >= config.MAX_CONSECUTIVE_LOSSES:
        all_losses = all(t.outcome == "LOSS" for t in recent_trades)
        if all_losses:
            ctx.cooldown_until = now + config.LOSS_COOLDOWN_SEC
            logger.warning(
                f"⚠️ {config.MAX_CONSECUTIVE_LOSSES} consecutive losses. "
                f"Cooldown {config.LOSS_COOLDOWN_SEC}s"
            )
            return False, f"Consecutive loss cooldown ({config.LOSS_COOLDOWN_SEC}s)"

    # No market available
    if not ctx.market:
        return False, "No active market"

    return True, "OK"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Position Sizing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_position_size(odds_lag: float, balance: float) -> float:
    """
    Determine position size in USD.
    Uses configured flat size, capped by available balance.
    Optionally scales up for high-conviction signals.
    """
    base_size = config.TRADE_SIZE_USD

    # Scale up for very high conviction (>15% odds lag)
    if odds_lag >= 0.20:
        size = base_size * 2.0
    elif odds_lag >= 0.15:
        size = base_size * 1.5
    else:
        size = base_size

    # Cap to available balance (leave $1 buffer)
    max_from_balance = max(0, balance - 1.0)
    size = min(size, max_from_balance)

    # Minimum viable order
    if size < 1.0:
        return 0.0

    return round(size, 2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Post-Trade Accounting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def record_trade_result(pnl: float):
    """Update risk state after a trade settles."""
    if pnl < 0:
        ctx.daily_loss += abs(pnl)

    ctx.session_pnl += pnl
    ctx.trade_count_today += 1
    ctx.traded_this_window = True
    ctx.last_trade_time = time.time()
    ctx.cooldown_until = time.time() + config.COOLDOWN_SEC

    # Check kill switch
    if ctx.daily_loss >= config.MAX_DAILY_LOSS:
        ctx.kill_switch_active = True
        logger.error(f"🛑 KILL SWITCH activated. Daily loss: ${ctx.daily_loss:.2f}")


def reset_daily_stats():
    """Reset daily counters. Call at midnight UTC."""
    ctx.daily_loss = 0.0
    ctx.trade_count_today = 0
    ctx.kill_switch_active = False
    logger.info("📅 Daily stats reset")
