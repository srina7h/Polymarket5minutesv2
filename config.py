"""
Confirmation Sniper — Configuration

All tunable parameters for the trading system.
Everything is configurable here — no magic numbers in modules.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Strategy: Confirmation Sniper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# BTC must move at least this much from window open before we consider entry
MIN_DELTA_USD = 5.0   # $5 — lowered since indicators confirm direction

# Direction consistency — what fraction of recent ticks must agree
MIN_DIRECTION_CONSISTENCY = 0.49  # 49% — Pyth 1s polling creates exactly 50/50 flip-flops

# Volatility filter — skip chaotic windows where vol/avg ratio exceeds this
MAX_VOL_RATIO = 8.0  # Raised — the stdev/mean ratio is naturally high for BTC ticks

# Minimum odds lag (true_prob - market_odds) to trigger entry
MIN_ODDS_LAG = 0.01  # 1% — lowered since indicators provide confirmation

# Never buy shares priced above this (too expensive, low return)
MAX_ENTRY_PRICE = 0.85

# Minimum entry price — avoid buying too cheap (uncertain territory)
MIN_ENTRY_PRICE = 0.10

# Entry timing window (seconds elapsed into the 5-min window)
ENTRY_WINDOW_START = 180   # Earliest: 3:00 in (after 3-min confirmation)
ENTRY_WINDOW_END = 280     # Latest: 4:40 in (last 2 minutes)

# Exit deadline — close any open position by this point
EXIT_DEADLINE_SECS = 290   # 4:50 into window (10s before close)

# BTC reversal threshold — if BTC reverses this much against us, exit
REVERSAL_THRESHOLD = 0.0015  # 0.15%

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Indicator Configuration (3-min confirmation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Cumulative Volume Delta — normalized net buy/sell pressure
MIN_CVD_THRESHOLD = 0.1    # Normalized CVD must exceed this (-1 to +1 scale)

# Tick EMA periods (number of ticks)
EMA_FAST_PERIOD = 10       # Fast EMA (responsive)
EMA_SLOW_PERIOD = 50       # Slow EMA (smooth)

# VWAP deviation — price must be this far from VWAP (as fraction)
MIN_VWAP_DEVIATION = 0.0001  # 0.01% above/below VWAP

# Indicator consensus scoring
# CVD = 2pts, EMA = 1pt, VWAP = 1pt, Momentum = 1pt
# Total possible = 5
MIN_INDICATOR_SCORE = 4    # Need 4/5 points to confirm direction

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# True Probability Model (delta → probability lookup)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Maps abs(BTC delta) at entry time → probability direction persists to settlement
# Derived from backtesting: "How often does a delta of $X at T+150s persist to T+300s?"
PROB_TABLE = [
    (5,    0.55),
    (10,   0.60),
    (20,   0.67),
    (30,   0.73),
    (50,   0.80),
    (80,   0.86),
    (120,  0.91),
    (200,  0.95),
    (500,  0.98),
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Risk Management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TRADE_SIZE_USD = 25.0       # USD per trade
MAX_DAILY_LOSS = 100.0      # Kill switch threshold
MAX_TRADES_PER_DAY = 999999 # Unlimited
COOLDOWN_SEC = 0            # No cooldown between trades
MAX_POSITIONS = 1           # Only one active position at a time
MAX_CONSECUTIVE_LOSSES = 5  # Pause after this many losses in a row
LOSS_COOLDOWN_SEC = 300     # 5-min cooldown after consecutive losses

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LIMIT_TIMEOUT_SEC = 2.0     # How long to wait for POST_ONLY fill
MAX_SLIPPAGE = 0.03         # 3¢ max slippage for IOC fallback
ORDER_RETRY_COUNT = 2       # Retries on order failure

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Sources
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Binance WS — used for volume data (CVD indicator)
BINANCE_WS_URL = "wss://dstream.binance.com/ws/btcusd_perp@aggTrade"

# Chainlink BTC/USD on-chain oracle — THE price source Polymarket settles on
CHAINLINK_BTC_USD_ADDR = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
CHAINLINK_RPC_URL = "https://ethereum-rpc.publicnode.com"  # Free public Ethereum RPC
CHAINLINK_POLL_INTERVAL = 1.0  # Poll every 1 second

# Pyth Network BTC/USD — Secondary price feed (high-frequency oracle)
PYTH_HERMES_URL = "https://hermes.pyth.network"
PYTH_BTC_USD_ID = "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
PYTH_POLL_INTERVAL = 1.0  # Poll every 1 second

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Polymarket Auth (from .env)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Telegram
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DASHBOARD_PORT = 8080
WS_PUSH_INTERVAL = 0.5  # Push to dashboard every 500ms

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DRY_RUN = True              # No real orders when True
LOG_LEVEL = "INFO"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Watchdog
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HEARTBEAT_INTERVAL = 15     # Seconds between health checks
VPN_INTERFACE = "utun3"     # macOS VPN interface name (utun for WireGuard, etc.)
