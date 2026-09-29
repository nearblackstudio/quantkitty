"""QuantKitty research cycle (every 4 hours).

Takes the next hypothesis from research/queue.json, backtests it through the
same model the desk trades, and applies the promotion gates from the playbook:

  1. deflated Sharpe probability > 0.90, counting every configuration ever tested
  2. out-of-sample Sharpe > 0.5 and at least half the in-sample Sharpe
  3. neighbouring parameters (each numeric parameter x0.75 and x1.25) all Sharpe > 0
  4. correlation with every live sleeve below 0.6
  5. adding it at risk parity raises the book's Sharpe
  6. passing on two separate cycles at least 24 hours apart

A hypothesis that clears all six is added to config.json as a probation sleeve
(half risk). Probation sleeves become live after 30 days if their last 90 days
are profitable; any sleeve whose last 90 days lose money after 90 days is retired.

Usage: python -m quantkitty.research --state research_state.json --out out/
"""
import argparse, copy, json, math, os, time
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from . import hl, model as M

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPLIT_DAYS = 365           # out-of-sample = the most recent year
BASE_TRIALS = 63           # configurations tested before this system existed
FUNDING_PER_BAR = 5e-5     # ~11% a year paid by longs; research approximation


def load_panel(n=60):
    uni = sorted(hl.universe(), key=lambda u: -u["vlm24h"])[:n]
    now_ms = int(time.time() * 1000); data = {}
    for u in uni:
        try:
            data[u["coin"]] = hl.candles(u["coin"], 5000, now_ms); time.sleep(0.2)
        except Exception:
            pass
    P = M.Panel.from_candles(data)
    F = pd.DataFrame(FUNDING_PER_BAR, index=P.C.index, columns=P.C.columns)
    return M.Panel(P.C, P.V, F)


