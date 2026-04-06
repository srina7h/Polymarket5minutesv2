#!/usr/bin/env python3
"""
Hybrid EV Gap Trader for Polymarket 5-Minute BTC
Combines LP Engine (Base) with Alpha Directional Trades (Momentum)
"""

import os
import sys
import time
import asyncio
import json
import logging
import collections
from datetime import datetime, timezone
import requests
import aiohttp
from aiohttp import web
import websockets
from dotenv import load_dotenv

import config

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:
    print("❌ py-clob-client not found. Run: source .venv/bin/activate && python3 hybrid_trader.py")
    sys.exit(1)

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HybridTrader")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
    CYAN = "\033[96m"; MAGENTA = "\033[95m"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Global State Context
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MODE_LP = "LP"
MODE_ALPHA = "ALPHA"

class TradingContext:
    def __init__(self):
        self.mode = MODE_LP
        self.active_lp_order_ids = set()
        self.active_alpha_order_ids = set()
        self.alpha_position = None
        self.inventory_yes = 0.0
        self.inventory_no = 0.0
        self.momentum_pct = 0.0
        self.current_btc_price = 0.0
        self.market_info = None
        self.cooldown_until = 0
        self.btc_price_history = collections.deque(maxlen=600)  # Stores (timestamp, price) ~ 10 mins at 1 update/sec

ctx = TradingContext()
auth_client = None

# Async Wrapper for Sync py-clob-client
async def async_clob(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Setup & Auth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
load_dotenv()
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIG_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

def init_clob_client() -> ClobClient:
    if not PRIVATE_KEY:
        logger.error("❌ Missing POLYMARKET_PRIVATE_KEY in .env")
        sys.exit(1)
    logger.info(f"Connecting to Polymarket CLOB at {HOST}...")
    try:
        c = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=SIG_TYPE, funder=FUNDER if FUNDER else None)
        c.set_api_creds(c.create_or_derive_api_creds())
        if c.get_ok() != "OK":
            raise Exception("Server not OK")
        logger.info("✓ Connected to Polymarket")
        return c
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        sys.exit(1)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Telegram & Watchdogs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def send_telegram(msg: str):
    token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.warning(f"Telegram failed: {e}")

