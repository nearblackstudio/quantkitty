"""QuantKitty model: point-in-time universe, strategy library, sleeve sizing,
portfolio construction and the backtester. Used identically by the live desk
and by research, so a strategy is traded exactly as it was tested."""
import math
import numpy as np
import pandas as pd

BPY = 6 * 365                 # 4h bars per year
TAKER = 0.00045


class Panel:
    """Aligned 4h panels: close C, notional volume V, funding F (per 4h bar)."""

    def __init__(self, C, V, F=None, top_n=40, min_adv=3e6, min_age=540):
        self.C = C.sort_index()
        self.V = V.reindex_like(self.C)
        self.F = (F if F is not None else pd.DataFrame(0.0, index=C.index, columns=C.columns)).reindex_like(self.C).fillna(0.0)
        self.R = self.C.pct_change(fill_method=None).fillna(0.0).clip(-0.9, 3.0)
        self.ADV = self.V.rolling(180, min_periods=90).mean() * 6
        age = self.C.notna().cumsum()
        rank = self.ADV.where(age >= min_age).rank(axis=1, ascending=False)
        self.ELIG = (rank <= top_n) & (self.ADV >= min_adv)
        slip = pd.DataFrame(0.0010, index=C.index, columns=C.columns)
        slip[self.ADV >= 50e6] = 0.0004
        slip[self.ADV >= 500e6] = 0.0002
        self.COST = slip + TAKER
        self._vol = {}

    def vol(self, bars=180):
        if bars not in self._vol:
            self._vol[bars] = self.R.rolling(bars, min_periods=60).std() * math.sqrt(BPY)
        return self._vol[bars]

    @staticmethod
    def from_candles(data):
        """data: {coin: [candle dicts with t, c, v]} -> Panel"""
        cl, vo = {}, {}
        for coin, rows in data.items():
            if len(rows) < 60:
                continue
            idx = pd.to_datetime([r["t"] for r in rows], unit="ms")
            c = pd.Series([float(r["c"]) for r in rows], index=idx)
            cl[coin] = c; vo[coin] = c * pd.Series([float(r["v"]) for r in rows], index=idx)
        return Panel(pd.DataFrame(cl), pd.DataFrame(vo))


# ---------------------------------------------------------------- execution model

def run(P, W):
    """Weights decided at bar t close, held over bar t+1. Returns per-bar pnl frame."""
    W = W.reindex_like(P.C).fillna(0.0).where(P.ELIG | (W.shift(1).fillna(0) != 0), 0.0)
    held = W.shift(1).fillna(0.0)
    price = (held * P.R).sum(axis=1)
    fund = -(held * P.F).sum(axis=1)
    turn = (W.diff().abs().fillna(W.abs()) * P.COST).sum(axis=1).shift(1).fillna(0)
    return pd.DataFrame({"pnl": price + fund - turn, "price": price, "funding": fund, "cost": -turn,
                         "gross": held.abs().sum(axis=1)})


def scale_to_vol(P, W, target=0.15, lookback=360):
    raw = (W.shift(1).fillna(0) * P.R).sum(axis=1)
    rv = raw.rolling(lookback, min_periods=120).std() * math.sqrt(BPY)
    return W.mul((target / rv).clip(upper=4.0).shift(1).fillna(0.0), axis=0)


def throttle(W, every=6, band=0.25, phase=0):
    """Trade once a day, and only when a target moves > band of itself or flips."""
    Wv = W.values; out = np.zeros_like(Wv); cur = np.zeros(Wv.shape[1])
    for i in range(len(Wv)):
        if (i - phase) % every == 0:
            t = np.nan_to_num(Wv[i])
            chg = (np.abs(t - cur) > band * np.maximum(np.abs(t), 1e-12)) | (np.sign(t) != np.sign(cur))
            cur = np.where(chg, t, cur)
        out[i] = cur
    return pd.DataFrame(out, index=W.index, columns=W.columns)


def stats(p):
    p = p.dropna()
    if len(p) < 50 or p.std() == 0:
        return {"Sharpe": 0.0, "CAGR": 0.0, "Vol": 0.0, "MaxDD": 0.0}
    yrs = len(p) / BPY; eq = (1 + p).cumprod()
    return {"Sharpe": float(p.mean() / p.std() * math.sqrt(BPY)), "CAGR": float(eq.iloc[-1] ** (1 / yrs) - 1),
            "Vol": float(p.std() * math.sqrt(BPY)), "MaxDD": float((eq / eq.cummax() - 1).min())}


def psr(p, sr_star=0.0):
    p = p.dropna(); n = len(p); s = p.mean() / p.std()
    g3, g4 = p.skew(), p.kurt() + 3
    z = (s - sr_star) * math.sqrt(n - 1) / math.sqrt(max(1e-9, 1 - g3 * s + (g4 - 1) / 4 * s * s))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def dsr_threshold(n_trials, n_bars=None):
    """Expected max per-bar Sharpe among n_trials unskilled strategies (annual SR sd 0.5)."""
    from statistics import NormalDist
    e = 0.5772156649; nd = NormalDist(); sd = 0.5 / math.sqrt(BPY)
    n = max(2, n_trials)
    return sd * ((1 - e) * nd.inv_cdf(1 - 1 / n) + e * nd.inv_cdf(1 - 1 / (n * math.e)))


# ---------------------------------------------------------------- strategy library
# Every family takes the panel and keyword params and returns raw target weights.
# Research may only combine these; it never writes new code.

