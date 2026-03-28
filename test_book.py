import requests
import json
import time

slug = f"btc-updown-5m-{(int(time.time()) // 300) * 300}"
print(f"SLUG: {slug}")
resp = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}).json()
if not len(resp):
    print("Market not up yet")
else:
    tokens = json.loads(resp[0]["clobTokenIds"])
    print(f"Token: {tokens[0]}")
    book = requests.get(f"https://clob.polymarket.com/book?token_id={tokens[0]}").json()
    print("BIDS:", book.get("bids", [])[:1])
    print("ASKS:", book.get("asks", [])[:1])
