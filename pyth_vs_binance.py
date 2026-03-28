import time
import requests

def test_binance():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1m", "limit": 5}
    t0 = time.time()
    resp = requests.get(url, params=params, timeout=5)
    lag = time.time() - t0
    data = resp.json()
    last_ts = data[-1][0] / 1000
    close = float(data[-1][4])
    return lag, last_ts, close

def test_pyth():
    end = int(time.time())
    start = end - 300
    url = f"https://benchmarks.pyth.network/v1/shims/tradingview/history?symbol=Crypto.BTC%2FUSD&resolution=1&from={start}&to={end}"
    t0 = time.time()
    resp = requests.get(url, timeout=5)
    lag = time.time() - t0
    data = resp.json()
    last_ts = data["t"][-1]
    close = float(data["c"][-1])
    return lag, last_ts, close

print("Testing Latency and Freshness...")
for i in range(3):
    b_lag, b_ts, b_close = test_binance()
    p_lag, p_ts, p_close = test_pyth()
    
    print(f"\\nRun {i+1}:")
    print(f"  Binance: Latency={b_lag*1000:.0f}ms, Price={b_close}, Freshness={int(time.time() - b_ts)}s ago")
    print(f"  Pyth:    Latency={p_lag*1000:.0f}ms, Price={p_close}, Freshness={int(time.time() - p_ts)}s ago")
    time.sleep(1)
