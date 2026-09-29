"""Hyperliquid API helpers (info endpoint only; orders live in desk.py)."""
import json, time, urllib.request

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"
BAR_MS = 4 * 3600 * 1000


def info(body, base=MAINNET, tries=5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(base + "/info", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=40))
        except Exception as e:  # rate limits and transient errors
            last = e
            time.sleep(2 + 3 * i)
    raise RuntimeError(f"info {body.get('type')} failed: {last}")


def universe(base=MAINNET):
    """Active perps with 24h notional volume, open interest and current hourly funding."""
    meta, ctx = info({"type": "metaAndAssetCtxs"}, base)
    out = []
    for u, c in zip(meta["universe"], ctx):
        if u.get("isDelisted"):
            continue
        out.append({"coin": u["name"], "szDecimals": u["szDecimals"], "maxLeverage": u["maxLeverage"],
                    "vlm24h": float(c.get("dayNtlVlm") or 0), "mark": float(c.get("markPx") or 0),
                    "oi": float(c.get("openInterest") or 0) * float(c.get("markPx") or 0),
                    "funding": float(c.get("funding") or 0)})
    return out


def candles(coin, bars, now_ms=None, base=MAINNET):
    now_ms = now_ms or int(time.time() * 1000)
    c = info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "4h",
                                                "startTime": now_ms - bars * BAR_MS, "endTime": now_ms}}, base)
    return [x for x in c if x["T"] < now_ms]          # closed bars only


def funding_sum(coin, t0, t1, base=MAINNET):
    rows, s = [], t0
    while s < t1:
        f = info({"type": "fundingHistory", "coin": coin, "startTime": s, "endTime": t1}, base)
        if not f:
            break
        rows += f; s = f[-1]["time"] + 1
        if len(f) < 500:
            break
    return sum(float(r["fundingRate"]) for r in rows)


def mids(base=MAINNET):
    return {k: float(v) for k, v in info({"type": "allMids"}, base).items() if not k.startswith("@")}


def account(address, base=TESTNET):
    us = info({"type": "clearinghouseState", "user": address}, base)
    pos = {}
    for p in us["assetPositions"]:
        q = p["position"]; szi = float(q["szi"])
        if szi:
            pos[q["coin"]] = {"szi": szi, "entry": float(q["entryPx"]), "upnl": float(q["unrealizedPnl"])}
    return float(us["marginSummary"]["accountValue"]), pos
