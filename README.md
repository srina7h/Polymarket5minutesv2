# LEMA Trading Bot — Polymarket 5-Min BTC

A working prototype of the **Late-Entry Momentum Arbitrage (LEMA)** strategy for Polymarket 5-minute BTC UP/DOWN prediction markets.

## What It Does

- Streams **real-time BTC prices** from Binance WebSocket
- Simulates 5-minute Polymarket windows aligned to clock boundaries
- Computes a **4-component Edge Score** (Price Edge, Momentum, Sentiment, Book Imbalance)
- Applies the **LEMA strategy**: observe for 3 min, evaluate, enter only when edge ≥ 8%
- Enforces strict **risk management** (2% max per trade, 6% daily stop, 3-loss cooldown)
- Logs every decision to `trade_journal.csv`
- Shows a real-time **terminal dashboard** with progress bars and colored P&L

## Quick Start

```bash
cd /Users/srinath/Documents/Polymarket5mins

# Install dependencies
pip install -r requirements.txt

# Run dry-run (paper trading with real BTC prices)
python main.py --dry-run

# Run for exactly 3 windows (15 minutes)
python main.py --dry-run --windows 3

# Run for 30 minutes
python main.py --dry-run --duration 30

# Custom starting capital
python main.py --dry-run --capital 5000 --windows 5
```

## Architecture

```
main.py              → Orchestrator: tick loop, window management, shutdown
config.py            → All tunable parameters
data_feeds.py        → Binance WebSocket + simulated Polymarket feed
edge_calculator.py   → 4-component Edge Score model
strategy.py          → LEMA decision engine (observe/evaluate/execute/hold)
risk_manager.py      → Risk rules: daily loss, consecutive losses, trade caps
trade_logger.py      → CSV journal + ANSI terminal dashboard
```

## Strategy Summary

| Phase | Time | Action |
|-------|------|--------|
| OBSERVE | 0–3 min | Collect BTC price data, do nothing |
| EVALUATE | 3–4 min | Compute edge score, check all 5 entry criteria |
| EXECUTE | 4–4.5 min | Enter if edge ≥ 8% with modified Kelly sizing |
| HOLD | 4.5–5 min | Wait for automatic settlement |

### Entry Criteria (ALL must pass)

1. Time remaining: 30–60 seconds
2. BTC spot moved ≥ $50 from oracle open
3. Direction consistent ≥ 3 of 4 minutes
4. Volatility ≤ 2× rolling average
5. Edge Score ≥ 8%

## Risk Management

| Rule | Value |
|------|-------|
| Max per trade | 2% of capital |
| Max daily loss | 6% → stop for day |
| Consecutive losses | 3 → 30min cooldown |
| Max trades/day | 15 |
| Position sizing | 25% of Kelly criterion |

## Output

The bot produces:
- **Terminal dashboard**: Real-time BTC price, delta, phase, edge scores, trade entries, P&L
- **trade_journal.csv**: Complete log of every window with 20 columns for analysis

## ⚠️ Disclaimer

This is a **dry-run prototype** for research and education. It does NOT place real trades. Prediction market trading involves risk of loss. Past patterns do not guarantee future results.
