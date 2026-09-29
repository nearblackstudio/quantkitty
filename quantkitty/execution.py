"""Order execution for the testnet mirror, and measurement of what it cost.

Each rebalance goes out as a post-only (add-liquidity-only) limit order at the
best bid (buys) or best ask (sells), so it can only fill as a maker. After
WAIT_S seconds, anything unfilled is cancelled and the remainder is sent as a
market order. Every fill is then measured against the mid price at decision
time, which gives the real all-in cost (fee plus price impact) in basis points.

Those measurements feed the paper book: once enough fills have been observed,
the paper book's trading costs are scaled by measured cost / modelled taker cost,
so the paper results only get the benefit that limit orders actually delivered.
"""
import math, time

from . import hl

WAIT_S = 60
MIN_ORDER_USD = 10.0
MIN_FILLS_FOR_PAPER = 30          # observed fills needed before the paper book uses measured costs
RATIO_FLOOR, RATIO_CAP = 0.5, 1.5 # testnet books are thinner than mainnet: never assume more than half off


def round_px(px, sz_decimals):
    """Hyperliquid perp prices: at most 5 significant figures and 6 - szDecimals decimals."""
    if px >= 100_000:
        return float(round(px))
    return round(float(f"{px:.5g}"), max(0, 6 - sz_decimals))


def _status(r):
    if not isinstance(r, dict) or r.get("status") != "ok":
        return {"error": str(r)}
    return r["response"]["data"]["statuses"][0]


def rebalance(ex, acct, deltas, szd, reduce_only, log):
    """deltas: {coin: signed size change in coins}. Returns per-coin order records.

    Phase 1: post-only limit at the touch. Phase 2 (after WAIT_S): cancel the rest,
    then send the unfilled remainder at market if it is still above the minimum."""
    recs, resting = {}, {}
    for c, dq in deltas.items():
        book = hl.info({"type": "l2Book", "coin": c}, hl.TESTNET)["levels"]
        if not book[0] or not book[1]:
            recs[c] = {"coin": c, "size": dq, "note": "empty book"}; continue
        px = float(book[0][0]["px"]) if dq > 0 else float(book[1][0]["px"])
        px = round_px(px, szd[c]); sz = round(abs(dq), szd[c])
        rec = {"coin": c, "size": round(dq, szd[c]), "limit_px": px, "maker_filled": 0.0, "taker_filled": 0.0}
        if sz * px < MIN_ORDER_USD:
            rec["note"] = "below minimum"; recs[c] = rec; continue
        try:
            st = _status(ex.order(c, dq > 0, sz, px, {"limit": {"tif": "Alo"}}, reduce_only=reduce_only.get(c, False)))
            if "resting" in st:
                resting[c] = st["resting"]["oid"]
            elif "filled" in st:
                rec["maker_filled"] = float(st["filled"]["totalSz"])
            else:
                rec["note"] = "post-only rejected: " + str(st.get("error", st))[:80]
        except Exception as e:
            rec["note"] = f"limit error: {e}"[:120]
        recs[c] = rec
    if resting:
        time.sleep(WAIT_S)
    open_now = {o["oid"]: o for o in hl.info({"type": "openOrders", "user": acct}, hl.TESTNET)}
    for c, oid in resting.items():
        rec = recs[c]; left = 0.0
        if oid in open_now:
            left = float(open_now[oid]["sz"])
            try:
                ex.cancel(c, oid)
            except Exception as e:
                log["notes"].append(f"cancel {c}: {e}")
        rec["maker_filled"] = round(abs(rec["size"]) - left, szd[c])
        if left and left * float(open_now[oid]["limitPx"]) >= MIN_ORDER_USD:
            try:
                st = _status(ex.market_open(c, rec["size"] > 0, round(left, szd[c]), None, 0.01)) \
                    if not reduce_only.get(c) else _status(ex.market_close(c, round(left, szd[c]), None, 0.01))
                if "filled" in st:
                    rec["taker_filled"] = float(st["filled"]["totalSz"])
                else:
                    rec["note"] = "market remainder failed: " + str(st.get("error", st))[:80]
            except Exception as e:
                rec["note"] = f"market error: {e}"[:120]
        elif left:
            rec["note"] = "remainder below minimum, left for next cycle"
    # post-only rejections (price moved through the touch) go straight to market
    for c, rec in recs.items():
        if rec.get("note", "").startswith("post-only rejected"):
            sz = round(abs(rec["size"]), szd[c])
            try:
                st = _status(ex.market_open(c, rec["size"] > 0, sz, None, 0.01)) \
                    if not reduce_only.get(c) else _status(ex.market_close(c, sz, None, 0.01))
                if "filled" in st:
                    rec["taker_filled"] = float(st["filled"]["totalSz"])
            except Exception as e:
                rec["note"] += f"; market error: {e}"[:80]
    return list(recs.values())


def measure(acct, t0_ms, mids_at_decision, modelled_rate):
    """Cost of this cycle's fills versus the decision-time mid.

    Returns {"fills", "notional", "maker_notional", "cost_usd", "modelled_usd"} where
    cost_usd = fee + adverse price versus mid, and modelled_usd is what the paper
    book's taker model would have charged for the same notional."""
    fills = hl.info({"type": "userFillsByTime", "user": acct, "startTime": t0_ms}, hl.TESTNET)
    out = {"fills": 0, "notional": 0.0, "maker_notional": 0.0, "cost_usd": 0.0, "modelled_usd": 0.0}
    for f in fills:
        c = f["coin"]; mid = mids_at_decision.get(c)
        if not mid:
            continue
        px, sz = float(f["px"]), float(f["sz"]); n = px * sz
        sign = 1 if f["side"] == "B" else -1
        out["fills"] += 1; out["notional"] += n
        if not f.get("crossed", True):
            out["maker_notional"] += n
        out["cost_usd"] += float(f.get("fee", 0)) + sign * (px - mid) * sz
        out["modelled_usd"] += n * modelled_rate(c)
    return out


def update_stats(st, m):
    e = st.setdefault("exec", {"fills": 0, "notional": 0.0, "maker_notional": 0.0, "cost_usd": 0.0, "modelled_usd": 0.0})
    for k in e:
        e[k] += m[k]
    return e


def paper_cost_ratio(st):
    """Multiplier for the paper book's cost model, from measured testnet executions."""
    e = st.get("exec")
    if not e or e["fills"] < MIN_FILLS_FOR_PAPER or e["modelled_usd"] <= 0:
        return 1.0
    return min(RATIO_CAP, max(RATIO_FLOOR, e["cost_usd"] / e["modelled_usd"]))
