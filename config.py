"""
LEMA Trading Bot — Configuration
All tunable parameters from the Polymarket 5-Min Trading Framework.
"""

# ─────────────────────────────────────────────
# Mode
# ─────────────────────────────────────────────
DRY_RUN = True  # False = live trading (not implemented in prototype)

# ─────────────────────────────────────────────
# Capital & Position Sizing
# ─────────────────────────────────────────────
INITIAL_CAPITAL = 10_000.00       # Starting paper capital (USDC)
MAX_POSITION_PCT = 0.02           # 2% of capital per trade
KELLY_FRACTION = 0.25             # 25% of full Kelly
CASH_RESERVE_PCT = 0.30           # Always keep 30% uninvested

# ─────────────────────────────────────────────
# Edge Score Thresholds
# ─────────────────────────────────────────────
MIN_EDGE_THRESHOLD = 0.08         # 8% — minimum to enter
HIGH_CONVICTION_THRESHOLD = 0.15  # 15% — use full position size
EDGE_WEIGHTS = {
    "price_edge": 0.40,
    "momentum": 0.30,
    "sentiment": 0.15,
    "book_imbalance": 0.15,
}

# ─────────────────────────────────────────────
# LEMA Timing (seconds into 5-min window)
# ─────────────────────────────────────────────
WINDOW_DURATION = 300             # 5 minutes
OBSERVATION_END = 180             # First 3 min: observe only
EVALUATION_END = 240              # 3–4 min: evaluate
ENTRY_START = 240                 # Earliest entry at 4:00
ENTRY_END = 270                   # Latest entry at 4:30
MIN_TIME_REMAINING = 30           # Don't enter with < 30s left

# ─────────────────────────────────────────────
# Entry Criteria
# ─────────────────────────────────────────────
MIN_DIRECTION_CONSISTENCY = 3     # ≥3 of 4 one-minute intervals same dir
MIN_SPOT_DELTA_USD = 50.0         # BTC must move ≥$50 from open
MAX_VOLATILITY_MULTIPLIER = 2.0   # Current vol ≤ 2× rolling avg
PRICE_NORMALIZATION_USD = 200.0   # $200 move = max magnitude (1.0)

# ─────────────────────────────────────────────
# Risk Management
# ─────────────────────────────────────────────
MAX_DAILY_LOSS_PCT = 0.06         # 6% of capital = stop for day
MAX_CONSECUTIVE_LOSSES = 3        # Pause after 3 in a row
COOLDOWN_SECONDS = 1800           # 30m cool-down after consecutive losses
MAX_TRADES_PER_DAY = 15
MAX_SLIPPAGE = 0.03               # $0.03 max slippage tolerance

# ─────────────────────────────────────────────
# Simulated Market Parameters (Dry-Run)
# ─────────────────────────────────────────────
SIM_BASE_ODDS = 0.50              # Starting odds for each window
SIM_ODDS_SENSITIVITY = 0.003     # How much odds move per $1 of BTC delta
SIM_SPREAD = 0.02                 # Simulated bid-ask spread
SIM_FEE_MAX = 0.0156              # 1.56% max dynamic taker fee at 50%

# ─────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"

# ─────────────────────────────────────────────
# EV Gap Strategy
# ─────────────────────────────────────────────
EV_MIN_GAP = 0.07              # 7% minimum EV gap (optimized via 3-day sweep)
EV_MAX_MARKET_PROB = 0.75      # Skip if market already moved > 75%
EV_FLAT_POSITION_PCT = 0.05    # 5% of capital per trade (flat sizing)

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
TRADE_JOURNAL_FILE = "trade_journal.csv"
LOG_LEVEL = "INFO"
TICK_INTERVAL = 1.0               # Main loop tick in seconds