def neighbours(spec):
    out = []
    for k, v in spec.get("params", {}).items():
        for f in (0.75, 1.25):
            s = copy.deepcopy(spec)
            if isinstance(v, list):
                s["params"][k] = [max(2, int(round(x * f))) for x in v]
            elif isinstance(v, int) and k not in ("tranches",):
                s["params"][k] = max(2, int(round(v * f)))
            elif isinstance(v, float):
                s["params"][k] = v * f
            else:
                continue
            out.append(s)
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--state"); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg_path = os.path.join(ROOT, "config.json"); q_path = os.path.join(ROOT, "research", "queue.json")
    cfg = json.load(open(cfg_path)); queue = json.load(open(q_path))
    st = {}
    if a.state and os.path.exists(a.state):
        d = json.load(open(a.state)); st = json.loads(d["state"]) if isinstance(d.get("state"), str) else d
    st.setdefault("trials", 0); st.setdefault("passes", {}); st.setdefault("tested", {})
    now = datetime.now(timezone.utc); os.makedirs(os.path.join(a.out, "ledger"), exist_ok=True)
    P = load_panel()
    split = P.C.index[-1] - pd.Timedelta(days=SPLIT_DAYS)
    live = [s for s in cfg["sleeves"] if s.get("status") in ("live", "probation")]
    _, book, X, _ = M.portfolio(P, live, cfg["desk"]["vol_target"])
    book = book[180:]; base_sr = M.stats(book)["Sharpe"]
    msgs, config_changed = [], False

    # ---- lifecycle of existing sleeves (probation -> live, retire losers)
    for s in cfg["sleeves"]:
        if s.get("status") not in ("live", "probation"):
            continue
        age = (now.date() - datetime.fromisoformat(s["since"]).date()).days
        recent = M.stats(X[s["name"]].iloc[-540:])["Sharpe"] if s["name"] in X else 0
        if s["status"] == "probation" and age >= 30 and recent > 0:
            s["status"] = "live"; config_changed = True; msgs.append(f"{s['name']} promoted from probation to live.")
        elif age >= 90 and recent < 0 and len(live) > 1:
            s["status"] = "retired"; s["retired"] = now.date().isoformat(); config_changed = True
            msgs.append(f"{s['name']} retired: last 90 days Sharpe {recent:.2f}.")

    # ---- pick the next hypothesis: an untested one, or a first-time pass due for confirmation
    due = [h for h in queue if h["id"] in st["passes"] and h["id"] not in [s.get("hypothesis") for s in cfg["sleeves"]]
           and now - datetime.fromisoformat(st["passes"][h["id"]]) >= timedelta(hours=24)]
    fresh = [h for h in queue if h["id"] not in st["tested"]]
    h = (due or fresh or [None])[0]
    result = {"ts": now.isoformat(timespec="seconds"), "base_book_sharpe": round(base_sr, 2)}
    if h is None:
        result.update({"verdict": "idle", "message": "Queue empty: no hypothesis to test this cycle."})
    else:
        spec = {"name": h["id"], "family": h["family"], "params": h.get("params", {})}
        W = M.sleeve(P, spec); p = M.run(P, W).pnl[360:]
        full, ins, oos = M.stats(p), M.stats(p[p.index < split]), M.stats(p[p.index >= split])
        nb = [M.stats(M.run(P, M.sleeve(P, n)).pnl[360:])["Sharpe"] for n in neighbours(spec)]
        st["trials"] += 1 + len(nb)
        n_trials = BASE_TRIALS + st["trials"]
        dsr = M.psr(p, M.dsr_threshold(n_trials))
        corr = {k: float(pd.concat([p, X[k]], axis=1).dropna().corr().iloc[0, 1]) for k in X}
        _, with_book, _, _ = M.portfolio(P, live + [dict(spec, status="live")], cfg["desk"]["vol_target"])
        marginal = M.stats(with_book[180:])["Sharpe"] - base_sr
        gates = {"deflated_sharpe": dsr > 0.90,
                 "out_of_sample": oos["Sharpe"] > 0.5 and oos["Sharpe"] >= 0.5 * ins["Sharpe"],
                 "neighbours": bool(nb) and min(nb) > 0 or not nb and full["Sharpe"] > 0,
                 "diversifies": max(corr.values(), default=0) < 0.6,
                 "improves_book": marginal > 0}
        passed = all(gates.values())
        st["tested"][h["id"]] = now.isoformat(timespec="seconds")
        verdict = "fail"
        if passed and h["id"] in st["passes"]:
            verdict = "promoted"
            cfg["sleeves"].append({"name": h["id"], "family": h["family"], "params": h.get("params", {}),
                                   "status": "probation", "since": now.date().isoformat(), "hypothesis": h["id"]})
            config_changed = True; msgs.append(f"{h['id']} passed twice and joins the book on probation at half risk.")
        elif passed:
            verdict = "first_pass"; st["passes"][h["id"]] = now.isoformat(timespec="seconds")
        elif h["id"] in st["passes"]:
            st["passes"].pop(h["id"]); verdict = "failed_confirmation"
        result.update({"id": h["id"], "family": h["family"], "params": h.get("params", {}), "rationale": h.get("rationale", ""),
                       "sharpe": round(full["Sharpe"], 2), "is_sharpe": round(ins["Sharpe"], 2), "oos_sharpe": round(oos["Sharpe"], 2),
                       "cagr": round(full["CAGR"], 3), "max_dd": round(full["MaxDD"], 3),
                       "neighbour_sharpes": [round(x, 2) for x in nb], "dsr": round(dsr, 3), "trials": n_trials,
                       "corr": {k: round(v, 2) for k, v in corr.items()}, "marginal_book_sharpe": round(marginal, 2),
                       "gates": gates, "verdict": verdict})
        failed = [k for k, v in gates.items() if not v]
        result["message"] = (f"{h['id']} ({h['family']}): Sharpe {full['Sharpe']:.2f} (in {ins['Sharpe']:.2f}, out {oos['Sharpe']:.2f}), "
                             f"deflated {dsr:.2f}, book {marginal:+.2f}. " +
                             ("Passed all gates. " if passed else "Failed: " + ", ".join(failed) + ". ") + " ".join(msgs))
    if not h and msgs:
        result["message"] += " " + " ".join(msgs)
    result["config_changed"] = config_changed
    if config_changed:
        json.dump(cfg, open(cfg_path, "w"), indent=2)
    sid = now.strftime("%Y%m%dT%H%MZ")
    json.dump(result, open(os.path.join(a.out, "ledger", f"research_{sid}.json"), "w"), default=float)
    json.dump({"state": json.dumps(st), "updated": now.isoformat(timespec="seconds")},
              open(os.path.join(a.out, "ledger", "system_research_state.json"), "w"))
    print(result["message"])


if __name__ == "__main__":
    main()
