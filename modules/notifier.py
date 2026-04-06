"""
Confirmation Sniper — Telegram Notifier

Async Telegram bot with structured HTML messages.
All messages are fire-and-forget (never blocks the trading loop).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

import config
from modules.context import ctx, Signal, TradeRecord

logger = logging.getLogger("Notifier")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Core Send Function
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def send_telegram(text: str, silent: bool = False):
    """Send an HTML-formatted message to Telegram."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        return  # Telegram not configured

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    ctx.telegram_connected = True
                else:
                    body = await resp.text()
                    logger.warning(f"Telegram API error {resp.status}: {body}")
    except Exception as e:
        ctx.telegram_connected = False
        logger.warning(f"Telegram send failed: {e}")


async def _fire(text: str, silent: bool = False):
    """Fire-and-forget telegram message (non-blocking)."""
    asyncio.create_task(send_telegram(text, silent))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Alert Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def notify_bot_online():
    """Bot startup notification."""
    await send_telegram(
        "✅ <b>BOT ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"Strategy: Confirmation Sniper\n"
        f"Mode: {'🔸 DRY RUN' if config.DRY_RUN else '🔴 LIVE'}\n"
        f"Trade Size: ${config.TRADE_SIZE_USD}\n"
        f"Max Daily Loss: ${config.MAX_DAILY_LOSS}\n"
        f"Dashboard: http://localhost:{config.DASHBOARD_PORT}"
    )


async def notify_signal_detected(signal: Signal):
    """Alert when a trading signal is found."""
    emoji = "📈" if signal.direction == "UP" else "📉"
    await _fire(
        f"🔍 <b>SIGNAL DETECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"{emoji} Direction: <b>{signal.direction}</b>\n"
        f"💰 BTC Δ: <b>${signal.btc_delta:+.0f}</b>\n"
        f"📊 True Prob: {signal.true_prob:.0%}\n"
        f"📉 Market Odds: {signal.market_odds:.0%}\n"
        f"⚡ Odds Lag: <b>{signal.odds_lag:.0%}</b>\n"
        f"🎯 Entry Target: {signal.entry_price:.2f}¢"
    )


async def notify_trade_executed(signal: Signal, usd_size: float, shares: float):
    """Alert when a trade is filled."""
    emoji = "🟢" if signal.direction == "UP" else "🔴"
    await _fire(
        f"⚡ <b>TRADE EXECUTED</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"{emoji} BUY {signal.direction} @ {signal.entry_price:.2f}\n"
        f"📦 Size: ${usd_size:.2f} ({shares:.1f} sh)\n"
        f"📊 Odds Lag: {signal.odds_lag:.0%}\n"
        f"💰 BTC Δ: ${signal.btc_delta:+.0f}\n"
        f"⏱ Window: {ctx.market.get('secs_remaining', 0)}s left"
    )


async def notify_trade_settled(trade: TradeRecord):
    """Alert when a trade settles."""
    if trade.outcome == "WIN":
        emoji = "🎯"
        color = "+"
    else:
        emoji = "💔"
        color = ""

    await _fire(
        f"{emoji} <b>TRADE SETTLED</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Result: <b>{trade.outcome}</b>\n"
        f"PnL: <b>{color}${trade.pnl:.2f}</b>\n"
        f"Direction: {trade.direction}\n"
        f"Entry: {trade.entry_price:.2f} → Exit: {trade.exit_price:.2f}\n"
        f"Held: {trade.hold_duration:.0f}s\n"
        f"Reason: {trade.exit_reason}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Session PnL: ${ctx.session_pnl:+.2f}"
    )


async def notify_emergency_exit(reason: str):
    """Alert on emergency exit."""
    await send_telegram(
        f"🚨 <b>EMERGENCY EXIT</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Reason: {reason}\n"
        f"BTC: ${ctx.current_btc_price:,.2f}\n"
        f"Momentum: {ctx.momentum_pct:.4%}\n"
        f"Session PnL: ${ctx.session_pnl:+.2f}"
    )


async def notify_kill_switch():
    """Alert when kill switch activates."""
    await send_telegram(
        f"🛑 <b>KILL SWITCH ACTIVATED</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Daily Loss: ${ctx.daily_loss:.2f}\n"
        f"Limit: ${config.MAX_DAILY_LOSS:.2f}\n"
        f"Bot halted. Manual restart required.\n"
        f"Session Trades: {ctx.trade_count_today}\n"
        f"Session PnL: ${ctx.session_pnl:+.2f}"
    )


async def notify_system_error(error: str):
    """Alert on system-level errors."""
    await _fire(
        f"⚠️ <b>SYSTEM ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"{error}",
        silent=True,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Periodic Summaries
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def send_hourly_summary():
    """Send hourly performance summary."""
    trades = ctx.session_trades
    if not trades:
        return

    wins = sum(1 for t in trades if t.outcome == "WIN")
    losses = sum(1 for t in trades if t.outcome == "LOSS")
    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0

    avg_win = 0.0
    avg_loss = 0.0
    if wins > 0:
        avg_win = sum(t.pnl for t in trades if t.outcome == "WIN") / wins
    if losses > 0:
        avg_loss = sum(t.pnl for t in trades if t.outcome == "LOSS") / losses

    uptime_hrs = (time.time() - ctx.bot_start_time) / 3600

    await send_telegram(
        f"📊 <b>HOURLY SUMMARY</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Trades: {total} ({wins}W / {losses}L)\n"
        f"Win Rate: {win_rate:.0f}%\n"
        f"Session PnL: ${ctx.session_pnl:+.2f}\n"
        f"Avg Win: ${avg_win:+.2f}\n"
        f"Avg Loss: ${avg_loss:+.2f}\n"
        f"Daily Loss: ${ctx.daily_loss:.2f} / ${config.MAX_DAILY_LOSS:.2f}\n"
        f"Uptime: {uptime_hrs:.1f}h\n"
        f"BTC: ${ctx.current_btc_price:,.2f}",
        silent=True,
    )


async def hourly_summary_task():
    """Background task: send summary every hour."""
    while True:
        await asyncio.sleep(3600)
        try:
            await send_hourly_summary()
        except Exception as e:
            logger.error(f"Hourly summary failed: {e}")
