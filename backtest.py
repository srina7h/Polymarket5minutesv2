#!/usr/bin/env python3
"""
Confirmation Sniper — Backtester

Fetches 1 week of BTC 1-minute candles from Binance, simulates 5-minute
Polymarket windows, and runs the full signal detector logic against them.

Usage:
    python backtest.py                  # Default: 7 days
    python backtest.py --days 14        # Custom duration
    python backtest.py --days 30 --verbose
"""

import argparse
import json
import math
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Configuration (mirrors config.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import config


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Data Structures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Candle:
    timestamp: int     # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Window:
    """A 5-minute Polymarket-style window."""
    start_ts: int          # Unix ms
    candles: list          # List of Candle (5 × 1min)
    open_price: float = 0.0
    close_price: float = 0.0
    outcome: str = ""      # "UP" or "DOWN"


@dataclass
class BacktestTrade:
    window_idx: int
    direction: str         # "UP" or "DOWN"
    entry_price: float     # What we paid per share (market odds)
    entry_time_secs: int   # Seconds into window when we entered
    btc_delta: float       # BTC delta at entry
    true_prob: float       # Our model's probability
    odds_lag: float        # Edge: true_prob - market_odds
    outcome: str = ""      # "WIN" or "LOSS"
    pnl: float = 0.0


@dataclass  
class BacktestResult:
    total_windows: int = 0
    trades: list = field(default_factory=list)
    gate_blocks: dict = field(default_factory=lambda: {
        "timing": 0, "already_traded": 0, "delta": 0,
        "consistency": 0, "volatility": 0, "indicators": 0, "odds_lag": 0,
        "price_bounds": 0, "no_market": 0,
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANSI colors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[91m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
    CYAN = "\033[96m"; MAGENTA = "\033[95m"; WHITE = "\033[97m"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fetch Data from Binance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_btc_candles(days: int) -> list[Candle]:
    """Fetch 1-minute BTC/USD candles from Binance coin-margined futures."""
    print(f"\n{C.CYAN}{'━' * 60}{C.RESET}")
    print(f"{C.BOLD}  📡 Fetching {days} days of BTC/USD 1min data from Binance...{C.RESET}")
    print(f"{C.CYAN}{'━' * 60}{C.RESET}\n")

    candles = []
    total_needed = days * 24 * 60  # 1min candles
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)
    
    current_start = start_ms
    batch = 0
    
    while current_start < end_ms:
        batch += 1
        # Primary: Binance coin-margined futures (BTCUSD_PERP)
        url = "https://dapi.binance.com/dapi/v1/klines"
        params = {
            "symbol": "BTCUSD_PERP",
            "interval": "1m",
            "startTime": current_start,
            "limit": 1000,
        }
        
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  {C.RED}✗ Batch {batch} failed: {e}{C.RESET}")
            # Fallback to spot BTCUSDT if futures unavailable
            try:
                url = "https://api.binance.com/api/v3/klines"
                params["symbol"] = "BTCUSDT"
                resp = requests.get(url, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e2:
                print(f"  {C.RED}✗ Backup also failed: {e2}. Stopping.{C.RESET}")
                break
        
        if not data:
            break
            
        for k in data:
            candles.append(Candle(
                timestamp=int(k[0]),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
            ))
        
        # Move forward
        current_start = int(data[-1][0]) + 60000  # Next minute
        
        pct = min(len(candles) / total_needed * 100, 100)
        print(f"  Batch {batch}: {len(candles):,} candles ({pct:.0f}%)", end="\r")
        
        time.sleep(0.2)  # Rate limit
    
    print(f"\n  {C.GREEN}✓ Fetched {len(candles):,} candles{C.RESET}")
    
    if candles:
        t0 = datetime.fromtimestamp(candles[0].timestamp / 1000, tz=timezone.utc)
        t1 = datetime.fromtimestamp(candles[-1].timestamp / 1000, tz=timezone.utc)
        print(f"  Range: {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} UTC")
    
    return candles


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Build 5-Minute Windows
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_windows(candles: list[Candle]) -> list[Window]:
    """Group 1-min candles into 5-minute windows."""
    windows = []
    
    # Sort and align to 5-min boundaries
    candles.sort(key=lambda c: c.timestamp)
    
    i = 0
    while i <= len(candles) - 5:
        # Check if these 5 candles form a contiguous 5-min block
        c0 = candles[i]
        
        # Align to 5-min boundary
        minute = (c0.timestamp // 60000) % 5
        if minute != 0:
            i += 1
            continue
        
        # Grab 5 consecutive candles
        group = candles[i:i+5]
        
        # Verify they're actually consecutive minutes
        gaps_ok = all(
            abs(group[j+1].timestamp - group[j].timestamp - 60000) < 5000
            for j in range(4)
        )
        
        if gaps_ok:
            w = Window(
                start_ts=group[0].timestamp,
                candles=group,
                open_price=group[0].open,
                close_price=group[-1].close,
            )
            w.outcome = "UP" if w.close_price > w.open_price else "DOWN"
            windows.append(w)
            i += 5
        else:
            i += 1
    
    print(f"  {C.GREEN}✓ Built {len(windows):,} 5-minute windows{C.RESET}")
    print(f"  Outcomes: {sum(1 for w in windows if w.outcome=='UP'):,} UP / "
          f"{sum(1 for w in windows if w.outcome=='DOWN'):,} DOWN")
    
    return windows


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Simulate Market Odds
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def simulate_market_odds(btc_delta: float, elapsed_secs: int) -> tuple[float, float]:
    """
    Simulate Polymarket YES/NO midpoints based on BTC movement.
    
    Key insight: Market odds REACT to BTC but with LAG.
    Early in the window, odds stay near 50/50.
    As the window progresses and BTC moves, they shift — but slowly.
    
    This lag is our edge.
    """
    # Base: 50/50
    base = 0.50
    
    # Market reaction: odds shift proportionally to delta, but lagged
    # The market fully prices in ~$100 of BTC movement by end of window
    delta_impact = btc_delta / 200.0  # $100 move → ±0.50 shift
    delta_impact = max(-0.45, min(0.45, delta_impact))  # Clamp
    
    # Time lag: market gets more efficient as window progresses
    # At 60s, market has only absorbed 30% of the move
    # At 150s, market has absorbed 60%
    # At 250s, market has absorbed 90%
    if elapsed_secs < 30:
        lag_factor = 0.15
    elif elapsed_secs < 60:
        lag_factor = 0.25
    elif elapsed_secs < 120:
        lag_factor = 0.40
    elif elapsed_secs < 180:
        lag_factor = 0.60
    elif elapsed_secs < 240:
        lag_factor = 0.75
    else:
        lag_factor = 0.90
    
    # Apply lagged impact
    yes_mid = base + (delta_impact * lag_factor)
    yes_mid = max(0.05, min(0.95, yes_mid))
    no_mid = 1.0 - yes_mid
    
    return round(yes_mid, 4), round(no_mid, 4)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# True Probability (copy from signal_detector)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_true_probability(btc_delta: float) -> float:
    """Map |BTC delta| to persistence probability using config table."""
    abs_delta = abs(btc_delta)
    table = config.PROB_TABLE
    
    if abs_delta <= table[0][0]:
        return table[0][1]
    if abs_delta >= table[-1][0]:
        return table[-1][1]
    
    for i in range(1, len(table)):
        if abs_delta <= table[i][0]:
            d_lo, p_lo = table[i - 1]
            d_hi, p_hi = table[i]
            t = (abs_delta - d_lo) / (d_hi - d_lo)
            return p_lo + t * (p_hi - p_lo)
    
    return table[-1][1]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Run Backtest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_backtest(windows: list[Window], verbose: bool = False) -> BacktestResult:
    """
    Simulate the Confirmation Sniper strategy against historical windows.
    
    For each 5-minute window (5 × 1-min candles):
    - Candles 1-3 (0-180s): Build indicator picture (CVD, EMA, VWAP, Momentum)
    - Candles 4-5 (180-300s): Evaluate gates and take trade if confirmed
    
    Uses actual 1-min OHLCV data — no fake interpolation.
    """
    print(f"\n{C.CYAN}{'━' * 60}{C.RESET}")
    print(f"{C.BOLD}  🎯 Running Confirmation Sniper Backtest{C.RESET}")
    print(f"{C.CYAN}{'━' * 60}{C.RESET}")
    print(f"  Config: delta≥${config.MIN_DELTA_USD} | "
          f"indicators≥{config.MIN_INDICATOR_SCORE}/5 | "
          f"vol<{config.MAX_VOL_RATIO} | "
          f"odds_lag≥{config.MIN_ODDS_LAG:.0%}")
    print(f"  Window: 3-min confirm → 2-min execute | "
          f"Trade size: ${config.TRADE_SIZE_USD}\n")
    
    result = BacktestResult(total_windows=len(windows))
    
    for idx, window in enumerate(windows):
        traded = False
        open_price = window.open_price
        
        # ══════════════════════════════════════════════
        # Phase 1: Build indicators candle-by-candle
        # ══════════════════════════════════════════════
        buy_volume = 0.0
        sell_volume = 0.0
        ema_fast = open_price
        ema_slow = open_price
        vwap_num = 0.0
        vwap_den = 0.0
        vwap = open_price
        candle_closes = []        # Track close prices for momentum
        direction_candles = []    # Track candle direction (+1/-1)
        
        for ci, candle in enumerate(window.candles):
            elapsed_end = (ci + 1) * 60  # End second of this candle
            
            # ── Update indicators with this candle ──
            
            # CVD: bullish candle (close > open) → buy volume, else sell
            usd_vol = candle.close * candle.volume
            if candle.close >= candle.open:
                buy_volume += usd_vol
                direction_candles.append(1)
            else:
                sell_volume += usd_vol
                direction_candles.append(-1)
            
            total_vol = buy_volume + sell_volume
            cvd = (buy_volume - sell_volume) / total_vol if total_vol > 0 else 0.0
            
            # EMA on close prices
            k_fast = 2.0 / (config.EMA_FAST_PERIOD + 1)
            k_slow = 2.0 / (config.EMA_SLOW_PERIOD + 1)
            ema_fast = candle.close * k_fast + ema_fast * (1 - k_fast)
            ema_slow = candle.close * k_slow + ema_slow * (1 - k_slow)
            
            # VWAP
            vwap_num += candle.close * usd_vol
            vwap_den += usd_vol
            if vwap_den > 0:
                vwap = vwap_num / vwap_den
            
            # Track closes for momentum
            candle_closes.append(candle.close)
            
            # BTC delta at this candle's close
            btc_delta = candle.close - open_price
            
            # ── Only evaluate for trade on candles 4 and 5 (180-300s) ──
            if elapsed_end <= 180:
                continue  # Still confirming
            
            if traded:
                continue
            
            # Current price is this candle's close
            current_price = candle.close
            
            # Momentum: price change from first candle's close
            momentum_pct = 0.0
            if len(candle_closes) >= 2 and candle_closes[0] > 0:
                momentum_pct = (current_price - candle_closes[0]) / candle_closes[0]
            
            # ── Gate 3: BTC delta magnitude ──
            if abs(btc_delta) < config.MIN_DELTA_USD:
                if ci == 4:  # Last candle
                    result.gate_blocks["delta"] += 1
                continue
            
            # ── Gate 4: Direction consistency ──
            # What fraction of candles agree with delta direction?
            if btc_delta > 0:
                consistency = sum(1 for d in direction_candles if d > 0) / len(direction_candles)
            else:
                consistency = sum(1 for d in direction_candles if d < 0) / len(direction_candles)
            
            if consistency < config.MIN_DIRECTION_CONSISTENCY:
                if ci == 4:
                    result.gate_blocks["consistency"] += 1
                continue
            
            # ── Gate 5: Volatility ──
            if len(candle_closes) >= 3:
                deltas = [c - open_price for c in candle_closes]
                vol_std = statistics.stdev(deltas)
                vol_mean = abs(statistics.mean(deltas)) + 0.01
                vol_ratio = vol_std / vol_mean
            else:
                vol_ratio = 0.0
            
            if vol_ratio > config.MAX_VOL_RATIO:
                if ci == 4:
                    result.gate_blocks["volatility"] += 1
                continue
            
            # ── Gate 6: Indicator Consensus Scoring ──
            up_score = 0
            down_score = 0
            
            # CVD (2 points)
            if cvd > config.MIN_CVD_THRESHOLD:
                up_score += 2
            elif cvd < -config.MIN_CVD_THRESHOLD:
                down_score += 2
            
            # EMA crossover (1 point) — always has enough data (5 candles)
            if ema_fast > ema_slow:
                up_score += 1
            elif ema_fast < ema_slow:
                down_score += 1
            
            # VWAP deviation (1 point)
            if vwap > 0 and current_price > 0:
                vwap_dev = (current_price - vwap) / vwap
                if vwap_dev > config.MIN_VWAP_DEVIATION:
                    up_score += 1
                elif vwap_dev < -config.MIN_VWAP_DEVIATION:
                    down_score += 1
            
            # Momentum (1 point)
            if momentum_pct > 0.0001:
                up_score += 1
            elif momentum_pct < -0.0001:
                down_score += 1
            
            # Determine consensus
            if up_score > down_score:
                ind_score = up_score
                ind_dir = "UP"
            elif down_score > up_score:
                ind_score = down_score
                ind_dir = "DOWN"
            else:
                ind_score = max(up_score, down_score)
                ind_dir = "FLAT"
            
            if ind_score < config.MIN_INDICATOR_SCORE or ind_dir == "FLAT":
                if ci == 4:
                    result.gate_blocks["indicators"] += 1
                continue
            
            # Use indicator-confirmed direction
            direction = ind_dir
            
            # ── Gate 7: True probability ──
            true_prob = compute_true_probability(btc_delta)
            
            # ── Gate 8: Odds lag ──
            yes_mid, no_mid = simulate_market_odds(btc_delta, elapsed_end)
            market_odds = yes_mid if direction == "UP" else no_mid
            
            odds_lag = true_prob - market_odds
            if odds_lag < config.MIN_ODDS_LAG:
                if ci == 4:
                    result.gate_blocks["odds_lag"] += 1
                continue
            
            # ── Gate 9: Price bounds ──
            if market_odds > config.MAX_ENTRY_PRICE or market_odds < config.MIN_ENTRY_PRICE:
                if ci == 4:
                    result.gate_blocks["price_bounds"] += 1
                continue
            
            # ═══ ALL GATES PASSED — TAKE TRADE ═══
            traded = True
            
            # Determine outcome
            trade_won = (direction == window.outcome)
            
            # PnL calculation
            shares = config.TRADE_SIZE_USD / market_odds
            if trade_won:
                pnl = (1.0 - market_odds) * shares  # Profit
            else:
                pnl = -market_odds * shares          # Loss (entire cost)
            
            trade = BacktestTrade(
                window_idx=idx,
                direction=direction,
                entry_price=round(market_odds, 4),
                entry_time_secs=elapsed_end,
                btc_delta=round(btc_delta, 2),
                true_prob=round(true_prob, 4),
                odds_lag=round(odds_lag, 4),
                outcome="WIN" if trade_won else "LOSS",
                pnl=round(pnl, 2),
            )
            
            result.trades.append(trade)
            
            ts = datetime.fromtimestamp(window.start_ts / 1000, tz=timezone.utc)
            
            if verbose:
                color = C.GREEN if trade_won else C.RED
                print(
                    f"  {ts:%m/%d %H:%M} | {color}{trade.outcome:4s}{C.RESET} | "
                    f"{direction:4s} | Entry:{market_odds:.2f} @{elapsed_end}s | "
                    f"Δ${btc_delta:+.0f} | CVD:{cvd:+.2f} Score:{ind_score}/5 | "
                    f"Lag:{odds_lag:.0%} | PnL: {color}${pnl:+.2f}{C.RESET}"
                )
        
        # Progress
        if (idx + 1) % 200 == 0:
            pct = (idx + 1) / len(windows) * 100
            print(f"  Processing... {pct:.0f}% ({idx+1}/{len(windows)} windows)", end="\r")
    
    print(f"  {C.GREEN}✓ Backtest complete{C.RESET}                                    ")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Print Results
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def print_results(result: BacktestResult):
    """Print formatted backtest results."""
    trades = result.trades
    wins = [t for t in trades if t.outcome == "WIN"]
    losses = [t for t in trades if t.outcome == "LOSS"]
    
    total_pnl = sum(t.pnl for t in trades)
    
    print(f"\n{C.CYAN}{'═' * 60}{C.RESET}")
    print(f"{C.BOLD}  📊 BACKTEST RESULTS{C.RESET}")
    print(f"{C.CYAN}{'═' * 60}{C.RESET}\n")
    
    # Overview
    print(f"  {C.BOLD}Overview{C.RESET}")
    print(f"  {'─' * 45}")
    print(f"  Total Windows Scanned:  {result.total_windows:,}")
    print(f"  Total Trades Taken:     {len(trades):,}")
    print(f"  Trade Frequency:        {len(trades)/max(result.total_windows,1)*100:.1f}% of windows")
    print()
    
    if not trades:
        print(f"  {C.YELLOW}No trades were triggered. Strategy may be too strict.{C.RESET}")
        print(f"\n  {C.BOLD}Gate Block Breakdown:{C.RESET}")
        for gate, count in sorted(result.gate_blocks.items(), key=lambda x: -x[1]):
            if count > 0:
                print(f"    {gate:20s}: {count:,}")
        return
    
    # Win/Loss
    win_rate = len(wins) / len(trades) * 100
    print(f"  {C.BOLD}Performance{C.RESET}")
    print(f"  {'─' * 45}")
    
    wr_color = C.GREEN if win_rate >= 55 else C.YELLOW if win_rate >= 50 else C.RED
    pnl_color = C.GREEN if total_pnl > 0 else C.RED
    
    print(f"  Win Rate:               {wr_color}{win_rate:.1f}%{C.RESET} ({len(wins)}W / {len(losses)}L)")
    print(f"  Total PnL:              {pnl_color}${total_pnl:+,.2f}{C.RESET}")
    
    if wins:
        avg_win = sum(t.pnl for t in wins) / len(wins)
        max_win = max(t.pnl for t in wins)
        print(f"  Avg Win:                {C.GREEN}${avg_win:+.2f}{C.RESET}")
        print(f"  Max Win:                {C.GREEN}${max_win:+.2f}{C.RESET}")
    
    if losses:
        avg_loss = sum(t.pnl for t in losses) / len(losses)
        max_loss = min(t.pnl for t in losses)
        print(f"  Avg Loss:               {C.RED}${avg_loss:+.2f}{C.RESET}")
        print(f"  Max Loss:               {C.RED}${max_loss:+.2f}{C.RESET}")
    
    # Profit factor
    gross_profit = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.01
    profit_factor = gross_profit / gross_loss
    pf_color = C.GREEN if profit_factor > 1.5 else C.YELLOW if profit_factor > 1.0 else C.RED
    print(f"  Profit Factor:          {pf_color}{profit_factor:.2f}{C.RESET}")
    
    # Expected value per trade
    ev = total_pnl / len(trades)
    ev_color = C.GREEN if ev > 0 else C.RED
    print(f"  Expected Value/Trade:   {ev_color}${ev:+.2f}{C.RESET}")
    
    # Drawdown
    equity = []
    running = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running += t.pnl
        equity.append(running)
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)
    
    print(f"  Max Drawdown:           {C.RED}-${max_dd:.2f}{C.RESET}")
    
    # Streaks
    max_win_streak = 0
    max_loss_streak = 0
    current_streak = 0
    last_outcome = None
    for t in trades:
        if t.outcome == last_outcome:
            current_streak += 1
        else:
            current_streak = 1
            last_outcome = t.outcome
        if t.outcome == "WIN":
            max_win_streak = max(max_win_streak, current_streak)
        else:
            max_loss_streak = max(max_loss_streak, current_streak)
    
    print(f"  Max Win Streak:         {C.GREEN}{max_win_streak}{C.RESET}")
    print(f"  Max Loss Streak:        {C.RED}{max_loss_streak}{C.RESET}")
    
    # Entry timing
    print()
    print(f"  {C.BOLD}Entry Analysis{C.RESET}")
    print(f"  {'─' * 45}")
    avg_entry_time = sum(t.entry_time_secs for t in trades) / len(trades)
    avg_entry_price = sum(t.entry_price for t in trades) / len(trades)
    avg_delta = sum(abs(t.btc_delta) for t in trades) / len(trades)
    avg_lag = sum(t.odds_lag for t in trades) / len(trades)
    
    print(f"  Avg Entry Time:         {avg_entry_time:.0f}s into window")
    print(f"  Avg Entry Price:        {avg_entry_price:.2f}¢")
    print(f"  Avg BTC Delta:          ${avg_delta:.1f}")
    print(f"  Avg Odds Lag:           {avg_lag:.1%}")
    
    # Direction split
    up_trades = [t for t in trades if t.direction == "UP"]
    dn_trades = [t for t in trades if t.direction == "DOWN"]
    up_wr = sum(1 for t in up_trades if t.outcome == "WIN") / max(len(up_trades), 1) * 100
    dn_wr = sum(1 for t in dn_trades if t.outcome == "WIN") / max(len(dn_trades), 1) * 100
    
    print(f"  UP Trades:              {len(up_trades)} ({up_wr:.0f}% WR)")
    print(f"  DOWN Trades:            {len(dn_trades)} ({dn_wr:.0f}% WR)")
    
    # Gate blocks
    print()
    print(f"  {C.BOLD}Gate Block Breakdown{C.RESET}")
    print(f"  {'─' * 45}")
    for gate, count in sorted(result.gate_blocks.items(), key=lambda x: -x[1]):
        if count > 0:
            bar = "█" * min(count // max(result.total_windows // 50, 1), 30)
            print(f"    {gate:20s}: {count:>5,}  {C.DIM}{bar}{C.RESET}")

    # Equity curve (ASCII)
    print()
    print(f"  {C.BOLD}Equity Curve{C.RESET}")
    print(f"  {'─' * 45}")
    
    if equity:
        min_eq = min(equity)
        max_eq = max(equity)
        height = 8
        width = min(len(equity), 60)
        step = max(len(equity) // width, 1)
        sampled = equity[::step][:width]
        
        for row in range(height, -1, -1):
            threshold = min_eq + (max_eq - min_eq) * row / height
            line = ""
            for val in sampled:
                if val >= threshold:
                    line += "█"
                else:
                    line += " "
            
            if row == height:
                label = f"${max_eq:+.0f}"
            elif row == 0:
                label = f"${min_eq:+.0f}"
            else:
                label = ""
            
            print(f"  {label:>8s} │{line}│")
        
        print(f"          └{'─' * len(sampled)}┘")
        print(f"           {'T=0':<{len(sampled)//2}}{'T=end':>{len(sampled)//2}}")
    
    print(f"\n{C.CYAN}{'═' * 60}{C.RESET}\n")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(description="Confirmation Sniper Backtester")
    parser.add_argument("--days", type=int, default=7, help="Number of days to backtest (default: 7)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print each trade")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()
    
    import random
    random.seed(args.seed)
    
    # Cache data to avoid re-fetching
    cache_file = f"/tmp/btcusd_1m_{args.days}d.json"
    candles = []
    
    if os.path.exists(cache_file):
        age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
        if age_hours < 4:
            print(f"  {C.DIM}Loading cached data ({age_hours:.1f}h old)...{C.RESET}")
            with open(cache_file) as f:
                raw = json.load(f)
                candles = [Candle(**c) for c in raw]
    
    if not candles:
        candles = fetch_btc_candles(args.days)
        # Cache
        with open(cache_file, "w") as f:
            json.dump([vars(c) for c in candles], f)
    
    if len(candles) < 10:
        print(f"\n  {C.RED}Not enough data fetched. Check network/VPN.{C.RESET}")
        return
    
    # Build windows
    windows = build_windows(candles)
    
    if not windows:
        print(f"\n  {C.RED}No valid 5-minute windows found.{C.RESET}")
        return
    
    # Run backtest
    result = run_backtest(windows, verbose=args.verbose)
    
    # Print results
    print_results(result)


if __name__ == "__main__":
    main()
