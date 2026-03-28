import requests

def get_payout(start, direction):
    end = start + 300
    url = f"https://benchmarks.pyth.network/v1/shims/tradingview/history?symbol=Crypto.BTC%2FUSD&resolution=1&from={start}&to={end}"
    try:
        data = requests.get(url).json()
        op = float(data["o"][0])
        cl = float(data["c"][-1])
        won = False
        if direction == "UP" and cl > op: won = True
        if direction == "DOWN" and cl < op: won = True
        
        # Calculate theoretical payout
        if getattr(data, "s", "ok") == "ok":
            print(f"Window {start}: Direction={direction}, Open={op:.2f}, Close={cl:.2f} -> {'WIN +$5' if won else 'LOSS -$5'}")
    except Exception as e:
        print(f"Error on {start}: {e}")

get_payout(1774470300, "DOWN")
get_payout(1774470900, "UP")
