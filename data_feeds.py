"""
LEMA Trading Bot — Data Feeds
Real-time BTC price from Binance WebSocket + Polymarket market simulation.
"""

import json
import logging
import threading
import time
from collections import deque

import websocket
import requests

import config

logger = logging.getLogger("lema.feeds")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Binance WebSocket Feed
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BinanceFeed:
    """Streams real-time BTC/USDT price from Binance."""

    def __init__(self):
        self._price = 0.0
        self._volume_24h = 0.0
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._running = False
        self._ws = None
        self._thread = None

        # Price history: (timestamp, price) tuples for momentum calc
        self._price_history = deque(maxlen=600)  # 10 min of 1s ticks
        self._last_record_time = 0

    # ── public api ──────────────────────────

    def start(self):
        """Start the WebSocket connection in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait up to 10s for first price
        if not self._connected.wait(timeout=10):
            logger.warning("Binance WS: no initial price within 10s")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_price(self) -> float:
        with self._lock:
            return self._price

    def get_volume_24h(self) -> float:
        with self._lock:
            return self._volume_24h

    def get_price_history(self, seconds: int = 300) -> list:
        """Return list of (timestamp, price) from the last N seconds."""
        cutoff = time.time() - seconds
        with self._lock:
            return [(t, p) for t, p in self._price_history if t >= cutoff]

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── internal ────────────────────────────

    def _run(self):
        backoff = 1
        while self._running:
            try:
                logger.info(f"Binance WS: connecting to {config.BINANCE_WS_URL}")
                self._ws = websocket.WebSocketApp(
                    config.BINANCE_WS_URL,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.error(f"Binance WS error: {e}")

            if self._running:
                self._connected.clear()
                logger.info(f"Binance WS: reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _on_open(self, ws):
        logger.info("Binance WS: connected ✓")
        self._connected.set()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            price = float(data.get("c", 0))  # 'c' = last price
            volume = float(data.get("v", 0))  # 'v' = 24h volume
            now = time.time()

            with self._lock:
                self._price = price
                self._volume_24h = volume
                # Record at most once per second
                if now - self._last_record_time >= 1.0:
                    self._price_history.append((now, price))
                    self._last_record_time = now

            if not self._connected.is_set():
                self._connected.set()
        except Exception as e:
            logger.debug(f"Binance WS parse error: {e}")

    def _on_error(self, ws, error):
        logger.warning(f"Binance WS error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info(f"Binance WS closed: {close_status_code} {close_msg}")
        self._connected.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Polymarket Feed (Simulated for Dry-Run)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PolymarketFeed:
    """
    In dry-run mode: simulates a 5-minute market using real BTC price.
    The oracle open price is captured at window start, and odds are
    computed from the spot delta.
    """

    def __init__(self, binance_feed: BinanceFeed):
        self._binance = binance_feed
        self._oracle_open = 0.0
        self._window_start = 0.0
        self._window_id = 0

    def start_new_window(self):
        """Capture the oracle open price at the start of a 5-min window."""
        self._oracle_open = self._binance.get_price()
        self._window_start = time.time()
        self._window_id += 1
        logger.info(
            f"[Window #{self._window_id}] Oracle open: "
            f"${self._oracle_open:,.2f}"
        )
        return self._oracle_open

    @property
    def oracle_open(self) -> float:
        return self._oracle_open

    @property
    def window_start(self) -> float:
        return self._window_start

    @property
    def window_id(self) -> int:
        return self._window_id

    def seconds_elapsed(self) -> float:
        return time.time() - self._window_start

    def seconds_remaining(self) -> float:
        return max(0, config.WINDOW_DURATION - self.seconds_elapsed())

    def get_spot_delta(self) -> float:
        """Current BTC spot - oracle open price."""
        return self._binance.get_price() - self._oracle_open

    def get_simulated_odds(self) -> dict:
        """
        Simulate market odds based on spot delta.
        Returns {'up': float, 'down': float, 'spread': float}
        """
        delta = self.get_spot_delta()
        # Odds move with delta; sensitivity is configurable
        up_odds = config.SIM_BASE_ODDS + (delta * config.SIM_ODDS_SENSITIVITY)
        up_odds = max(0.02, min(0.98, up_odds))
        down_odds = 1.0 - up_odds

        return {
            "up": round(up_odds, 4),
            "down": round(down_odds, 4),
            "spread": config.SIM_SPREAD,
        }

    def get_simulated_book(self) -> dict:
        """
        Simulate order book imbalance from spot delta.
        Positive delta → more bids (buyers), negative → more asks.
        """
        delta = self.get_spot_delta()
        # Normalize: $200 move = fully imbalanced
        norm = max(-1.0, min(1.0, delta / config.PRICE_NORMALIZATION_USD))
        bid_vol = 50_000 * (1 + norm * 0.5)
        ask_vol = 50_000 * (1 - norm * 0.5)
        return {
            "bid_volume": round(bid_vol, 2),
            "ask_volume": round(ask_vol, 2),
            "imbalance": round(
                (bid_vol - ask_vol) / (bid_vol + ask_vol), 4
            ),
        }

    def get_dynamic_fee(self, probability: float) -> float:
        """
        Dynamic taker fee: peaks at 50%, tapers to 0% at edges.
        fee = MAX_FEE × (1 − |2p − 1|)
        """
        return config.SIM_FEE_MAX * (1 - abs(2 * probability - 1))

    def resolve_window(self) -> dict:
        """
        Resolve the current window. Returns outcome.
        """
        close_price = self._binance.get_price()
        delta = close_price - self._oracle_open
        winner = "UP" if delta >= 0 else "DOWN"
        return {
            "window_id": self._window_id,
            "oracle_open": self._oracle_open,
            "close_price": close_price,
            "delta": delta,
            "winner": winner,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Optional: Live Polymarket API (for future use)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_active_btc_5m_markets() -> list:
    """
    Query Polymarket Gamma API for active BTC 5-minute markets.
    Returns a list of market dicts or empty list on failure.
    """
    try:
        resp = requests.get(
            f"{config.POLYMARKET_GAMMA_API}/markets",
            params={"active": "true", "closed": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        # Filter for BTC 5-minute markets
        btc_5m = [
            m for m in markets
            if "bitcoin" in m.get("question", "").lower()
            and ("5-minute" in m.get("question", "").lower()
                 or "5 minute" in m.get("question", "").lower())
        ]
        return btc_5m
    except Exception as e:
        logger.warning(f"Polymarket API error: {e}")
        return []
