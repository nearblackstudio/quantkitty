"""QuantKitty desk run (every 4 hours).

Roles, in order: market analyst (universe + data), strategist (model targets),
risk manager (limits and kill switches), trader (virtual paper book at mainnet
prices, mirrored onto the Hyperliquid testnet account).

Usage:
  python -m quantkitty.desk --state state.json --flags flags.json --out out/ [--execute]
Secrets from env: HL_ACCOUNT (main address), HL_AGENT_KEY (testnet API wallet key).
Writes one JSON file per ledger document into out/ledger/.
"""
import argparse, json, math, os, time
from datetime import datetime, timezone

import pandas as pd

from . import hl, model as M, execution as EX

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path, default):
    if path and os.path.exists(path):
        d = json.load(open(path))
        if isinstance(d.get("state"), str):          # ledger doc wraps state as a JSON string
            return json.loads(d["state"] or "{}")
        return d
    return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state"); ap.add_argument("--flags"); ap.add_argument("--out", required=True)
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    D, RK = cfg["desk"], cfg["risk"]
    st = load(a.state, {}); flags = load(a.flags, {})
    now = datetime.now(timezone.utc); now_ms = int(now.timestamp() * 1000)
    log = {"notes": [], "errors": [], "trades": [], "orders": []}
    os.makedirs(os.path.join(a.out, "ledger"), exist_ok=True)

    # ---------------- market analyst
    uni = hl.universe()
    held = set(st.get("paper", {}).get("positions", {}))
    cands = [u["coin"] for u in uni if u["vlm24h"] >= 1e6 or u["coin"] in held]
    data = {}
    for c in cands:
        try:
            data[c] = hl.candles(c, 2000, now_ms); time.sleep(0.15)
        except Exception as e:
            log["notes"].append(f"no candles for {c}: {e}")
    P = M.Panel.from_candles(data)
    uc = D["universe"]
    P = M.Panel(P.C, P.V, None, uc["top_n"], uc["min_adv_usd"], uc["min_age_bars"])
    last_bar = P.C.index[-1]
    age_h = (now - last_bar.tz_localize("UTC")).total_seconds() / 3600 - 4
    eligible = sorted(P.ELIG.iloc[-1][P.ELIG.iloc[-1]].index)
    mids = hl.mids()

    # ---------------- strategist
    live = [s for s in cfg["sleeves"] if s.get("status") in ("live", "probation")]
    W, pnl, X, wts = M.portfolio(P, live, D["vol_target"], D["leverage_cap_book"])   # probation sleeves at half risk
    target = W.iloc[-1].copy()
    target = target[target.abs() > 1e-6]

    # ---------------- risk manager
    pb = st.setdefault("paper", {"cash": float(D["paper_start_equity"]), "positions": {}, "peak": float(D["paper_start_equity"])})
    # accrue funding since the last run on open paper positions, then mark to market
    last_ms = st.get("last_run_ms", now_ms)
    for c, p in pb["positions"].items():
        if last_ms < now_ms:
            try:
                f = hl.funding_sum(c, last_ms, now_ms)
            except Exception:
                f = 0.0
            cost = p["qty"] * mids.get(c, p["entry"]) * f          # longs pay positive funding
            pb["cash"] -= cost; p["funding"] = p.get("funding", 0.0) + cost
    def equity():
        return pb["cash"] + sum(p["qty"] * (mids.get(c, p["entry"]) - p["entry"]) for c, p in pb["positions"].items())
    eq = equity()
    pb["peak"] = max(pb["peak"], eq)
    dk, wk = now.strftime("%Y-%m-%d"), now.strftime("%G-W%V")
    if st.get("day_key") != dk: st["day_key"], st["day_start"] = dk, eq
    if st.get("week_key") != wk: st["week_key"], st["week_start"] = wk, eq
    halt = str(flags.get("hl_halt", "false")).lower() == "true"
    block_new = halt; scale = 1.0; set_halt = False
    if eq < RK["hard_drawdown"] * pb["peak"]:
        set_halt = halt = True; target = target * 0
        log["notes"].append(f"Hard drawdown: equity {eq:,.0f} below {RK['hard_drawdown']:.0%} of peak {pb['peak']:,.0f}. Flattening and halting.")
    if eq < RK["soft_drawdown"] * pb["peak"]:
        st["soft"] = True
    elif eq > RK["soft_recover"] * pb["peak"]:
        st["soft"] = False
    if st.get("soft"):
        scale = 0.5; log["notes"].append("Soft drawdown mode: sizes halved.")
    if eq / st["day_start"] - 1 <= RK["daily_loss_limit"]:
        block_new = True; log["notes"].append("Daily loss limit hit: no new risk today.")
    if eq / st["week_start"] - 1 <= RK["weekly_loss_limit"]:
        block_new = True; log["notes"].append("Weekly loss limit hit: no new risk this week.")
    if age_h > RK["max_data_age_hours"]:
        block_new = True; log["errors"].append(f"Stale data: last closed bar {last_bar} is {age_h:.1f}h old.")
    target = (target * scale).clip(-D["coin_cap"], D["coin_cap"])
    gross = target.abs().sum()
    if gross > D["gross_cap"]:
        target *= D["gross_cap"] / gross
        log["notes"].append(f"Gross exposure capped from {gross:.2f}x to {D['gross_cap']}x.")

    # ---------------- trader: paper book at mainnet prices
    taker_rate = lambda c: M.TAKER + (0.0002 if P.ADV[c].iloc[-1] >= 500e6 else 0.0004 if P.ADV[c].iloc[-1] >= 50e6 else 0.0010) \
        if c in P.ADV else M.TAKER + 0.0010
    cost_ratio = EX.paper_cost_ratio(st)              # measured testnet execution vs the taker model (1.0 until 30 fills)
    cost_rate = lambda c: taker_rate(c) * cost_ratio
    coins = sorted(set(target.index) | set(pb["positions"]))
    sgn = lambda x: (x > 0) - (x < 0)
    for c in coins:
        px = mids.get(c)
        if not px:
            continue
        pos = pb["positions"].get(c, {"qty": 0.0, "entry": px, "t": now_ms, "fees": 0.0, "funding": 0.0})
        q0, qt = pos["qty"], float(target.get(c, 0.0)) * eq / px
        if block_new and (abs(qt) > abs(q0) or sgn(qt) != sgn(q0)):     # risk may shrink, never grow
            qt = q0 if sgn(qt) == sgn(q0) else 0.0
        dq = qt - q0
        if abs(dq) * px < max(1.0, 0.002 * eq):                          # ignore dust rebalances
            continue
        fee = abs(dq) * px * cost_rate(c); pb["cash"] -= fee
        if q0 and (qt == 0 or sgn(qt) != sgn(q0)):
            closed = q0
        elif q0 and abs(qt) < abs(q0):
            closed = q0 - qt
        else:
            closed = 0.0
        fee_close = fee * abs(closed) / abs(dq) if closed else 0.0
        if closed:
            frac = abs(closed / q0); pnl_ = closed * (px - pos["entry"]); pb["cash"] += pnl_
            tr = {"coin": c, "side": sgn(closed), "size": abs(closed), "entry_px": pos["entry"], "exit_px": px,
                  "entry_t": pos["t"], "exit_t": now_ms, "gross": round(pnl_, 2),
                  "fees": round(pos["fees"] * frac + fee_close, 2), "funding": round(pos["funding"] * frac, 2)}
            tr["net"] = round(tr["gross"] - tr["fees"] - tr["funding"], 2); log["trades"].append(tr)
            pos["fees"] *= 1 - frac; pos["funding"] *= 1 - frac
        remaining = q0 - closed
        opened = qt - remaining
        if opened:
            pos["entry"] = (pos["entry"] * remaining + px * opened) / qt if remaining else px
            if not remaining:
                pos["t"] = now_ms
        pos["fees"] += fee - fee_close; pos["qty"] = qt
        log["orders"].append({"coin": c, "from": round(q0, 6), "to": round(qt, 6), "px": px})
        if abs(qt) * px < 0.01:
            pb["positions"].pop(c, None)
        else:
            pb["positions"][c] = pos
    eq = equity(); pb["peak"] = max(pb["peak"], eq)

    # ---------------- trader: testnet mirror
    tn = {"equity": None, "positions": {}, "orders": []}
    acct = os.environ.get("HL_ACCOUNT")
    if acct:
        tn_eq, tn_pos = hl.account(acct)
        tn["equity"], tn["positions"] = tn_eq, tn_pos
        tmids = hl.mids(hl.TESTNET)
        meta = hl.info({"type": "meta"}, hl.TESTNET); szd = {u["name"]: u["szDecimals"] for u in meta["universe"]}
        want = {}
        ok = [c for c in target.index if c in tmids and c in szd and abs(tmids[c] / mids[c] - 1) <= RK["max_venue_divergence"]]
        for c in ok:                                   # full-size mirror where the account is big enough
            n = float(target[c]) * tn_eq
            if abs(n) >= D["testnet_min_order_usd"]:
                want[c] = n
        if not want:                                   # small account: mirror the largest positions at minimum size
            for c in sorted(ok, key=lambda c: -abs(target[c]))[:D.get("testnet_mirror_top", 4)]:
                want[c] = math.copysign(D["testnet_min_order_usd"], float(target[c]))
        ex = None
        if a.execute and os.environ.get("HL_AGENT_KEY"):
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            ex = Exchange(Account.from_key(os.environ["HL_AGENT_KEY"]), hl.TESTNET, account_address=acct)
        deltas, reduce_only = {}, {}
        for c in sorted(set(want) | set(tn_pos)):
            if c not in tmids or c not in szd:
                continue
            cur_q = tn_pos.get(c, {}).get("szi", 0.0); cur_n = cur_q * tmids[c]
            w_n = want.get(c, 0.0)
            flip = cur_n and w_n and (cur_n > 0) != (w_n > 0)
            if not flip and abs(w_n - cur_n) < max(D["testnet_min_order_usd"], 0.3 * abs(w_n)):
                continue
            deltas[c] = w_n / tmids[c] - cur_q
            reduce_only[c] = (w_n == 0)
            tn["orders"].append({"coin": c, "from_usd": round(cur_n, 2), "to_usd": round(w_n, 2)})
        if ex and deltas:
            t0 = int(time.time() * 1000) - 1000
            recs = EX.rebalance(ex, acct, deltas, szd, reduce_only, log)
            by = {r["coin"]: r for r in recs}
            for o in tn["orders"]:
                o.update({k: v for k, v in by.get(o["coin"], {}).items() if k != "coin"})
            time.sleep(3)
            m = EX.measure(acct, t0, tmids, taker_rate)
            tn["execution"] = {k: round(v, 4) for k, v in m.items()}
            e = EX.update_stats(st, m)
            tn["execution_total"] = {k: round(v, 4) for k, v in e.items()}
            tn["equity"], tn["positions"] = hl.account(acct)

    # ---------------- ledger records
    st["last_run_ms"] = now_ms
    sid = now.strftime("%Y%m%dT%H%MZ")
    L = os.path.join(a.out, "ledger")
    status = "error" if log["errors"] else ("traded" if log["orders"] else "held")
    top = target.reindex(target.abs().sort_values(ascending=False).index)
    msg = (f"Paper equity {eq:,.0f} (peak {pb['peak']:,.0f}); {len(pb['positions'])} positions, gross {target.abs().sum():.2f}x; "
           f"{len(log['orders'])} paper rebalances, {len(log['trades'])} closed; universe {len(eligible)} coins; "
           f"testnet {tn['equity'] if tn['equity'] is None else round(tn['equity'], 2)} with {len(tn['orders'])} orders.")
    if tn.get("execution", {}).get("notional"):
        m = tn["execution"]; tot = tn["execution_total"]
        msg += (f" Execution: {m['maker_notional'] / m['notional']:.0%} filled as maker, cost {1e4 * m['cost_usd'] / m['notional']:.1f} bp"
                f" vs {1e4 * m['modelled_usd'] / m['notional']:.1f} bp modelled; {tot['fills']:.0f} fills measured so far,"
                f" paper cost multiplier {cost_ratio:.2f}.")
    if log["notes"]: msg += " " + " ".join(log["notes"][:4])
    if log["errors"]: msg += " Errors: " + " | ".join(log["errors"][:4])
    json.dump({"ts": now.isoformat(timespec="seconds"), "status": status, "message": msg, "network": "testnet"},
              open(f"{L}/run_{sid}.json", "w"))
    json.dump({"ts": now.isoformat(timespec="seconds"), "network": "testnet", "equity": round(eq, 2), "peak": round(pb["peak"], 2),
               "cash": round(pb["cash"], 2), "halt": bool(halt), "errors": len(log["errors"]),
               "positions": {c: {"szi": p["qty"], "entry": p["entry"], "upnl": round(p["qty"] * (mids.get(c, p["entry"]) - p["entry"]), 2)}
                             for c, p in pb["positions"].items()},
               "targets": {c: round(float(w), 4) for c, w in top.head(40).items()},
               "sleeve_weights": {k: round(float(wts[k].iloc[-1]), 3) for k in wts},
               "universe": eligible, "paper_cost_multiplier": round(cost_ratio, 3),
               "testnet": {"equity": tn["equity"], "positions": tn["positions"], "orders": tn["orders"],
                           "execution": tn.get("execution"), "execution_total": tn.get("execution_total")}},
              open(f"{L}/snapshot_{sid}.json", "w"), default=float)
    for t in log["trades"]:
        json.dump(t, open(f"{L}/trade_{t['coin']}_{t['exit_t']}.json", "w"))
    json.dump({"state": json.dumps(st, default=float), "updated": now.isoformat(timespec="seconds")}, open(f"{L}/system_state.json", "w"))
    nf = dict(flags); nf["updated"] = now.isoformat(timespec="seconds")
    if set_halt: nf["hl_halt"] = "true"
    json.dump(nf, open(f"{L}/system_flags.json", "w"))
    print(msg)


if __name__ == "__main__":
    main()
