import time
import requests

def test_binance_usdc():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDC", "interval": "1m", "limit": 1}
    t0 = time.time()
    resp = requests.get(url, params=params, timeout=5)
    lag = time.time() - t0
    data = resp.json()
    last_ts = data[-1][0] / 1000
    close = float(data[-1][4])
    return lag, last_ts, close

def test_pyth_hermes():
    # Pyth's actual real-time streaming endpoint (Hermes)
    url = "https://hermes.pyth.network/v2/updates/price/latest?ids[]=e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
    t0 = time.time()
    resp = requests.get(url, timeout=5)
    lag = time.time() - t0
    data = resp.json()
    parsed = data["parsed"][0]["price"]
    price = float(parsed["price"]) * (10 ** parsed["expo"])
    publish_time = int(parsed["publish_time"])
    return lag, publish_time, price

print("Testing Latency and Freshness (Binance BTCUSDC vs Pyth Hermes BTC/USD)...")
for i in range(3):
    b_lag, b_ts, b_close = test_binance_usdc()
    p_lag, p_ts, p_close = test_pyth_hermes()
    
    print(f"\\nRun {i+1}:")
    print(f"  Binance BTCUSDC: Latency={b_lag*1000:.0f}ms, Price={b_close}, Freshness={int(time.time() - b_ts)}s ago (Kline open time)")
    print(f"  Pyth Hermes:     Latency={p_lag*1000:.0f}ms, Price={p_close:.2f}, Freshness={int(time.time() - p_ts)}s ago (Feed publish time)")
    time.sleep(1)