async def watchdog_task():
    """Monitors VPN via ifconfig and general connection."""
    vpn_iface = getattr(config, "VPN_INTERFACE", "tun0")
    while True:
        try:
            # Check VPN
            proc = await asyncio.create_subprocess_shell('ifconfig', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await proc.communicate()
            if vpn_iface not in stdout.decode():
                logger.error(f"🚨 VPN interface {vpn_iface} down! Engaging Dead Man's Switch.")
                await dead_mans_switch()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        await asyncio.sleep(getattr(config, "HEARTBEAT_INTERVAL", 10))

async def dead_mans_switch():
    """Cancel all active orders immediately on critical failure."""
    logger.error("🛑 Disconnecting and Cancelling ALL ORDERS!")
    try:
        await async_clob(auth_client.cancel_all)
        await send_telegram("🚨 <b>CRITICAL:</b> Dead Man Switch engaged. All Polymarket orders cancelled.")
    except Exception as e:
        logger.error(f"Dead Man Switch failed to cancel: {e}")
    sys.exit(1)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dynamic Market Discovery
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def compute_current_window_slug() -> tuple:
    now = int(time.time())
    window_start = (now // 300) * 300
    secs_left = 300 - (now - window_start)
    return f"btc-updown-5m-{window_start}", window_start, secs_left

async def fetch_active_market() -> dict:
    slug, ws, sl = compute_current_window_slug()
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Try slug direct
            async with session.get(f"{config.POLYMARKET_GAMMA_API}/markets?slug={slug}", timeout=10) as resp:
                if resp.status == 200:
                    markets = await resp.json()
                    if markets and len(markets) > 0:
                        mkt = markets[0]
                        tokens = json.loads(mkt.get("clobTokenIds", "[]")) if isinstance(mkt.get("clobTokenIds"), str) else mkt.get("clobTokenIds", [])
                        if len(tokens) >= 2:
                            return {
                                "title": mkt.get("question", slug),
                                "yes_token": tokens[0],
                                "no_token": tokens[1],
                                "secs_remaining": sl,
                                "window_start": ws
                            }
            # 2. Try Fallback search
            async with session.get(f"{config.POLYMARKET_GAMMA_API}/markets?limit=100&active=true&closed=false", timeout=10) as resp:
                if resp.status == 200:
                    markets = await resp.json()
                    for mkt in markets:
                        q = mkt.get("question", "").lower()
                        if ("btc" in q or "bitcoin" in q) and ("5" in q) and ("min" in q or "minute" in q):
                            tokens = json.loads(mkt.get("clobTokenIds", "[]")) if isinstance(mkt.get("clobTokenIds"), str) else mkt.get("clobTokenIds", [])
                            if len(tokens) >= 2:
                                return {
                                    "title": mkt.get("question", "BTC 5-Min"),
                                    "yes_token": tokens[0],
                                    "no_token": tokens[1],
                                    "secs_remaining": sl,
                                    "window_start": ws
                                }
    except Exception as e:
        logger.warning(f"Gamma API error: {e}")
    return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Signal Engine (Binance WS)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def binance_ws_task():
    """Connect to Binance stream, track momentum over configured window."""
    ws_url = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
    window_sec = getattr(config, "MOMENTUM_WINDOW_SEC", 10)
    
    logger.info("Initializing Signal Engine [Binance WS]...")
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    price = float(data['p'])
                    ts = time.time()
                    
                    ctx.current_btc_price = price
                    ctx.btc_price_history.append((ts, price))
                    
                    # Compute Momentum
                    cutoff = ts - window_sec
                    while ctx.btc_price_history and ctx.btc_price_history[0][0] < cutoff:
                        ctx.btc_price_history.popleft()
                        
                    if len(ctx.btc_price_history) > 0:
                        old_price = ctx.btc_price_history[0][1]
                        ctx.momentum_pct = (price - old_price) / old_price
                        
                    await evaluate_alpha_signals()
                    
        except websockets.ConnectionClosed:
            logger.warning("Binance WS disconnected. Reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Binance WS Error: {e}")
            await asyncio.sleep(5)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Engine Interactions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def get_wallet_balance() -> float:
    if getattr(config, "LIVE_DRY_RUN", False):
        return getattr(config, "INITIAL_CAPITAL", 1000.0)
    try:
        b = await async_clob(auth_client.get_balance)
        raw = float(b)
        return raw / 1_000_000 if raw > 1000 else raw
    except Exception:
        return 0.0

async def cancel_all_lp_orders():
    """Cancel any tracking LP limit orders"""
    if not ctx.active_lp_order_ids: return
    logger.info(f"Cancelling {len(ctx.active_lp_order_ids)} LP orders...")
    if not getattr(config, "LIVE_DRY_RUN", False):
        for oid in ctx.active_lp_order_ids:
            try: await async_clob(auth_client.cancel, oid)
            except: pass
    ctx.active_lp_order_ids.clear()

async def submit_fok_order(token_id: str, direction: str, usd_size: float, price: float) -> bool:
    """Submit Alpha Engine Entry FOK order."""
    if getattr(config, "LIVE_DRY_RUN", False):
        logger.info(f"[DRY_RUN] ALPHA Entry FOK {direction} @ {price:.2f} Size: ${usd_size}")
        return True
        
    try:
        args = MarketOrderArgs(token_id=token_id, amount=usd_size, side=BUY)
        signed = await async_clob(auth_client.create_market_order, args)
        resp = await async_clob(auth_client.post_order, signed, OrderType.FOK)
        logger.info(f"✓ Alpha Entry FOK Filled: {resp}")
        await send_telegram(f"⚡ <b>ALPHA ENTRY:</b> {direction} filled for ${usd_size:.2f} @ presumed ~{price:.2f}")
        return True
    except Exception as e:
        logger.error(f"Alpha FOK Failed: {e}")
        return False

async def submit_limit_order(token_id: str, direction: str, shares: float, price: float, is_alpha_exit: bool = False):
    """Submit an exact limit order for LP or Alpha Exits."""
    if getattr(config, "LIVE_DRY_RUN", False):
        m = f"{'[LP]' if not is_alpha_exit else '[ALPHA EXIT]'} Limit BUY {direction} {shares:.1f}sh @ {price:.2f}"
        logger.info(f"[DRY_RUN] {m}")
        if is_alpha_exit: ctx.active_alpha_order_ids.add("mock_alpha_ext_" + str(time.time()))
        else: ctx.active_lp_order_ids.add("mock_lp_ord_" + str(time.time()))
        return
        
    try:
        args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed = await async_clob(auth_client.create_order, args)
        resp = await async_clob(auth_client.post_order, signed, OrderType.GTC)
        oid = resp.get("orderID")
        if oid:
            if is_alpha_exit: ctx.active_alpha_order_ids.add(oid)
            else: ctx.active_lp_order_ids.add(oid)
            return oid
    except Exception as e:
        logger.error(f"Limit Order creation failed: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Alpha Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def evaluate_alpha_signals():
    """Triggered by Binance WS ticks. Checks if momentum triggers Alpha Mode."""
    if ctx.mode != MODE_LP or not ctx.market_info:
        return # Skip if already in Alpha, or no active market
        
    if time.time() < ctx.cooldown_until:
        return
        
    sup = getattr(config, "STRONG_UP_THRESHOLD", 0.002)
    sdown = getattr(config, "STRONG_DOWN_THRESHOLD", -0.002)
    
    alpha_trigger = None
    if ctx.momentum_pct >= sup: alpha_trigger = "UP"
    elif ctx.momentum_pct <= sdown: alpha_trigger = "DOWN"
    
    if alpha_trigger:
        logger.warning(f"🚀 ALPHA SIGNAL DETECTED. Momentum: {ctx.momentum_pct:.3%}. Switching to Alpha Mode.")
        ctx.mode = MODE_ALPHA
        await cancel_all_lp_orders()
        asyncio.create_task(execute_alpha_trade(alpha_trigger))

async def execute_alpha_trade(direction: str):
    token = ctx.market_info["yes_token"] if direction == "UP" else ctx.market_info["no_token"]
    
    # Check if orderbook midpoint allows entry inside bounds (0.22 - 0.28)
    try:
        mid_raw = await async_clob(auth_client.get_midpoint, token)
        price = float(mid_raw.get("mid", 0.25)) if isinstance(mid_raw, dict) else float(mid_raw)
    except:
        price = 0.25 # fallback guess
        
    # Alpha Filter 1: Entry Range
    amin = getattr(config, "ALPHA_ENTRY_MIN", 0.22)
    amax = getattr(config, "ALPHA_ENTRY_MAX", 0.28)
    if price < amin or price > amax:
        logger.info(f"Alpha Entry filtered. Book price {price:.2f} not in [{amin}, {amax}] bounds.")
        ctx.mode = MODE_LP
        return
        
    usd_size = getattr(config, "LIVE_TRADE_AMOUNT_USD", 5.0)
    bal = await get_wallet_balance()
    usd_size = min(usd_size, bal)
    
    if usd_size < 1.0:
        logger.info("Insufficient balance for Alpha trade.")
        ctx.mode = MODE_LP
        return

    # STEP 2: Enter Alpha Trade
    success = await submit_fok_order(token, direction, usd_size, price)
    if not success:
        ctx.cooldown_until = time.time() + getattr(config, "ALPHA_COOLDOWN_SEC", 15)
        ctx.mode = MODE_LP
        return
        
    # Track position
    shares = usd_size / price
    ctx.alpha_position = {"direction": direction, "token": token, "entry": price, "shares": shares, "usd": usd_size}
    
    # STEP 3: Place staggered Limits
    await submit_alpha_exits()
    
    # STEP 4: Start Alpha Monitor
    asyncio.create_task(monitor_alpha_position())

async def submit_alpha_exits():
    """Step 3: Staggered Exits at higher limits"""
    if not ctx.alpha_position: return
    p = ctx.alpha_position
    tiers = getattr(config, "ALPHA_EXIT_TIERS", {0.35: 0.50, 0.40: 0.30, 0.45: 0.20})
    
    for tp_price, pct in tiers.items():
        if tp_price > p["entry"]:
            # Actually, to *exit* a position, we must SELL the shares
            # Wait, py_clob_client SELL means selling YES/NO tokens we own.
            # I must use OrderArgs with side=SELL!
            sz = p["shares"] * pct
            if getattr(config, "LIVE_DRY_RUN", False):
                logger.info(f"[DRY_RUN] [ALPHA EXIT] Limit SELL {p['direction']} {sz:.1f}sh @ {tp_price:.2f}")
                ctx.active_alpha_order_ids.add("mock_exit_" + str(time.time()) + str(tp_price))
            else:
                try:
                    args = OrderArgs(token_id=p["token"], price=tp_price, size=sz, side=SELL)
                    signed = await async_clob(auth_client.create_order, args)
                    resp = await async_clob(auth_client.post_order, signed, OrderType.GTC)
                    if resp.get("orderID"): ctx.active_alpha_order_ids.add(resp.get("orderID"))
                except Exception as e:
                    logger.error(f"Failed Alpha Limit Exit {tp_price}: {e}")

async def monitor_alpha_position():
    """Step 4: Watch for Stop Loss or Reversals"""
    sl_max = getattr(config, "ALPHA_STOP_LOSS_MAX", 0.15)
    
    while ctx.mode == MODE_ALPHA and ctx.alpha_position:
        p = ctx.alpha_position
        try:
            mid_raw = await async_clob(auth_client.get_midpoint, p["token"])
            curr_price = float(mid_raw.get("mid", 0.5)) if isinstance(mid_raw, dict) else float(mid_raw)
            
            # SL check
            if curr_price <= sl_max:
                logger.warning(f"🚨 ALPHA STOP LOSS hit for {p['direction']} @ {curr_price:.2f}. Bailing!")
                await cancel_alpha_orders()
                await execute_alpha_bailout(p["token"], p["shares"])
                break
                
            # Reversal Check
            rev_mom = 0.001
            if p["direction"] == "UP" and ctx.momentum_pct <= -rev_mom:
                logger.warning("📉 Reversal Momentum Detected vs UP Alpha! Bailing!")
                await cancel_alpha_orders()
                await execute_alpha_bailout(p["token"], p["shares"])
                break
            elif p["direction"] == "DOWN" and ctx.momentum_pct >= rev_mom:
                logger.warning("📈 Reversal Momentum Detected vs DOWN Alpha! Bailing!")
                await cancel_alpha_orders()
                await execute_alpha_bailout(p["token"], p["shares"])
                break
                
        except Exception:
            pass # ignore errors here
            
        await asyncio.sleep(2)

async def cancel_alpha_orders():
    for oid in ctx.active_alpha_order_ids:
        if not getattr(config, "LIVE_DRY_RUN", False):
            try: await async_clob(auth_client.cancel, oid)
            except: pass
    ctx.active_alpha_order_ids.clear()

async def execute_alpha_bailout(token: str, shares: float):
    # Fire FOK SELL to dump everything
    if not getattr(config, "LIVE_DRY_RUN", False):
        try:
            # Polymarket Market SELL order
            args = MarketOrderArgs(token_id=token, amount=shares, side=SELL)
            signed = await async_clob(auth_client.create_market_order, args)
            await async_clob(auth_client.post_order, signed, OrderType.FOK)
            await send_telegram("🛡 <b>STOP LOSS EXECUTED:</b> Alpha trade closed to limit losses.")
        except Exception as e:
            logger.error(f"Stop Loss sell failed: {e}")
    else:
        logger.info(f"[DRY_RUN] Alpha Stop Loss FOK SELL executed.")
        
    ctx.alpha_position = None
    ctx.mode = MODE_LP
    ctx.cooldown_until = time.time() + getattr(config, "ALPHA_COOLDOWN_SEC", 15)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LP Engine (Market Making Base Layer)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def lp_engine_task():
    """Maintains two sided quotes to farm spread"""
    refresh = getattr(config, "LP_REFRESH_INTERVAL", 5)
    imp = getattr(config, "LP_SPREAD_IMPROVEMENT", 0.01)
    
    logger.info("Initializing LP Engine [Market Making]...")
    while True:
        await asyncio.sleep(refresh)
        
        ctx.market_info = await fetch_active_market()
        logger.info(f"LP Heartbeat. Market Info: {'YES' if ctx.market_info else 'NONE'}, Secs: {ctx.market_info.get('secs_remaining') if ctx.market_info else -1}")
        if not ctx.market_info:
            await cancel_all_lp_orders()
            continue
            
        if ctx.market_info["secs_remaining"] < 15:
            await cancel_all_lp_orders() # Close to settlement, too dangerous to quote
            continue
            
        if ctx.mode != MODE_LP or time.time() < ctx.cooldown_until:
            continue
            
        m = ctx.market_info
        try:
            mid_yes = await async_clob(auth_client.get_midpoint, m["yes_token"])
            mid_no = await async_clob(auth_client.get_midpoint, m["no_token"])
            
            # Handle Py-Clob-Client returning either a string or a dict {"mid": "0.55"}
            yes_val = mid_yes.get("mid", 0.50) if isinstance(mid_yes, dict) else mid_yes
            no_val = mid_no.get("mid", 0.50) if isinstance(mid_no, dict) else mid_no
            
            yes_px = float(yes_val) if yes_val else 0.50
            no_px = float(no_val) if no_val else 0.50
        except Exception as e:
            logger.error(f"Midpoint fetch failed: {e}")
            continue
            
        # Delta skewing
        delta = ctx.inventory_yes - ctx.inventory_no
        max_d = getattr(config, "MAX_INVENTORY_DELTA", 15.0)
        
        y_bias = 0.0
        n_bias = 0.0
        if delta > max_d: n_bias += 0.02 # Too much YES, price NO richer to get fills
        elif delta < -max_d: y_bias += 0.02 # Too much NO, price YES richer
        
        # We quote slightly better than midpoint to hop queue, bound 0.01 - 0.99
        y_quote = min(0.99, max(0.01, yes_px + imp + y_bias))
        n_quote = min(0.99, max(0.01, no_px + imp + n_bias))
        
        # Only repost if quotes moved significantly (to maintain queue position) to save rate limits
        # [Simplified for now: we blindly cancel/repost, though optimization would check old vs new price first]
        await cancel_all_lp_orders()
        
        # Place new quotes
        sz = getattr(config, "LIVE_TRADE_AMOUNT_USD", 5.0) / 2.0  # Split sizing 
        sh_y = sz / y_quote
        sh_n = sz / n_quote
        
        await submit_limit_order(m["yes_token"], "UP", sh_y, y_quote)
        await submit_limit_order(m["no_token"], "DOWN", sh_n, n_quote)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dashboard Server
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def api_status(request):
    # Shallow copy to avoid locking issues, convert sets to lists to make json serializable
    data = {
        "mode": ctx.mode,
        "cooldown_until": ctx.cooldown_until,
        "momentum_pct": ctx.momentum_pct,
        "current_btc_price": ctx.current_btc_price,
        "inventory_yes": ctx.inventory_yes,
        "inventory_no": ctx.inventory_no,
        "active_lp_order_ids": list(ctx.active_lp_order_ids),
        "active_alpha_order_ids": list(ctx.active_alpha_order_ids),
        "alpha_position": ctx.alpha_position,
        "market_info": ctx.market_info
    }
    return web.json_response(data)

async def serve_index(request):
    try:
        with open("dashboard/index.html", "r") as f:
            return web.Response(text=f.read(), content_type='text/html')
    except FileNotFoundError:
        return web.Response(text="Dashboard HTML not found", status=404)

async def init_dashboard():
    app = web.Application()
    app.router.add_get('/api/status', api_status)
    app.router.add_get('/', serve_index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("🌐 Live Dashboard mounted successfully at http://localhost:8080")
    
    # Keep alive endlessly to match other tasks
    while True:
        await asyncio.sleep(3600)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main Application
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def master_entry():
    global auth_client
    print(f"\n{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  🔥 HYBRID POLYMARKET BOT — LP + ALPHA MOMENTUM{C.RESET}")
    print(f"{C.CYAN}{'═' * 60}{C.RESET}\n")

    auth_client = init_clob_client()
    
    # Boot tasks
    w_task = asyncio.create_task(watchdog_task())
    ws_task = asyncio.create_task(binance_ws_task())
    lp_task = asyncio.create_task(lp_engine_task())
    db_task = asyncio.create_task(init_dashboard())
    
    await send_telegram("✅ <b>BOT ONLINE:</b> Hybrid Tracer Matrix active.")
    
    try:
        await asyncio.gather(w_task, ws_task, lp_task, db_task)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Fatal Event Loop Crash: {e}")
    finally:
        await dead_mans_switch()

if __name__ == "__main__":
    try:
        asyncio.run(master_entry())
    except KeyboardInterrupt:
        logger.info("\nCaught KeyboardInterrupt. Shutting down gracefully...")
        # Since loop is closing, we might need a sync cancel all here.
        if auth_client: auth_client.cancel_all()
        sys.exit(0)
