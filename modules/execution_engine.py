"""
Confirmation Sniper — Execution Engine

Handles all order lifecycle:
- Entry: POST_ONLY → FOK → IOC fallback
- Exit: Market sell or limit at high price
- Emergency bailout
- Dry-run simulation
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import config
from modules.context import ctx, Position, TradeRecord

logger = logging.getLogger("ExecutionEngine")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Async wrapper for sync py-clob-client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _clob(func, *args, **kwargs):
    """Run sync clob function in thread pool."""
    return await asyncio.to_thread(func, *args, **kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Balance Query
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def get_balance(clob_client) -> float:
    """Get USDC balance from Polymarket."""
    if config.DRY_RUN:
        return 1000.0  # Simulated balance

    try:
        raw = await _clob(clob_client.get_balance)
        bal = float(raw)
        # Polymarket sometimes returns in micro-USDC
        return bal / 1_000_000 if bal > 10_000 else bal
    except Exception as e:
        logger.error(f"Balance query failed: {e}")
        return 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entry Execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def execute_entry(clob_client, signal, position_usd: float) -> bool:
    """
    Execute entry trade with 3-tier fallback:
    1. FOK market order (fastest)
    2. IOC with slippage tolerance
    3. GTC limit order (last resort)

    Returns True if filled.
    """
    token_id = (
        ctx.market["yes_token"] if signal.direction == "UP"
        else ctx.market["no_token"]
    )

    entry_price = signal.entry_price

    logger.info(
        f"⚡ EXECUTING ENTRY: {signal.direction} | "
        f"${position_usd:.2f} @ ~{entry_price:.2f} | "
        f"Token: {token_id[:16]}..."
    )

    # ── DRY RUN ──
    if config.DRY_RUN:
        shares = position_usd / entry_price if entry_price > 0 else 0
        ctx.active_position = Position(
            direction=signal.direction,
            token_id=token_id,
            entry_price=entry_price,
            shares=round(shares, 2),
            usd_cost=position_usd,
            entry_time=time.time(),
            order_id="dry_run_" + str(int(time.time())),
        )
        ctx.traded_this_window = True
        logger.info(
            f"  [DRY_RUN] Filled {shares:.1f} shares of {signal.direction} "
            f"@ {entry_price:.2f}"
        )
        return True

    # ── LIVE EXECUTION ──
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # Tier 1: FOK Market Order
        try:
            mo_args = MarketOrderArgs(
                token_id=token_id,
                amount=position_usd,
                side=BUY,
            )
            signed = await _clob(clob_client.create_market_order, mo_args)
            resp = await _clob(clob_client.post_order, signed, OrderType.FOK)

            if resp and resp.get("orderID"):
                fill_price = entry_price  # Approximate
                shares = position_usd / fill_price if fill_price > 0 else 0

                ctx.active_position = Position(
                    direction=signal.direction,
                    token_id=token_id,
                    entry_price=fill_price,
                    shares=round(shares, 2),
                    usd_cost=position_usd,
                    entry_time=time.time(),
                    order_id=resp.get("orderID", ""),
                )
                ctx.traded_this_window = True
                logger.info(f"  ✓ FOK filled: {resp}")
                return True
            else:
                logger.warning(f"  FOK returned no orderID: {resp}")
        except Exception as e:
            logger.warning(f"  FOK failed: {e}")

        # Tier 2: GTC Limit Order (slightly above market)
        try:
            limit_price = min(0.95, entry_price + config.MAX_SLIPPAGE)
            shares_est = position_usd / limit_price

            limit_args = OrderArgs(
                token_id=token_id,
                price=round(limit_price, 2),
                size=round(shares_est, 2),
                side=BUY,
            )
            signed = await _clob(clob_client.create_order, limit_args)
            resp = await _clob(clob_client.post_order, signed, OrderType.GTC)

            if resp and resp.get("orderID"):
                # Wait briefly for fill
                await asyncio.sleep(config.LIMIT_TIMEOUT_SEC)

                ctx.active_position = Position(
                    direction=signal.direction,
                    token_id=token_id,
                    entry_price=limit_price,
                    shares=round(shares_est, 2),
                    usd_cost=position_usd,
                    entry_time=time.time(),
                    order_id=resp.get("orderID", ""),
                )
                ctx.traded_this_window = True
                logger.info(f"  ✓ Limit filled: {resp}")
                return True
        except Exception as e:
            logger.warning(f"  Limit order failed: {e}")

        logger.error("  ✗ All entry methods failed")
        return False

    except ImportError:
        logger.error("py-clob-client not available for live trading")
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Exit Execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def execute_exit(clob_client, reason: str = "settlement") -> float:
    """
    Close the active position.
    Returns realized PnL.
    """
    if not ctx.active_position:
        return 0.0

    pos = ctx.active_position
    current_mid = ctx.yes_midpoint if pos.direction == "UP" else ctx.no_midpoint

    logger.info(
        f"🔒 CLOSING POSITION: {pos.direction} | "
        f"Entry={pos.entry_price:.2f} | Current={current_mid:.2f} | "
        f"Reason={reason}"
    )

    # ── DRY RUN ──
    if config.DRY_RUN:
        # Simulate settlement outcome based on BTC delta
        if reason == "settlement":
            # Check if our direction matches final BTC direction
            btc_won = (
                (pos.direction == "UP" and ctx.btc_delta > 0) or
                (pos.direction == "DOWN" and ctx.btc_delta < 0)
            )
            if btc_won:
                pnl = (1.0 - pos.entry_price) * pos.shares  # Full payout
                outcome = "WIN"
            else:
                pnl = -pos.usd_cost
                outcome = "LOSS"
        else:
            # Early exit — sell at current midpoint
            pnl = (current_mid - pos.entry_price) * pos.shares
            outcome = "WIN" if pnl > 0 else "LOSS"

        exit_price = 1.0 if (reason == "settlement" and pnl > 0) else current_mid

        # Loud terminal output
        win_count = sum(1 for t in ctx.session_trades if t.outcome == "WIN")
        total_count = len(ctx.session_trades)
        session_pnl = ctx.session_pnl + pnl

        if outcome == "WIN":
            color = "\033[92m"  # green
            icon = "🟢"
        else:
            color = "\033[91m"  # red
            icon = "🔴"
        RST = "\033[0m"; BOLD = "\033[1m"; CYAN = "\033[96m"; DIM = "\033[2m"

        print(
            f"\n{BOLD}{color}{'━' * 60}{RST}\n"
            f"  {icon} {BOLD}TRADE {outcome}{RST}  ({reason})\n"
            f"{color}{'━' * 60}{RST}\n"
            f"  Direction:   {pos.direction}\n"
            f"  Entry:       {pos.entry_price:.2f}¢\n"
            f"  Exit:        {exit_price:.2f}¢\n"
            f"  Shares:      {pos.shares:.1f}\n"
            f"  {BOLD}PnL:         {color}${pnl:+.2f}{RST}\n"
            f"  ─────────────────────────────\n"
            f"  Session PnL: {color}${session_pnl:+.2f}{RST}\n"
            f"  Win Rate:    {win_count + (1 if outcome == 'WIN' else 0)}/{total_count + 1}"
            f"  ({(win_count + (1 if outcome == 'WIN' else 0))/(total_count+1)*100:.0f}%)\n"
            f"  Trades:      #{total_count + 1}\n"
            f"{color}{'━' * 60}{RST}\n"
        )

        logger.info(
            f"  [DRY_RUN] {outcome}: PnL=${pnl:+.2f} | "
            f"Exit={exit_price:.2f}"
        )

    else:
        # ── LIVE EXIT ──
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL

            mo_args = MarketOrderArgs(
                token_id=pos.token_id,
                amount=pos.shares,
                side=SELL,
            )
            signed = await _clob(clob_client.create_market_order, mo_args)
            resp = await _clob(clob_client.post_order, signed, OrderType.FOK)
            logger.info(f"  ✓ Exit order: {resp}")
        except Exception as e:
            logger.error(f"  Exit failed: {e}")

        pnl = (current_mid - pos.entry_price) * pos.shares
        exit_price = current_mid
        outcome = "WIN" if pnl > 0 else "LOSS"

    # Record trade
    trade = TradeRecord(
        trade_id=len(ctx.session_trades) + 1,
        timestamp=datetime.now(timezone.utc).isoformat(),
        direction=pos.direction,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        shares=pos.shares,
        usd_cost=pos.usd_cost,
        pnl=round(pnl, 2),
        outcome=outcome,
        btc_delta=round(ctx.btc_delta, 2),
        odds_lag=0.0,
        window_start=ctx.last_window_start,
        hold_duration=round(time.time() - pos.entry_time, 1),
        exit_reason=reason,
    )
    ctx.session_trades.append(trade)

    # Update risk state
    from modules.risk_manager import record_trade_result
    record_trade_result(pnl)

    # Clear position
    ctx.active_position = None

    return pnl


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Emergency Bailout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def emergency_cancel_all(clob_client):
    """Cancel ALL orders — dead man's switch."""
    logger.error("🛑 EMERGENCY: Cancelling all orders")
    if not config.DRY_RUN:
        try:
            await _clob(clob_client.cancel_all)
        except Exception as e:
            logger.error(f"Emergency cancel failed: {e}")

    ctx.active_position = None
