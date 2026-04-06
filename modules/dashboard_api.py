"""
Confirmation Sniper — Dashboard API

aiohttp-based backend serving:
- GET /         → Dashboard HTML
- GET /api/status → Full context snapshot
- GET /api/trades → Trade history
- GET /api/performance → Performance metrics
- GET /ws       → WebSocket real-time feed
"""

import asyncio
import json
import logging
import time

from aiohttp import web

import config
from modules.context import ctx

logger = logging.getLogger("DashboardAPI")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REST Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def handle_index(request):
    """Serve the dashboard HTML."""
    try:
        with open("dashboard/index.html", "r") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="Dashboard not found", status=404)


async def handle_status(request):
    """Return full context snapshot."""
    return web.json_response(ctx.get_snapshot())


async def handle_trades(request):
    """Return trade history."""
    trades = [
        {
            "id": t.trade_id,
            "timestamp": t.timestamp,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "shares": t.shares,
            "usd_cost": t.usd_cost,
            "pnl": t.pnl,
            "outcome": t.outcome,
            "btc_delta": t.btc_delta,
            "hold_duration": t.hold_duration,
            "exit_reason": t.exit_reason,
        }
        for t in ctx.session_trades
    ]
    return web.json_response({"trades": trades, "count": len(trades)})


async def handle_performance(request):
    """Return computed performance metrics."""
    trades = ctx.session_trades
    if not trades:
        return web.json_response({
            "total_trades": 0, "win_rate": 0, "session_pnl": 0,
            "avg_win": 0, "avg_loss": 0, "profit_factor": 0,
            "max_drawdown": 0, "best_trade": 0, "worst_trade": 0,
        })

    wins = [t for t in trades if t.outcome == "WIN"]
    losses = [t for t in trades if t.outcome == "LOSS"]

    total_win_pnl = sum(t.pnl for t in wins)
    total_loss_pnl = abs(sum(t.pnl for t in losses))

    # Max drawdown calculation
    equity_curve = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        running += t.pnl
        equity_curve.append(running)
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)

    return web.json_response({
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "session_pnl": round(ctx.session_pnl, 2),
        "avg_win": round(total_win_pnl / len(wins), 2) if wins else 0,
        "avg_loss": round(-total_loss_pnl / len(losses), 2) if losses else 0,
        "profit_factor": round(total_win_pnl / total_loss_pnl, 2) if total_loss_pnl > 0 else 999,
        "max_drawdown": round(max_dd, 2),
        "best_trade": round(max(t.pnl for t in trades), 2) if trades else 0,
        "worst_trade": round(min(t.pnl for t in trades), 2) if trades else 0,
        "equity_curve": [round(e, 2) for e in equity_curve],
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WebSocket Real-Time Feed
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def handle_websocket(request):
    """WebSocket endpoint for real-time dashboard updates."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    ctx.ws_clients.append(ws)
    logger.info(f"Dashboard WS client connected ({len(ctx.ws_clients)} total)")

    try:
        async for msg in ws:
            pass  # We only push data, never read from clients
    except Exception:
        pass
    finally:
        if ws in ctx.ws_clients:
            ctx.ws_clients.remove(ws)
        logger.info(f"Dashboard WS client disconnected ({len(ctx.ws_clients)} total)")

    return ws


async def ws_broadcast_task():
    """Background task: push context snapshot to all WS clients."""
    while True:
        if ctx.ws_clients:
            snapshot = ctx.get_snapshot()
            payload = json.dumps(snapshot)

            dead_clients = []
            for ws in ctx.ws_clients:
                try:
                    await ws.send_str(payload)
                except Exception:
                    dead_clients.append(ws)

            for ws in dead_clients:
                if ws in ctx.ws_clients:
                    ctx.ws_clients.remove(ws)

        await asyncio.sleep(config.WS_PUSH_INTERVAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dashboard Server Init
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def start_dashboard():
    """Initialize and start the dashboard web server."""
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/trades", handle_trades)
    app.router.add_get("/api/performance", handle_performance)
    app.router.add_get("/ws", handle_websocket)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.DASHBOARD_PORT)
    await site.start()

    logger.info(f"🌐 Dashboard: http://localhost:{config.DASHBOARD_PORT}")

    # Start broadcast task
    asyncio.create_task(ws_broadcast_task())

    # Keep alive
    while True:
        await asyncio.sleep(3600)
