"""Hyperliquid public market data (read-only; no key, no account, no orders): perp metadata and contexts for the
native dex (crypto) and the HIP-3 'xyz' dex (tokenized stocks, indices, commodities), candles, top of book, mids.
Fees follow Hyperliquid's docs: tier-0 taker 0.045% on validator-operated perps; HIP-3 perps scale it by
(deployerFeeScale + 1 if < 1 else 2 * deployerFeeScale) and by 0.1 in growth mode."""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.hyperliquid.xyz/info"
BASE_TAKER = 0.00045

# (coin on Hyperliquid, short label, category, plain description)
MARKETS = [
    ("BTC", "BTC", "crypto", "Bitcoin"),
    ("ETH", "ETH", "crypto", "Ether"),
    ("SOL", "SOL", "crypto", "Solana"),
    ("HYPE", "HYPE", "crypto", "Hyperliquid's token"),
    ("XRP", "XRP", "crypto", "XRP"),
    ("xyz:NVDA", "NVDA", "stock", "Nvidia, tokenized stock"),
    ("xyz:TSLA", "TSLA", "stock", "Tesla, tokenized stock"),
    ("xyz:AAPL", "AAPL", "stock", "Apple, tokenized stock"),
    ("xyz:META", "META", "stock", "Meta, tokenized stock"),
    ("xyz:SP500", "SP500", "index", "S&P 500 index"),
    ("xyz:XYZ100", "XYZ100", "index", "trade.xyz index of 100 large US companies"),
    ("xyz:GOLD", "GOLD", "commodity", "gold"),
    ("xyz:CL", "OIL", "commodity", "WTI crude oil"),
    ("xyz:SILVER", "SILVER", "commodity", "silver"),
]
COINS = [m[0] for m in MARKETS]
INTERVAL_MS = {"5m": 300_000, "1h": 3_600_000}


def info(body, timeout=15, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"hyperliquid {body.get('type')}: {type(last).__name__}: {last}")


def taker_fee(u):
    if ":" not in u["name"]:
        return BASE_TAKER
    s = float(u.get("deployerFeeScale", 1.0))
    scale = s + 1 if s < 1 else 2 * s
    return BASE_TAKER * scale * (0.1 if u.get("growthMode") == "enabled" else 1.0)


def contexts():
    out = {}
    for dex in (None, "xyz"):
        meta, ctx = info({"type": "metaAndAssetCtxs"} | ({"dex": dex} if dex else {}))
        for u, c in zip(meta["universe"], ctx):
            out[u["name"]] = {"mark": float(c["markPx"]), "oracle": float(c.get("oraclePx") or c["markPx"]),
                              "funding": float(c["funding"]), "oi_usd": float(c["openInterest"]) * float(c["markPx"]),
                              "vol24": float(c.get("dayNtlVlm") or 0), "prev_day": float(c.get("prevDayPx") or 0),
                              "fee": taker_fee(u), "delisted": bool(u.get("isDelisted"))}
    return out


def mids():
    m = info({"type": "allMids"})
    m.update(info({"type": "allMids", "dex": "xyz"}))
    return {k: float(v) for k, v in m.items() if k in COINS}


def candles(coin, interval, n):
    now = int(time.time() * 1000)
    c = info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                "startTime": now - (n + 1) * INTERVAL_MS[interval], "endTime": now}})
    return [float(x["c"]) for x in c][-n:]


def book(coin):
    lv = info({"type": "l2Book", "coin": coin})["levels"]
    return float(lv[0][0]["px"]), float(lv[1][0]["px"])


def snapshot(coins=COINS):
    """Everything one decision needs, fetched in parallel (~45 read-only requests, well under the rate limit)."""
    t = time.time()
    with ThreadPoolExecutor(8) as ex:
        f_ctx = ex.submit(contexts)
        f5 = {c: ex.submit(candles, c, "5m", 13) for c in coins}
        f1 = {c: ex.submit(candles, c, "1h", 25) for c in coins}
        fb = {c: ex.submit(book, c) for c in coins}
        ctx = f_ctx.result()
        snap = {"t": t, "ctx": {c: ctx[c] for c in coins}, "c5": {c: f5[c].result() for c in coins},
                "c1": {c: f1[c].result() for c in coins}, "book": {c: fb[c].result() for c in coins}}
    snap["mid"] = {c: (b + a) / 2 for c, (b, a) in snap["book"].items()}
    snap["fetch_s"] = time.time() - t
    return snap
