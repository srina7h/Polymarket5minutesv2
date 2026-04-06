"""
Confirmation Sniper — Market Fetcher

Four async tasks:
1a. Chainlink Oracle Poller — reads BTC/USD from the on-chain Chainlink feed
    (this is the EXACT price source Polymarket settles on)
1b. Binance WebSocket — real-time aggTrades for volume data (CVD indicator)
2.  Market Discovery — finds current Polymarket BTC 5-min market
3.  CLOB Midpoint Poller — fetches YES/NO midpoints
"""

import asyncio
import json
import logging
import struct
import time

import aiohttp
import websockets

import config
from modules.context import ctx

logger = logging.getLogger("MarketFetcher")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Task 1a: Chainlink BTC/USD Oracle Poller
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Minimal ABI call: latestRoundData() → (uint80,int256,uint256,uint256,uint80)
# Function selector: 0xfeaf968c
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"

async def chainlink_price_task():
    """
    Poll the Chainlink BTC/USD on-chain oracle.
    This is the EXACT price feed Polymarket settles on.
    Reads latestRoundData() via eth_call every ~1 second.
    """
    logger.info("Starting Chainlink BTC/USD oracle poller...")
    
    rpc_url = config.CHAINLINK_RPC_URL
    contract_addr = config.CHAINLINK_BTC_USD_ADDR
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{
                        "to": contract_addr,
                        "data": LATEST_ROUND_DATA_SELECTOR,
                    }, "latest"],
                    "id": 1,
                }
                
                async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    result = await resp.json()
                
                if "result" in result and result["result"] != "0x":
                    hex_data = result["result"][2:]  # Strip 0x
                    # Decode: 5 x 32-byte words (roundId, answer, startedAt, updatedAt, answeredInRound)
                    # answer is at offset 32 bytes (word index 1), signed int256
                    if len(hex_data) >= 320:  # 5 * 64 hex chars
                        answer_hex = hex_data[64:128]  # 2nd 32-byte word
                        answer = int(answer_hex, 16)
                        # Handle signed int256
                        if answer >= 2**255:
                            answer -= 2**256
                        
                        # Chainlink BTC/USD has 8 decimals
                        price = answer / 1e8
                        
                        if price > 1000:  # Sanity check
                            ts = time.time()
                            # Update the price in context (use 0 qty since this is price-only)
                            ctx.update_chainlink_price(price, ts)
                            
                            if not ctx.binance_connected:
                                # If Binance isn't connected, use Chainlink as sole source
                                ctx.binance_connected = True
                                logger.info(f"✓ Chainlink BTC/USD connected: ${price:,.2f}")

            except asyncio.TimeoutError:
                pass  # Silent timeout, retry
            except Exception as e:
                logger.error(f"Chainlink oracle error: {e}")
            
            await asyncio.sleep(config.CHAINLINK_POLL_INTERVAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Task 1b: Pyth Network BTC/USD (Secondary Price Feed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def pyth_price_task():
    """
    Poll the Pyth Network Hermes API for BTC/USD.
    Acts as a secondary/cross-validation price feed.
    Higher frequency than Chainlink on-chain reads.
    """
    logger.info("Starting Pyth Network BTC/USD price poller...")
    
    url = f"{config.PYTH_HERMES_URL}/v2/updates/price/latest"
    params = {"ids[]": config.PYTH_BTC_USD_ID}
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                
                if "parsed" in data and data["parsed"]:
                    price_data = data["parsed"][0]["price"]
                    raw_price = int(price_data["price"])
                    exponent = int(price_data["expo"])
                    price = raw_price * (10 ** exponent)
                    
                    if price > 1000:  # Sanity check
                        ts = time.time()
                        ctx.update_pyth_price(price, ts)
                        
                        if not hasattr(ctx, '_pyth_logged'):
                            logger.info(f"✓ Pyth BTC/USD connected: ${price:,.2f}")
                            ctx._pyth_logged = True

            except asyncio.TimeoutError:
                pass
            except Exception as e:
                if not hasattr(ctx, '_pyth_err_logged'):
                    logger.warning(f"Pyth feed error: {e}")
                    ctx._pyth_err_logged = True
            
            await asyncio.sleep(config.PYTH_POLL_INTERVAL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Task 1c: Binance WebSocket (Volume Data for CVD)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def binance_ws_task():
    """Stream BTC/USD aggTrades from Binance for volume data (CVD indicator)."""
    logger.info("Starting Binance WebSocket feed (volume data)...")

    while True:
        try:
            async with websockets.connect(
                config.BINANCE_WS_URL,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                logger.info("✓ Binance WebSocket connected (volume feed)")

                async for msg in ws:
                    data = json.loads(msg)
                    price = float(data["p"])
                    qty = float(data["q"])
                    # Binance: m=True means buyer is maker → seller-initiated (bearish)
                    # m=False means seller is maker → buyer-initiated (bullish)
                    is_buyer = not data["m"]
                    ts = time.time()
                    ctx.update_binance_volume(price, qty, is_buyer, ts)

        except websockets.ConnectionClosed:
            logger.warning("Binance WS disconnected. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Binance WS error: {e}")
            await asyncio.sleep(5)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Task 2: Market Discovery (Gamma API)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_window_info() -> dict:
    """Compute current 5-min window slug and timing."""
    now = int(time.time())
    window_start = (now // 300) * 300
    elapsed = now - window_start
    secs_remaining = 300 - elapsed
    slug = f"btc-updown-5m-{window_start}"
    return {
        "slug": slug,
        "window_start": window_start,
        "elapsed": elapsed,
        "secs_remaining": secs_remaining,
    }


async def fetch_market_from_gamma(session: aiohttp.ClientSession, slug: str) -> dict | None:
    """Try to find the BTC 5-min market from Gamma API."""
    try:
        # Strategy 1: Direct slug lookup
        async with session.get(
            f"{config.GAMMA_API}/markets",
            params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                markets = await resp.json()
                if markets and len(markets) > 0:
                    mkt = markets[0]
                    tokens = mkt.get("clobTokenIds", [])
                    if isinstance(tokens, str):
                        tokens = json.loads(tokens)
                    if len(tokens) >= 2:
                        return {
                            "title": mkt.get("question", slug),
                            "yes_token": tokens[0],
                            "no_token": tokens[1],
                            "condition_id": mkt.get("conditionId", ""),
                            "market_id": mkt.get("id", ""),
                        }
    except Exception as e:
        logger.debug(f"Slug lookup failed: {e}")

    try:
        # Strategy 2: Search active markets
        async with session.get(
            f"{config.GAMMA_API}/markets",
            params={"limit": 100, "active": "true", "closed": "false"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                markets = await resp.json()
                for mkt in markets:
                    q = mkt.get("question", "").lower()
                    if ("btc" in q or "bitcoin" in q) and ("5" in q) and ("min" in q or "minute" in q):
                        tokens = mkt.get("clobTokenIds", [])
                        if isinstance(tokens, str):
                            tokens = json.loads(tokens)
                        if len(tokens) >= 2:
                            return {
                                "title": mkt.get("question", "BTC 5-Min"),
                                "yes_token": tokens[0],
                                "no_token": tokens[1],
                                "condition_id": mkt.get("conditionId", ""),
                                "market_id": mkt.get("id", ""),
                            }
    except Exception as e:
        logger.debug(f"Fallback search failed: {e}")

    return None


async def market_discovery_task():
    """Every 5s, find the current BTC 5-min market and detect window transitions."""
    logger.info("Starting market discovery loop...")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                window = compute_window_info()

                # Detect new window
                if window["window_start"] != ctx.last_window_start:
                    logger.info(f"📊 New window detected: {window['slug']}")
                    ctx.new_window(window["window_start"])

                # Update timing
                if ctx.market:
                    ctx.market["secs_remaining"] = window["secs_remaining"]
                    ctx.market["elapsed"] = window["elapsed"]

                # Compute phase
                ctx.phase = ctx.compute_phase(window["secs_remaining"])

                # Discover market if not found or stale
                if not ctx.market or ctx.market.get("window_start") != window["window_start"]:
                    market_data = await fetch_market_from_gamma(session, window["slug"])
                    if market_data:
                        market_data["secs_remaining"] = window["secs_remaining"]
                        market_data["elapsed"] = window["elapsed"]
                        market_data["window_start"] = window["window_start"]
                        ctx.market = market_data
                        logger.info(f"  ✓ Market: {market_data['title']}")
                    else:
                        ctx.market = None

            except Exception as e:
                logger.error(f"Market discovery error: {e}")

            await asyncio.sleep(5)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Task 3: CLOB Midpoint Poller
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def midpoint_poller_task(clob_client):
    """Every 2s, fetch YES/NO midpoints from Polymarket CLOB."""
    logger.info("Starting CLOB midpoint poller...")

    while True:
        try:
            if ctx.market and ctx.market.get("yes_token"):
                # Fetch midpoints in thread (py-clob-client is sync)
                yes_mid = await asyncio.to_thread(
                    clob_client.get_midpoint, ctx.market["yes_token"]
                )
                no_mid = await asyncio.to_thread(
                    clob_client.get_midpoint, ctx.market["no_token"]
                )

                # Handle different response formats
                if isinstance(yes_mid, dict):
                    ctx.yes_midpoint = float(yes_mid.get("mid", 0.50))
                else:
                    ctx.yes_midpoint = float(yes_mid) if yes_mid else 0.50

                if isinstance(no_mid, dict):
                    ctx.no_midpoint = float(no_mid.get("mid", 0.50))
                else:
                    ctx.no_midpoint = float(no_mid) if no_mid else 0.50

                ctx.clob_connected = True
        except Exception as e:
            ctx.clob_connected = False
            logger.debug(f"Midpoint poll error: {e}")

        await asyncio.sleep(2)
