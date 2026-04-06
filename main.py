#!/usr/bin/env python3
"""
Confirmation Sniper — Main Orchestrator

Entry point for the Polymarket BTC 5-Min trading system.
Coordinates all modules via asyncio task management.

Usage:
    python main.py
"""

import asyncio
import logging
import os
import socket
import sys
import time

import config
from modules.context import ctx, PHASE_SNIPING, PHASE_HOLDING, PHASE_EXITING


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DNS Fix: Google DNS refuses Polymarket domains.
# Patch resolution to use Cloudflare (1.1.1.1).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_original_getaddrinfo = socket.getaddrinfo

def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Use Cloudflare DNS for Polymarket domains that Google DNS refuses."""
    POLYMARKET_DOMAINS = ['clob.polymarket.com', 'gamma-api.polymarket.com', 'data-api.polymarket.com']
    if host in POLYMARKET_DOMAINS:
        try:
            import subprocess
            result = subprocess.run(
                ['dig', '+short', host, '@1.1.1.1'],
                capture_output=True, text=True, timeout=5
            )
            ips = [line.strip() for line in result.stdout.strip().split('\n') if line.strip() and not line.strip().startswith(';')]
            if ips:
                ip = ips[0]
                # Return IPv4 result
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
        except Exception:
            pass
    return _original_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = _patched_getaddrinfo

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Logging Setup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Main")

# Quiet noisy libs
for lib in ["httpx", "requests", "urllib3", "websockets", "aiohttp"]:
    logging.getLogger(lib).setLevel(logging.WARNING)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANSI Colors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
    CYAN = "\033[96m"; MAGENTA = "\033[95m"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLOB Client Init
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def init_clob_client():
    """Initialize and authenticate the Polymarket CLOB client."""
    if not config.PRIVATE_KEY:
        logger.error("❌ Missing POLYMARKET_PRIVATE_KEY in .env")
        sys.exit(1)

    # Ensure 0x prefix on private key
    pk = config.PRIVATE_KEY
    if not pk.startswith("0x"):
        pk = "0x" + pk

    try:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            config.CLOB_HOST,
            key=pk,
            chain_id=config.CHAIN_ID,
            signature_type=config.SIGNATURE_TYPE,
            funder=config.FUNDER_ADDRESS if config.FUNDER_ADDRESS else None,
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        if client.get_ok() != "OK":
            raise Exception("CLOB server not OK")

        ctx.clob_connected = True
        logger.info("✓ Polymarket CLOB connected")
        return client

    except ImportError:
        logger.warning("⚠️ py-clob-client not installed. Running in view-only mode.")
        return None
    except Exception as e:
        logger.error(f"❌ CLOB auth failed: {e}")
        if not config.DRY_RUN:
            sys.exit(1)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Signal Loop (Main Trading Logic)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def signal_loop_task(clob_client):
    """
    Core trading loop. Runs every 1 second.
    
    State machine:
    WATCHING → CONFIRMING → SNIPING → HOLDING → EXITING → SETTLED
    """
    from modules.signal_detector import evaluate_signal, check_reversal, check_time_exit
    from modules.execution_engine import execute_entry, execute_exit, get_balance
    from modules.risk_manager import can_trade, compute_position_size
    from modules import notifier

    logger.info("Starting signal loop...")

    while True:
        try:
            # ── Phase: SNIPING — look for entry ──
            if ctx.phase == PHASE_SNIPING and not ctx.active_position:
                signal = evaluate_signal()

                if signal:
                    # Risk check
                    allowed, reason = can_trade()
                    if not allowed:
                        logger.info(f"⚠️  Signal blocked by risk: {reason}")
                    else:
                        # Get balance and compute size
                        balance = await get_balance(clob_client)
                        size = compute_position_size(signal.odds_lag, balance)

                        if size > 0:
                            ctx.pending_signal = signal

                            # Notify signal
                            await notifier.notify_signal_detected(signal)

                            # Execute entry
                            success = await execute_entry(clob_client, signal, size)

                            if success and ctx.active_position:
                                shares = ctx.active_position.shares
                                await notifier.notify_trade_executed(signal, size, shares)
                                print(
                                    f"\n{C.BOLD}{C.CYAN}{'─' * 60}{C.RESET}\n"
                                    f"  {C.BOLD}📥 TRADE OPENED{C.RESET}\n"
                                    f"  Direction: {C.GREEN if signal.direction == 'UP' else C.RED}"
                                    f"{signal.direction}{C.RESET}\n"
                                    f"  Entry:     {signal.entry_price:.2f}¢\n"
                                    f"  Size:      ${size:.2f} ({shares:.1f} shares)\n"
                                    f"  BTC Δ:     ${signal.btc_delta:+.0f}\n"
                                    f"  Edge:      {signal.odds_lag:.1%} lag\n"
                                    f"{C.CYAN}{'─' * 60}{C.RESET}\n"
                                )
                            else:
                                logger.info("Entry failed — no fill")
                                ctx.pending_signal = None
                        else:
                            logger.info(f"Position size = 0 (balance: ${balance:.2f})")

            # ── Phase: HOLDING — monitor position ──
            elif ctx.phase == PHASE_HOLDING and ctx.active_position:
                # Check for reversal exit
                if check_reversal():
                    pnl = await execute_exit(clob_client, reason="reversal")
                    await notifier.notify_emergency_exit(f"BTC reversal (PnL: ${pnl:+.2f})")
                    if ctx.session_trades:
                        await notifier.notify_trade_settled(ctx.session_trades[-1])

            # ── Phase: EXITING — close position before deadline ──
            elif ctx.phase == PHASE_EXITING and ctx.active_position:
                if check_time_exit():
                    pnl = await execute_exit(clob_client, reason="timeout")
                    if ctx.session_trades:
                        await notifier.notify_trade_settled(ctx.session_trades[-1])

            # ── Phase: SETTLED — window ended, settle any remaining position ──
            elif ctx.phase == "SETTLED" and ctx.active_position:
                pnl = await execute_exit(clob_client, reason="settlement")
                if ctx.session_trades:
                    await notifier.notify_trade_settled(ctx.session_trades[-1])

            # ── Kill switch check ──
            if ctx.kill_switch_active and not ctx._kill_notified:
                await notifier.notify_kill_switch()
                ctx._kill_notified = True

        except Exception as e:
            logger.error(f"Signal loop error: {e}", exc_info=True)
            ctx.last_error = str(e)

        await asyncio.sleep(1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Watchdog (VPN + Health)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def watchdog_task(clob_client):
    """Monitor system health: VPN, connections, daily reset."""
    from modules.execution_engine import emergency_cancel_all
    from modules import notifier
    from modules.risk_manager import reset_daily_stats

    last_day = time.gmtime().tm_yday

    while True:
        try:
            # VPN check (macOS)
            proc = await asyncio.create_subprocess_shell(
                "ifconfig",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode()

            if config.VPN_INTERFACE and config.VPN_INTERFACE not in output:
                logger.error(f"🚨 VPN interface {config.VPN_INTERFACE} not found!")
                await notifier.notify_system_error(
                    f"VPN down: {config.VPN_INTERFACE} not found"
                )

            # Daily reset at midnight UTC
            current_day = time.gmtime().tm_yday
            if current_day != last_day:
                reset_daily_stats()
                last_day = current_day

            # Connection health log
            logger.debug(
                f"Health: BIN={'✓' if ctx.binance_connected else '✗'} "
                f"CLOB={'✓' if ctx.clob_connected else '✗'} "
                f"TG={'✓' if ctx.telegram_connected else '✗'} "
                f"Phase={ctx.phase} "
                f"PnL=${ctx.session_pnl:+.2f}"
            )

        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        await asyncio.sleep(config.HEARTBEAT_INTERVAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Status Display
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def status_display_task():
    """Print compact status line to terminal every 10s."""
    while True:
        btc = ctx.current_btc_price
        delta = ctx.btc_delta
        phase = ctx.phase
        pnl = ctx.session_pnl
        trades = ctx.trade_count_today
        secs = ctx.market.get("secs_remaining", 0) if ctx.market else 0
        ind_score = ctx.indicator_score
        ind_dir = ctx.indicator_direction
        cvd = ctx.cvd

        pos_str = ""
        if ctx.active_position:
            p = ctx.active_position
            mid = ctx.yes_midpoint if p.direction == "UP" else ctx.no_midpoint
            unr_pnl = (mid - p.entry_price) * p.shares
            pos_str = f" | POS: {p.direction} @ {p.entry_price:.2f} PnL=${unr_pnl:+.2f}"

        delta_color = C.GREEN if delta > 0 else C.RED if delta < 0 else C.DIM
        pnl_color = C.GREEN if pnl > 0 else C.RED if pnl < 0 else C.DIM
        ind_color = C.GREEN if ind_score >= 4 else C.YELLOW if ind_score >= 2 else C.DIM

        print(
            f"  {C.DIM}BTC{C.RESET} ${btc:,.0f} "
            f"{delta_color}Δ${delta:+.0f}{C.RESET} | "
            f"{C.CYAN}{phase:>10}{C.RESET} | "
            f"{secs:>3}s | "
            f"{ind_color}Ind:{ind_score}/5→{ind_dir}{C.RESET} "
            f"CVD:{cvd:+.2f} | "
            f"PnL: {pnl_color}${pnl:+.2f}{C.RESET}"
            f"{pos_str}",
            end="\r",
        )

        await asyncio.sleep(10)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main Entry Point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    """Boot all systems and run."""
    # Add kill switch notification tracking
    ctx._kill_notified = False

    print(f"\n{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  🎯 CONFIRMATION SNIPER — Polymarket BTC 5-Min{C.RESET}")
    print(f"{C.CYAN}{'═' * 60}{C.RESET}\n")

    mode = f"{C.YELLOW}DRY RUN{C.RESET}" if config.DRY_RUN else f"{C.RED}🔴 LIVE{C.RESET}"
    print(f"  Mode:           {mode}")
    print(f"  Trade Size:     ${config.TRADE_SIZE_USD}")
    print(f"  Entry Window:   {config.ENTRY_WINDOW_START}s – {config.ENTRY_WINDOW_END}s (last 2 min)")
    print(f"  Min BTC Delta:  ${config.MIN_DELTA_USD}")
    print(f"  Min Odds Lag:   {config.MIN_ODDS_LAG:.0%}")
    print(f"  Min Indicators: {config.MIN_INDICATOR_SCORE}/5 consensus")
    print(f"  Dashboard:      http://localhost:{config.DASHBOARD_PORT}")

    tg_status = "✓ Configured" if config.TELEGRAM_BOT_TOKEN else "✗ Not configured"
    print(f"  Telegram:       {tg_status}")
    print()

    # Init CLOB client
    clob_client = init_clob_client()

    # Import modules
    from modules.market_fetcher import chainlink_price_task, pyth_price_task, binance_ws_task, market_discovery_task, midpoint_poller_task
    from modules.dashboard_api import start_dashboard
    from modules.notifier import notify_bot_online, hourly_summary_task

    # Send startup notification
    await notify_bot_online()

    # Create all async tasks
    tasks = [
        asyncio.create_task(chainlink_price_task(), name="chainlink_price"),
        asyncio.create_task(pyth_price_task(), name="pyth_price"),
        asyncio.create_task(binance_ws_task(), name="binance_ws"),
        asyncio.create_task(market_discovery_task(), name="market_discovery"),
        asyncio.create_task(signal_loop_task(clob_client), name="signal_loop"),
        asyncio.create_task(start_dashboard(), name="dashboard"),
        asyncio.create_task(watchdog_task(clob_client), name="watchdog"),
        asyncio.create_task(status_display_task(), name="status_display"),
        asyncio.create_task(hourly_summary_task(), name="hourly_summary"),
    ]

    # Only start midpoint poller if we have a CLOB client
    if clob_client:
        tasks.append(
            asyncio.create_task(midpoint_poller_task(clob_client), name="midpoint_poller")
        )

    print(f"  {C.GREEN}✓ All {len(tasks)} systems online{C.RESET}")
    print(f"\n  {C.BOLD}Monitoring 5-minute windows...{C.RESET} (Ctrl+C to stop)\n")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        # Dead man's switch
        logger.info("Shutting down — cancelling all orders...")
        if clob_client and not config.DRY_RUN:
            from modules.execution_engine import emergency_cancel_all
            await emergency_cancel_all(clob_client)
        from modules.notifier import send_telegram
        await send_telegram("🛑 <b>BOT OFFLINE</b>\nConfirmation Sniper shutting down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n\n  {C.YELLOW}Bot stopped gracefully.{C.RESET}\n")
        sys.exit(0)