def tsmom(P, lookbacks=(42, 84, 180, 360), coin_vol=0.40):
    """Time-series momentum ensemble (Moskowitz, Ooi, Pedersen 2012)."""
    sig = sum(np.sign(P.C / P.C.shift(L) - 1) for L in lookbacks) / len(lookbacks)
    n = P.ELIG.sum(axis=1).replace(0, np.nan)
    W = sig * (coin_vol / P.vol()).clip(upper=3.0)
    return W.where(P.ELIG, 0.0).div(n, axis=0).fillna(0.0)


def _xs(P, score, q=0.2, hold=42, offset=0):
    s = score.where(P.ELIG); rk = s.rank(axis=1, pct=True); iv = (1 / P.vol()).where(P.ELIG)
    lo = (rk >= 1 - q) * iv; sh = (rk <= q) * iv
    W = (lo.div(lo.sum(axis=1), axis=0) - sh.div(sh.sum(axis=1), axis=0)).fillna(0.0) * 0.5
    reb = pd.Series((np.arange(len(W)) - offset) % hold == 0, index=W.index)
    return W.where(reb, np.nan).ffill().fillna(0.0)


def xs_mom(P, lookbacks=(48, 72, 96), skip=6, hold=42, tranches=3, q=0.2):
    """Cross-sectional momentum (Liu, Tsyvinski, Wu 2022), staggered tranches."""
    Ws = [_xs(P, P.C.shift(skip) / P.C.shift(lb) - 1, q, hold, o)
          for lb in lookbacks for o in range(0, hold, hold // tranches)]
    return sum(Ws) / len(Ws)


def donchian(P, n=120, coin_vol=0.40):
    """Channel breakout: long above the n-bar high, short below the n-bar low, exit at the midpoint."""
    hi = P.C.rolling(n).max().shift(1).values; lo = P.C.rolling(n).min().shift(1).values
    c = P.C.values; mid = (hi + lo) / 2
    s = np.zeros_like(c); cur = np.zeros(c.shape[1])
    for i in range(len(c)):
        with np.errstate(invalid="ignore"):
            cur = np.where(c[i] > hi[i], 1, np.where(c[i] < lo[i], -1, cur))
            cur = np.where(((cur == 1) & (c[i] < mid[i])) | ((cur == -1) & (c[i] > mid[i])), 0, cur)
        cur = np.nan_to_num(cur); s[i] = cur
    s = pd.DataFrame(s, index=P.C.index, columns=P.C.columns)
    n_el = P.ELIG.sum(axis=1).replace(0, np.nan)
    return (s * (coin_vol / P.vol()).clip(upper=3)).where(P.ELIG, 0).div(n_el, axis=0).fillna(0)


def low_vol(P, lookback=180, hold=42):
    """Low-volatility anomaly: long the calmest fifth, short the most volatile fifth."""
    return _xs(P, -P.vol(lookback), 0.2, hold)


def beta_hedged_mom(P, lookbacks=(48, 72, 96), hold=42):
    """Cross-sectional momentum on BTC-beta-adjusted returns."""
    if "BTC" not in P.C:
        return xs_mom(P, lookbacks, hold=hold)
    rb = P.R["BTC"]; beta = P.R.rolling(360, min_periods=120).cov(rb).div(rb.rolling(360, min_periods=120).var(), axis=0)
    resid = (P.R - beta.mul(rb, axis=0)).fillna(0)
    Ws = [_xs(P, resid.rolling(lb).sum().shift(6), 0.2, hold, o) for lb in lookbacks for o in (0, 14, 28)]
    return sum(Ws) / len(Ws)


def funding_timing(P, lb=18, hold=6):
    """Short-term: fade coins whose funding is extreme versus their own history."""
    z = (P.F.rolling(lb, min_periods=6).mean() - P.F.rolling(540, min_periods=120).mean()) / P.F.rolling(540, min_periods=120).std()
    return _xs(P, -z, 0.2, hold)


LIBRARY = {"tsmom": tsmom, "xs_mom": xs_mom, "donchian": donchian, "low_vol": low_vol,
           "beta_hedged_mom": beta_hedged_mom, "funding_timing": funding_timing}


def sleeve(P, spec, target=0.15):
    """spec: {'family': name, 'params': {...}} -> vol-scaled, throttled weights."""
    W = LIBRARY[spec["family"]](P, **{k: tuple(v) if isinstance(v, list) else v for k, v in spec.get("params", {}).items()})
    return throttle(scale_to_vol(P, W, target), 6, 0.25)


def portfolio(P, specs, target=0.25, lev_cap=3.0):
    """Risk parity across sleeves, then scale the book to the volatility target.
    Returns (final weight matrix, per-bar portfolio pnl, sleeve pnls, sleeve weights)."""
    Ws = {s["name"]: sleeve(P, s) for s in specs}
    X = pd.DataFrame({k: run(P, W).pnl for k, W in Ws.items()}).fillna(0)
    iv = 1 / X.rolling(540, min_periods=180).std().shift(1)
    wts = iv.div(iv.sum(axis=1), axis=0).fillna(1 / len(Ws))
    raw = (X * wts).sum(axis=1)
    rv = raw.rolling(540, min_periods=180).std().shift(1) * math.sqrt(BPY)
    lev = (target / rv).clip(upper=lev_cap).fillna(0)
    scale = {s["name"]: (0.5 if s.get("status") == "probation" else 1.0) for s in specs}
    final = sum(Ws[k].mul(wts[k] * lev * scale[k], axis=0) for k in Ws)
    return final, raw * lev, X, wts
