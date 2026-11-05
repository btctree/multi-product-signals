# COPY of the review's live simulator, kept here so it cannot vanish with a temp folder. Source sha256 (CRLF as saved)
# dabc7d25f3c893385bfb63295920db699577446079073b8d9fbc6946a1e7a223. Only change: the body of m1_daily() is gone
# (it read another project through a machine path; no arm uses it). Frozen: see engine/forward/frozen.py.
# Its research switches (placebos, news, stop modes) stay unused: the scoreboard passes only the frozen settings.
"""Live-faithful simulator of the IBKR bot (execution/ib_bot.py as of 2026-10-24) - research only.

What it reproduces (see spec.txt for the code references):
  * two trading runs every weekday, 09:00 UTC and 23:35 UTC; each market decided only when
    its close is final (09:00: US on the previous close, JP on today's close; HK and EU
    deferred to 23:35); orders fill at that market's next open (JP 00:00, HK 01:30,
    EU 07:00-08:00, US 13:30 UTC), so slots freed by Asian/EU exits can be re-used by the
    09:00 run the same day
  * one pool of 15 slots for every market: free = 15 - holdings - working buys; a skipped
    candidate uses no slot
  * the published card list: live BUY cards (and crypto BUY/HOLD cards) ranked by the
    integer momentum score (0-100), ties in the CI download order, top 20 only
  * sizing min(NetLiq/15, HK$20,000) / FX / signal close, whole shares, JP lots of 100,
    HKEX board lots; one lot too big -> skip
  * per-currency cash: HKD is never sold; a USD/JPY/EUR buy that is short converts 1.02 x the
    shortfall from the single largest other non-HKD balance; an HK buy converts 1.03 x;
    sale proceeds stay in their own currency; USD 2 + 2bp per conversion
  * entry = DAY limit at signal close x 1.005 on the venue tick: fills at the open if the open
    is at or below it, at the limit if the day's low reaches it, otherwise not at all
  * exits decided on the close, MKT at the next open: close < SMA200; trailing cut-loss
    (stop0 = max(close-3.5ATR, 0.88 close) from the signal close, hw from the signal close,
    3.5 ATR or 2.0 ATR once close >= entry + 1.5 ATR, never lowered); 60 weekdays counted
    from the run date that placed the buy (holidays count); the fill day's own close is
    checked; stops and prices in ACTUAL traded prices (never rescaled for dividends or
    splits - a split forces a spurious exit, as live)
  * IBKR fixed commissions with minimums, HK stamp duty and levies by year, EU transaction
    taxes (FR/IT/ES), US SEC/FINRA fees; dividends paid as cash net of withholding
  * kill switch: entries blocked when NetLiq < 92% of the peak daily NetLiq; policies
    'strict' (never resets), 'rerisk20' (operator resets the peak after 20 blocked weekdays),
    'off'
Variants: 'E' (current logic), 'D3E1' (E filtered by E1 for US + D3 cards after E),
'D3C6E1' (+ exit on a completed bearish chart pattern)."""
import os, sys, math, pickle, json
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
import numpy as np
import pandas as pd

DATA = None
# PREREG_1028_loss (live loss review): set by the runner, never stored in cfg (results pickle the cfg)
SEC_OF = None     # {ticker: sector or industry label}
EARN_RS = None    # {ticker: sorted int64 array of earnings reaction sessions (day numbers)}
NEWS_EV = None    # PREREG_1028_news: {ticker: sorted int64 array of decision sessions (day numbers) with a news event}
MACRO = None      # PREREG_1028_news Note B1: (US-close day numbers, shock-on booleans)
NEWS_EXIT = None  # PREREG_1028_news Note F: {ticker: sorted int64 day numbers of sessions with an exit event}
# ---- BTC slot (owner request 2026-10-25) ----
_M1 = None
_HKCAL = None
M1_CONV = {"High": 3.0, "Med": 2.5, "Low": 2.25}       # M1's own long exposure (margin % x 5x), in slots
M1_CONV_SHORT = {"High": 1.2, "Med": 1.0, "Low": 0.9}  # M1's own short exposure (margin % x 2x), in slots


def m1_daily():
    """Removed from this copy: no scoreboard arm has a BTC slot (cfg btc=None), so nothing calls it."""
    raise RuntimeError("the forward scoreboard has no BTC slot")


def hk_calendar():
    global _HKCAL
    if _HKCAL is None:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prices_live", "_IDX_HSI.csv")
        _HKCAL = set(dnum(d) for d in pd.read_csv(f, index_col=0, parse_dates=True).index)
    return _HKCAL
# ---- idle-cash parking (owner request 2026-10-25) ----
_IRX = None


def irx_rate(day):
    """13-week T-bill yield (decimal) on or before day number `day`."""
    global _IRX
    if _IRX is None:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rates_dl", "IRX.csv")
        d = pd.read_csv(f, index_col=0, parse_dates=True)["Close"].dropna() / 100.0
        _IRX = (np.array([dnum(x) for x in d.index]), d.values.astype(float))
    i = np.searchsorted(_IRX[0], day, "right") - 1
    return float(_IRX[1][max(i, 0)])


# sweep: fee = annual fee (decimal), wht = withholding on income, cmin/cps/px = commission min (USD) / per share / share price,
# cbp = commission as a fraction of value, hs = half spread, buf = USD left as cash, thr = minimum USD added per sweep
PARK = {
    "ibint": dict(sweep=False),
    "bills": dict(ymin=0.01, lag=2, sweep=True, fee=0.0, wht=0.0, cmin=5.0, cps=0.0, px=1.0, cbp=0.00002, hs=0.0001, buf_slots=3, thr=5000.0, lot=1000.0, roll_days=91),
    "bills_b1": dict(ymin=0.01, lag=2, sweep=True, fee=0.0, wht=0.0, cmin=5.0, cps=0.0, px=1.0, cbp=0.00002, hs=0.0001, buf_slots=1, thr=2000.0, lot=1000.0, roll_days=91),
    "sgov": dict(ymin=0.01, lag=0, sweep=True, fee=0.0012, wht=0.0, cmin=1.0, cps=0.005, px=100.0, cbp=0.0, hs=0.0003, buf_slots=1, thr=1000.0, lot=0.0, roll_days=0),
    "sgov_b2": dict(ymin=0.01, lag=0, sweep=True, fee=0.0012, wht=0.0, cmin=1.0, cps=0.005, px=100.0, cbp=0.0, hs=0.0003, buf_slots=2, thr=2000.0, lot=0.0, roll_days=0),
    "sgov_wht": dict(ymin=0.01, lag=0, sweep=True, fee=0.0009, wht=0.30, cmin=1.0, cps=0.005, px=100.0, cbp=0.0, hs=0.0003, buf_slots=1, thr=1000.0, lot=0.0, roll_days=0),
    "ib01": dict(ymin=0.01, lag=2, sweep=True, fee=0.0007, wht=0.0, cmin=4.0, cps=0.0, px=1.0, cbp=0.0005, hs=0.0003, buf_slots=3, thr=3000.0, lot=0.0, roll_days=0),
    # PREREG_1028_park: the BOT's rule (execution/ib_bot.py park_plan / park_idle_usd), see Sim.park_step_bot.
    # Interest, IBKR credit interest and SGOV costs go to a side USD balance (park_led) that counts in NetLiq
    # and that the park step can park, but that the stock sizing never sees -> identical stock trades.
    "ibint_L": dict(mode="bot", sweep=False),
    "bot3": dict(mode="bot", sweep=True, fee=0.0012, wht=0.0, cmin=1.0, cps=0.005, px=100.5, hs=0.0003, buf_slots=3, thr=1000.0, lag=0),
    "bot2": dict(mode="bot", sweep=True, fee=0.0012, wht=0.0, cmin=1.0, cps=0.005, px=100.5, hs=0.0003, buf_slots=2, thr=1000.0, lag=0),
    "bot1": dict(mode="bot", sweep=True, fee=0.0012, wht=0.0, cmin=1.0, cps=0.005, px=100.5, hs=0.0003, buf_slots=1, thr=1000.0, lag=0),
    "bot0": dict(mode="bot", sweep=True, fee=0.0012, wht=0.0, cmin=1.0, cps=0.005, px=100.5, hs=0.0003, buf_slots=0, thr=1000.0, lag=0),
    "bot0_wht": dict(mode="bot", sweep=True, fee=0.0012, wht=0.30, cmin=1.0, cps=0.005, px=100.5, hs=0.0003, buf_slots=0, thr=1000.0, lag=0),
    "bot0_lag": dict(mode="bot", sweep=True, fee=0.0012, wht=0.0, cmin=1.0, cps=0.005, px=100.5, hs=0.0003, buf_slots=0, thr=1000.0, lag=1),
}
for _b in (3, 2, 1, 0):                # PREREG_1028_park Note A: information-only 0.01% half-spread arms
    PARK[f"bot{_b}_s1"] = dict(PARK[f"bot{_b}"], hs=0.0001)
PARK_LIMIT_BUFFER = 0.005      # ib_bot LIMIT_BUFFER: a parking BUY is sized at px x 1.005
_DFF = None


def dff_rate(day):
    """Fed funds effective (decimal) on or before day number `day` (IBKR's USD benchmark)."""
    global _DFF
    if _DFF is None:
        f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rates_dl", "DFF.csv")
        d = pd.read_csv(f, index_col=0, parse_dates=True).iloc[:, 0].dropna() / 100.0
        _DFF = (np.array([dnum(x) for x in d.index]), d.values.astype(float))
    i = np.searchsorted(_DFF[0], day, "right") - 1
    return float(_DFF[1][max(i, 0)])


def bill_yield(day):
    """13-week T-bill investment (bond-equivalent) yield from the ^IRX discount rate."""
    d = irx_rate(day)
    return 365.0 * d / (360.0 - d * 91.0)
E1 = None


def _load():
    global DATA, E1
    if DATA is None:
        DATA = pickle.load(open(os.path.join(HERE, "live_data.pkl"), "rb"))
        E1 = pickle.load(open(os.path.join(HERE, "live_e1.pkl"), "rb"))
        prep_indexes()


def dnum(x):
    return int(np.datetime64(pd.Timestamp(x).date(), "D").astype(np.int64))


def use_gate_data():
    """PREREG_1026c gate sweep: switch to live_data_g.pkl (adds E_nm / D3_nm = cards without the mom90 > 30% test)
    and live_e1_g.pkl (E1 verdicts for every US E_nm card with mom90 > 20%). Must run before the first Sim."""
    global DATA, E1
    if DATA is not None and DATA.get("_gate"):
        return
    DATA = pickle.load(open(os.path.join(HERE, "live_data_g.pkl"), "rb"))
    E1 = pickle.load(open(os.path.join(HERE, "live_e1_g.pkl"), "rb"))
    DATA["_gate"] = True
    DATA["cards_g"] = {}
    prep_indexes()


def gate_cards(ge, gd):
    """Card table with the momentum gate mom90 > ge on E cards and > gd on D3 cards (live: 0.30 / 0.30)."""
    key = (ge, gd)
    if key not in DATA["cards_g"]:
        cards = {}
        for t, r in DATA["tickers"].items():
            for kind in ("E", "CRY", "D3"):
                if kind == "CRY":
                    m = r.get("CRY")
                elif kind == "E":
                    m = r["E_nm"] & (r["mom"] > ge) if "E_nm" in r else None
                else:
                    m = r["D3_nm"] & (r["mom"] > gd) if "D3_nm" in r else None
                if m is None:
                    continue
                for i in np.flatnonzero(m):
                    cards.setdefault((r["group"], int(r["dn"][i])), []).append((kind, t, int(i)))
        DATA["cards_g"][key] = cards
    return DATA["cards_g"][key]


def prep_indexes():
    T = DATA["tickers"]
    for t, r in T.items():
        r["dn"] = r["dates"].astype(np.int64)
    groups = {}
    for t, r in T.items():
        groups.setdefault(r["group"], set()).update(r["dn"].tolist())
    DATA["gdates"] = {g: np.array(sorted(s), dtype=np.int64) for g, s in groups.items()}
    DATA["gmarket"] = {}
    cards = {}
    for t, r in T.items():
        DATA["gmarket"][r["group"]] = r["m"]
        for kind in ("E", "CRY", "D3"):
            if kind not in r:
                continue
            for i in np.flatnonzero(r[kind]):
                cards.setdefault((r["group"], int(r["dn"][i])), []).append((kind, t, int(i)))
    DATA["cards"] = cards
    fx = {}
    for c, s in DATA["fx"].items():
        v = s.to_numpy(dtype=float)
        if c == "GBP":
            v = v / 100.0          # London prices are quoted in pence: one 'GBP' unit = one penny
        fx[c] = (np.array([dnum(d) for d in s.index], dtype=np.int64), v)
    d, v = fx["EUR"]
    fx["DKK"] = (d, v / 7.46)      # DKK is pegged to EUR (only reached in the research-mode bridge)
    DATA["fxa"] = fx


# ---------------------------------------------------------------- venue rules
def jp_tick(p):
    for lim, t in ((3000, 1), (5000, 5), (30000, 10), (50000, 50), (300000, 100), (500000, 500), (3000000, 1000)):
        if p <= lim:
            return t
    return 5000


def hk_tick(p):
    for lim, t in ((0.25, 0.001), (10.0, 0.005), (20.0, 0.01), (50.0, 0.02), (100.0, 0.05), (200.0, 0.1),
                   (500.0, 0.2), (1000.0, 0.5), (2000.0, 1.0), (5000.0, 2.0), (9995.0, 5.0)):
        if p <= lim:
            return t
    return 5.0


_RTS = ((0.1, 0.0001), (0.2, 0.0001), (0.5, 0.0001), (1.0, 0.0001), (2.0, 0.0002), (5.0, 0.0005), (10.0, 0.001),
        (20.0, 0.002), (50.0, 0.005), (100.0, 0.01), (200.0, 0.02), (500.0, 0.05), (1000.0, 0.1), (2000.0, 0.2),
        (5000.0, 0.5), (10000.0, 1.0), (20000.0, 2.0), (50000.0, 5.0))


def eu_tick(p):
    for up, t in _RTS:
        if p < up - 1e-9:
            return t
    return 10.0


def snap(raw, tick):
    lim = round(raw / tick) * tick
    if tick >= 1:
        return float(int(round(lim)))
    return round(lim, 2 if tick >= 0.01 else 4 if tick >= 0.0001 else 6)


def limit_price(p, m):
    raw = p * 1.005
    if m == "JP":
        return snap(raw, jp_tick(raw))
    if m == "HK":
        return snap(raw, hk_tick(raw))
    if m == "EU":
        return snap(raw, eu_tick(raw))
    if m == "CRYPTO":
        return round(raw, 2)
    return round(raw, 2)


_IDXS = {}


def load_index(n=200):
    """Home index close below its n-day average, per market (scratch prices_live)."""
    if n not in _IDXS:
        _IDX = _IDXS[n] = {}
        base = os.path.join(HERE, "..", "prices_live")
        for m, f in (("US", "_IDX_GSPC.csv"), ("HK", "_IDX_HSI.csv"), ("JP", "_IDX_N225.csv"), ("EU", "_IDX_STOXX50E.csv")):
            s = pd.read_csv(os.path.join(base, f), index_col=0, parse_dates=True)["Close"]
            below = (s < s.rolling(n).mean()).to_numpy()
            _IDX[m] = (np.array([dnum(d) for d in s.index], dtype=np.int64), below)
    return _IDXS[n]


def index_below(idx, m, cutoff):
    if m not in idx:
        return False
    d, b = idx[m]
    i = np.searchsorted(d, cutoff, "right") - 1
    return bool(b[i]) if i >= 0 else False


def hk_stamp_rate(day):
    if day < dnum("2021-08-01"):
        return 0.001
    if day < dnum("2023-11-17"):
        return 0.0013
    return 0.001


NL_ISSUERS = {"AIR.PA", "RACE.MI", "STLAM.MI", "STM.PA", "STMMI.MI"}


def sec_rate(day):
    """SEC Section 31 fee per USD of sales, by fiscal period (approximate)."""
    for start, rate in (("2025-05-14", 0.0), ("2024-05-22", 0.0000278), ("2023-05-22", 0.000008),
                        ("2022-05-23", 0.0000229), ("2021-02-25", 0.0000051), ("2020-02-19", 0.0000221),
                        ("2019-05-22", 0.0000207), ("2018-04-10", 0.0000130), ("2017-10-01", 0.0000231),
                        ("2016-10-01", 0.0000218), ("2015-10-01", 0.0000218)):
        if day >= dnum(start):
            return rate
    return 0.0000184


def winter(day):
    """UK winter time (last Sunday of October .. last Sunday of March)."""
    ts = pd.Timestamp(np.datetime64(day, "D"))

    def last_sunday(y, m):
        d = pd.Timestamp(year=y, month=m, day=31)
        return d - pd.Timedelta(days=(d.weekday() + 1) % 7)
    return ts >= last_sunday(ts.year, 10) or ts < last_sunday(ts.year, 3)


def eu_ftt(t, day):
    if t in NL_ISSUERS:
        return 0.0
    suf = t.rsplit(".", 1)[1] if "." in t else ""
    if suf == "PA":
        return 0.002 if day < dnum("2017-01-01") else 0.003 if day < dnum("2025-04-01") else 0.004
    if suf == "MI":
        return 0.001
    if suf == "MC":
        return 0.002 if day >= dnum("2021-01-16") else 0.0
    return 0.0


def fees(t, m, side, n, px, day):
    """IBKR Pro Fixed commission + exchange/regulatory fees + taxes, in the trade currency."""
    v = n * px
    if m == "US":
        c = min(max(1.0, 0.005 * n), 0.01 * v)
        if side == "S":
            c += sec_rate(day) * v + min(8.30, 0.000166 * n)
        return c
    if m == "JP":
        return max(80.0, 0.0008 * v)
    if m == "EU":
        c = max(3.0, 0.0005 * v)
        if side == "B":
            c += eu_ftt(t, day) * v
        return c
    if m == "HK":
        c = max(18.0, 0.0008 * v) + math.ceil(hk_stamp_rate(day) * v) + 0.0000565 * v + 0.000027 * v
        c += (0.0000015 * v if day >= dnum("2022-01-01") else 0.0) + min(100.0, max(2.0, 0.00002 * v))
        return c
    if m == "CRYPTO":
        return max(1.75, 0.0018 * v)
    return 0.0


WHT = {"US": 0.30, "JP": 0.15315, "HK": 0.0, "CRYPTO": 0.0}
WHT_EU = {"PA": 0.25, "DE": 0.26375, "AS": 0.15, "MI": 0.26, "MC": 0.19, "BR": 0.30, "HE": 0.35, "LS": 0.25, "VI": 0.275}


def wht(t, m):
    if m == "EU":
        return WHT_EU.get(t.rsplit(".", 1)[1], 0.25)
    return WHT.get(m, 0.0)


# ---------------------------------------------------------------- simulator
class Sim:
    def __init__(self, cfg):
        _load()
        self.cfg = cfg
        self.T = DATA["tickers"]
        self.slots = cfg.get("slots", 15)
        self.cap = cfg.get("cap", 20000.0)
        self.cap_mode = cfg.get("cap_mode", "fixed")       # 'relative': cap = max(cap, cap_rel x NetLiq/15)
        self.cap_rel = cfg.get("cap_rel", 1.5)
        self.variant = cfg.get("variant", "E")
        v = self.variant
        self.e1 = E1[cfg.get("e1", "fixed")] if "E1" in v else None
        self.c6 = "C6" in v
        self.d3 = "D3" in v
        self.jp_morning = cfg.get("jp_morning", False)      # True: JP decided at the 09:00 run too
        self.jp1000 = cfg.get("jp1000", False)              # True: 1,000-share TSE units before 2018-10-01
        self.fx_friday = cfg.get("fx_friday", True)         # Friday-night conversions wait for Monday
        self.fx_pending = []
        self.fx_blocked = set()
        self.locked_hkd = 0.0
        self.crypto = cfg.get("crypto", False)
        # industry trend filter (PREREG_1026 #1): {ticker: (day numbers, blocked bool array)} - a new buy in
        # the ticker is skipped while its (market, sector) composite is below its long average
        self.sector_block = cfg.get("sector_block")
        self.kill = cfg.get("kill", "rerisk20")
        # bridge switches (all default to the live behaviour)
        self.fractional = cfg.get("fractional", False)    # True: exact shares, no lots
        self.fee_mode = cfg.get("fee_mode", "live")        # 'bp': research COST_BP per side, no minimums
        self.floor = cfg.get("floor", True)                # 12% floor on the first cut-loss
        self.net_div = cfg.get("net_div", True)            # False: dividends gross (no withholding)
        # structural switches (research-engine behaviour when flipped)
        self.rank = cfg.get("rank", "score")                # 'mom': raw 90d momentum, no top-20 cap
        # PREREG_1026c ranking arms: 'mkt_demote', 'mkt_minus25', 'voladj', 'random'; min_score: stricter E gate
        self.min_score = cfg.get("min_score")
        self.idx_rank = load_index(100) if self.rank in ("mkt_demote", "mkt_minus25") else None
        self.rank_seed = cfg.get("rank_seed", 0)
        g = cfg.get("gate")
        ge, gd = cfg.get("gate_e", g), cfg.get("gate_d3", g)
        if ge is not None or gd is not None:
            assert DATA.get("_gate"), "call use_gate_data() first"
            self.cards = gate_cards(0.30 if ge is None else ge, 0.30 if gd is None else gd)
        else:
            self.cards = DATA["cards"]
        self.overflow_runs, self.cand_runs, self._ovr, self._hadc = 0, 0, False, False
        # PREREG_1026d score components: fpct / mpct = {(ticker, bar): expanding percentile among EARLIER signal bars,
        # sign-aligned so higher = better}; rank 'feat' orders by the component, 'blend' by the mean of component and
        # momentum percentiles; fcut: skip a card whose component percentile is below it (None = no filter)
        self.fpct, self.mpct = cfg.get("fpct"), cfg.get("mpct")
        self.fcut = cfg.get("fcut")
        self.fcut_kinds = cfg.get("fcut_kinds", ("E", "D3"))
        self.fcut_tab = cfg.get("fcut_pct", self.fpct)         # filter table; default = the rank table (mixes: another one)
        self.sizing = cfg.get("sizing", "nl15")             # 'cashfree': all cash / free slots
        self.fx_pool = cfg.get("fx_pool", False)            # True: one fungible pot, free conversions
        self.fill_mode = cfg.get("fill_mode", "limit")      # 'open': always fill at the open
        self.anchor = cfg.get("anchor", "signal")           # 'fill': entry/hw/stop from the fill open
        self.all_enterable = cfg.get("all_enterable", False)
        self.one_run = cfg.get("one_run", False)            # True: only the 23:35 run
        self.fx_convert = cfg.get("fx_convert", False)      # True: FX_CONVERT=1, HKD may fund other buys
        # proposal: the bot may convert its OWN HKD (its fills; never earmarked owner money, never HK
        # dividends, which live outside the bot's pocket) to fund buys in other currencies
        self.hkd_fix = cfg.get("hkd_fix", False)
        self.btc = cfg.get("btc")                           # None | BTC-slot settings (see patch_btc.py)
        self.btc_units, self.btc_inv, self.btc_last = 0.0, 0.0, None
        self.btc_log = {"trades": 0, "costs": 0.0, "fee": 0.0, "long_days": 0, "short_days": 0, "entries": 0, "fund_fail": 0}
        self.park = cfg.get("park")                         # None | key of PARK: idle-cash option
        self.park_bal, self.park_last, self.park_roll, self.park_free_prev = 0.0, None, None, 0.0
        self.park_log = {"income": 0.0, "ibint": 0.0, "costs": 0.0, "trades": 0}
        self.park_bot = self.park is not None and PARK[self.park].get("mode") == "bot"
        self.park_led, self.park_per_slot = 0.0, 0.0        # PREREG_1028_park side USD balance / run's per-slot size
        if self.park_bot:
            self.park_log.update(draws=0, refills=0, parks=0, lag_cost=0.0, lag_days=0, lag_max=0.0,
                                 share_sum=0.0, buf_sum=0.0, steps=0, parked_hkd_sum=0.0)
            self.park_y = {}
        if self.hkd_fix:
            self.fx_convert = True
        self.hk_from = dnum(cfg["hk_from"]) if cfg.get("hk_from") else None
        self.start, self.end = dnum(cfg.get("start", "2015-05-04")), dnum(cfg.get("end", "2026-10-23"))
        self.cash = {"USD": 0.0, "JPY": 0.0, "EUR": 0.0, "HKD": 0.0, "GBP": 0.0, "CHF": 0.0, "DKK": 0.0}
        self.res = {c: 0.0 for c in self.cash}
        cap0 = cfg.get("capital", 100000.0)
        r = self.rate("USD", self.start - 1)
        self.cash["USD"] = cap0 / r - 2.0           # day-0 owner conversion of the HKD start into USD
        self.flows = {dnum(d): a for d, a in cfg.get("flows", [])}
        self.pos, self.pend = {}, {}
        self.trades, self.events, self.curve = [], [], []
        self.nl_hist = []                            # (day, NL) one per weekday at 23:35
        self.peak_since = self.start
        self.halt_days = 0
        self.killed_days = []
        self.reduced_days = []
        self.mkt_days = 0
        self.tight_now = False
        self.size_mult = 1.0
        self.max_pos = None
        import re as _re
        parts = self.kill.split("+")
        self.kill = parts[1] if len(parts) > 1 else parts[0]
        _m = _re.match(r"mkt(half)?(\d+)$", parts[0])
        self.mkt_half = bool(_m and _m.group(1))
        self.idx = load_index(int(_m.group(2))) if _m else None
        if _m and cfg.get("idx_tab") is not None:      # PREREG_1027 placebo: the index-below table shifted in time
            self.idx = cfg["idx_tab"]
        self.skips = {}
        self.costs = {"commission": 0.0, "fx": 0.0, "div": 0.0}
        self.costs_y = {}
        self.fx_count = 0
        # PREREG_1028_stop: None = today's close check (default); "close" = the same rule with the cut-loss log on;
        # "intraday" = a resting stop at IBKR (stop_slip below the level; stop_attach = in force from the fill bar)
        self.stop_mode = cfg.get("stop_mode")
        self.stop_slip = cfg.get("stop_slip", 0.001)
        self.stop_attach = cfg.get("stop_attach", False)
        self.stop_log = []
        # PREREG_1028_loss: all None = today's rules
        self.ttw = cfg.get("ttw")              # {"n": weekdays, "mode": "once"|"cont", "need": "entry"|"atr", "x": ATR mult}
        self.be = cfg.get("be")                # {"trig": f, "floor": g}: once hw >= entry x (1+f), stop >= entry x (1+g)
        self.sec_cap = cfg.get("sec_cap")      # max holdings + working buys sharing one SEC_OF label
        self.earn_block = cfg.get("earn_block")    # skip a buy that would meet an earnings reaction within N sessions after the fill
        self.earn_exit = cfg.get("earn_exit")      # sell before an earnings reaction unless close >= entry x (1 + x)
        self.news_block = cfg.get("news_block")    # PREREG_1028_news: skip a buy with a NEWS_EV event in the N sessions up to the signal
        self.news_mkt = cfg.get("news_mkt", "US")          # None = every market
        self.rand_skip = cfg.get("rand_skip")      # placebo: {"p": share, "seed": int} skip news_mkt buys at random
        self.macro_block = cfg.get("macro_block")  # skip every new buy while MACRO says the shock is on (latest known US close)
        self.macro_tight = cfg.get("macro_tight", False)    # Note C: while the shock is on, trail every position at 2.0 ATR
        self.macro_exit = cfg.get("macro_exit", False)      # Note C: while the shock is on, sell a position closing below its entry
        self.news_exit = cfg.get("news_exit", False)        # Note F: sell at the next open after a NEWS_EXIT event on the decided session
        self.loss_log = {"ttw": 0, "earn_exit": 0, "sec_skip": 0, "earn_skip": 0, "be_raise": 0, "news_skip": 0,
                         "rand_skip": 0, "news_cand": 0, "macro_skip": 0,
                         "macro_exit": 0, "macro_tight_days": 0, "news_exit": 0}

    # ---- helpers
    def fee(self, t, m, side, n, px, day):
        if self.fee_mode == "bp":
            # engine/config.py COST_BP (per side, commission + slippage, no minimums)
            return {"US": 10, "HK": 25, "JP": 15, "EU": 15, "CRYPTO": 20}[m] / 10_000 * n * px
        return fees(t, m, side, n, px, day)

    def add_cost(self, kind, amt, day):
        self.costs[kind] += amt
        y = int(pd.Timestamp(np.datetime64(day, "D")).year)
        self.costs_y.setdefault(y, {"commission": 0.0, "fx": 0.0, "div": 0.0})[kind] += amt

    def rate(self, c, day):
        if c == "HKD":
            return 1.0
        d, v = DATA["fxa"][c]
        i = np.searchsorted(d, day, "right") - 1
        return float(v[max(i, 0)])

    def last_idx(self, r, cutoff):
        return int(np.searchsorted(r["dn"], cutoff, "right") - 1)

    def macro_on(self, day, run):
        if MACRO is None:
            return False
        md_, mb_ = MACRO
        k_ = int(np.searchsorted(md_, day - 1 if run == "0900" else day, "right")) - 1
        return bool(k_ >= 0 and mb_[k_])

    def cutoff(self, m, day, run):
        if run == "0900" and m in ("US", "EU", "CRYPTO"):
            return day - 1
        return day

    def decidable(self, m, run, day=None):
        if run == "2335" or m in ("US", "CRYPTO"):
            return True
        if m == "JP":
            return self.jp_morning
        if m == "HK" and day is not None:
            return winter(day)          # the morning run is 10:00 UTC in UK winter, after HK settles
        return False

    def skip(self, why, day):
        y = int(pd.Timestamp(np.datetime64(day, "D")).year)
        self.skips.setdefault(y, {}).setdefault(why, 0)
        self.skips[y][why] += 1

    def netliq(self, day, run):
        rt = day - 1 if run == "0900" else day
        nl = sum(v * self.rate(c, rt) for c, v in self.cash.items())
        for t, p in self.pos.items():
            r = self.T[t]
            i = self.last_idx(r, self.cutoff(r["m"], day, run))
            nl += p["sa"] * r["raw_c"][max(i, 0)] * self.rate(r["ccy"], rt)
        if self.btc is not None and (self.btc_units > 0 or self.btc_inv > 0):
            nl += self.btc_value(rt)
        if self.park_led:
            nl += self.park_led * self.rate("USD", rt)
        return nl

    # ---- BTC slot
    def btc_px(self, d):
        """BTC close (USD) of UTC day d (last available)."""
        M = m1_daily()
        i = int(np.searchsorted(M["days"], d, "right") - 1)
        return float(M["close"][max(i, 0)])

    def btc_value(self, d):
        fx = self.rate("USD", d)
        v = self.btc_units * self.btc_px(d) * fx
        if self.btc_inv > 0 and self.btc_last is not None and d > self.btc_last:
            # inverse value is carried to the previous UTC close each morning; mark the partial day
            v += self.btc_inv * (1.0 - (self.btc_px(d) / self.btc_px(self.btc_last) - 1.0))
        elif self.btc_inv > 0:
            v += self.btc_inv
        return v

    def btc_daily(self, day):
        """Every calendar day: fund fee on the ETF, daily-reset compounding of the inverse product."""
        B = self.btc
        if self.btc_units > 0:
            self.btc_units *= 1.0 - B.get("fee_yr", 0.01) / 365.0
            self.btc_log["fee"] += self.btc_units * self.btc_px(day - 1) * self.rate("USD", day - 1) * B.get("fee_yr", 0.01) / 365.0
        if self.btc_inv > 0 and self.btc_last is not None and day in hk_calendar():
            # the -1x product resets once per HK business day: one reset over each weekend / holiday
            r = self.btc_px(day - 1) / self.btc_px(self.btc_last) - 1.0
            self.btc_inv *= (1.0 - r) * (1.0 - B.get("inv_fee_yr", 0.0199) * (day - 1 - self.btc_last) / 365.0)
            self.btc_last = day - 1

    def btc_cost(self, v):
        return max(18.0, 0.0008 * v) + 0.0000865 * v + 0.0010 * v

    def btc_step(self, day):
        """HK session: follow M1's position at the end of the previous UTC day (long via the spot ETF, short via the
        -1x inverse product when enabled); size a new entry as k x (free cash / free slots)."""
        B = self.btc
        if day not in hk_calendar():
            return
        # conversions that landed over the weekend are credited before this session (as the next run would)
        if self.fx_pending:
            for c_, amt in self.fx_pending:
                self.cash[c_] += amt
            self.fx_pending = []
        self.fx_blocked = set()
        M = m1_daily()
        j = int(np.searchsorted(M["days"], day - 1, "right") - 1)
        if j < 0:
            return
        hyb = B.get("hybrid", False)
        pos = int((M["hpos"] if hyb else M["pos"])[j]); bucket = (M["hbucket"] if hyb else M["bucket"])[j]
        if pos == 1:
            self.btc_log["long_days"] += 1
        elif pos == -1:
            self.btc_log["short_days"] += 1
        fx = self.rate("USD", day - 1)
        px = self.btc_px(day - 1) * fx                    # HKD per BTC at this session
        share = px / 64000.0                              # 3439: about BTC/64,000 per share
        # exits
        if self.btc_units > 0 and pos != 1:
            v = self.btc_units * px
            c = self.btc_cost(v)
            self.cash["HKD"] += v - c
            self.btc_log["costs"] += c; self.btc_log["trades"] += 1
            self.btc_units = 0.0
        if self.btc_inv > 0 and not (pos == -1 and B.get("ls")):
            v = self.btc_inv
            c = self.btc_cost(v)
            self.cash["HKD"] += v - c
            self.btc_log["costs"] += c; self.btc_log["trades"] += 1
            self.btc_inv, self.btc_last = 0.0, None
        want = 1 if pos == 1 else (-1 if (pos == -1 and B.get("ls")) else 0)
        if want == 0 or (want == 1 and self.btc_units > 0) or (want == -1 and self.btc_inv > 0):
            return
        # entry size: k slots of free cash / free slots (this slot included), never beyond what can be funded
        rday = day - 1
        cash_free = sum((v - self.res[cc] - (self.locked_hkd if cc == "HKD" else 0.0)) * self.rate(cc, rday)
                        for cc, v in self.cash.items())
        if self.sizing == "cashfree":
            slot = max(0.0, cash_free) / max(1, self.slots - len(self.pos) - len(self.pend))
        else:
            slot = self.netliq(day, "0900") / self.slots
        k = B.get("k", 1.0) if want == 1 else B.get("k_short", B.get("k", 1.0))
        if k == "conv":
            k = (M1_CONV if want == 1 else M1_CONV_SHORT).get(bucket, 2.5 if want == 1 else 1.0)
        notional = min(k * slot, max(0.0, cash_free) * 0.97)
        if B.get("cap_k") and self.cap is not None:
            notional = min(notional, k * self.cap)          # the order cap applies per slot of Bitcoin too
        # never beyond what ONE conversion can fund (ensure() draws on a single source currency, x1.03 for HKD)
        spend = lambda c_: self.cash[c_] - self.res[c_] - (self.locked_hkd if c_ == "HKD" else 0.0)
        others = [max(0.0, spend(c_)) * self.rate(c_, rday) for c_ in self.cash if c_ != "HKD"]
        fundable = max(0.0, spend("HKD")) + max(0.0, (max(others) if others else 0.0) - 2.0 * self.rate("USD", rday)) / (1.03 * 1.0002)
        notional = min(notional, (fundable * 0.994 - 18.0) / 1.0019)
        if want == 1:
            n_sh = math.floor(notional / share / 100.0) * 100.0
            v = n_sh * share
        else:
            v = notional
        if v < 1000.0:
            return
        c = self.btc_cost(v)
        if not self.ensure("HKD", v + c, rday, day, None):
            self.btc_log["fund_fail"] += 1
            return
        self.cash["HKD"] -= v + c
        self.btc_log["costs"] += c; self.btc_log["trades"] += 1; self.btc_log["entries"] += 1
        if want == 1:
            self.btc_units = v / px
        else:
            self.btc_inv, self.btc_last = v, day - 1

    def ensure(self, ccy, need, rday, day=None, run=None):
        avail = self.cash[ccy] - self.res[ccy] - (self.locked_hkd if ccy == "HKD" else 0.0)
        if avail >= need:
            return True
        if ccy in self.fx_blocked:
            return False
        short = need - avail
        buf = 1.03 if ccy == "HKD" else 1.02
        spend = lambda c: self.cash[c] - self.res[c] - (self.locked_hkd if c == "HKD" else 0.0)
        srcs = [c for c in self.cash if c != ccy and (c != "HKD" or self.fx_convert) and spend(c) > 0]
        srcs.sort(key=lambda c: -spend(c) * self.rate(c, rday))
        for s in srcs:
            need_src = short * buf * self.rate(ccy, rday) / self.rate(s, rday)
            fee_src = (2.0 * self.rate("USD", rday) + 0.0002 * short * buf * self.rate(ccy, rday)) / self.rate(s, rday)
            if spend(s) >= need_src + fee_src:
                self.cash[s] -= need_src + fee_src
                if (self.fx_friday and run == "2335" and day is not None
                        and pd.Timestamp(np.datetime64(day, "D")).weekday() == 4):
                    # IDEALPRO is shut from Friday ~21:00 UTC: the order rests, the money lands
                    # Sunday ~21:15 UTC, this entry is skipped and the currency stays blocked
                    self.fx_pending.append((ccy, short * buf))
                    self.fx_blocked.add(ccy)
                    self.add_cost("fx", fee_src * self.rate(s, rday), rday)
                    self.fx_count += 1
                    return False
                self.cash[ccy] += short * buf
                self.add_cost("fx", fee_src * self.rate(s, rday), rday)
                self.fx_count += 1
                return True
        return False

    # ---- fills at the opens
    def fill_market(self, day, markets):
        for t in [t for t, p in self.pos.items() if p.get("exit") and self.T[t]["m"] in markets]:
            p, r = self.pos[t], self.T[t]
            i = self.last_idx(r, day)
            if i < 0 or r["dn"][i] != day or day < p["exit"]["earliest"]:
                continue
            px = float(r["o"][i])
            n = p["sa"] / r["F"][i]
            f = self.fee(t, r["m"], "S", n, px, day)
            proceeds = n * px - f
            self.cash[r["ccy"]] += proceeds
            rt = self.rate(r["ccy"], day)
            self.add_cost("commission", f * rt, day)
            tr = dict(p["rec"])
            tr.update(exit_signal=p["exit"]["decided"], exit_date=day, exit_px=px, exit_shares=n, reason=p["exit"]["reason"],
                      proceeds_hkd=proceeds * rt, exit_fee_hkd=f * rt, div_hkd=p["div_hkd"])
            tr["pnl_hkd"] = tr["proceeds_hkd"] + tr["div_hkd"] - tr["cost_hkd"]
            tr["ret"] = tr["pnl_hkd"] / tr["cost_hkd"]
            tr["hold_weekdays"] = int(np.busday_count(np.datetime64(tr["fill_date"], "D"), np.datetime64(day, "D")))
            self.trades.append(tr)
            if self.stop_mode is not None and p["exit"]["reason"].startswith("cut-loss"):
                self.stop_log.append(dict(kind="close", ticker=t, market=r["m"], day=day, decided=p["exit"]["decided"],
                                          stop=p["exit"]["stop"], px=px, close=p["exit"]["close"], F=float(r["F"][i]),
                                          backstop=self.stop_mode == "intraday"))
            del self.pos[t]
        if self.stop_mode == "intraday":
            # PREREG_1028_stop: the resting stop of every holding with no exit pending, from the session after the fill
            for t in [t for t, p in self.pos.items() if not p.get("exit") and self.T[t]["m"] in markets]:
                p, r = self.pos[t], self.T[t]
                i = self.last_idx(r, day)
                if i < 0 or r["dn"][i] != day or day <= p["rec"]["fill_date"]:
                    continue
                self._stop_check(t, p, r, i, day)
        for t in [t for t, o in self.pend.items() if self.T[t]["m"] in markets]:
            o, r = self.pend[t], self.T[t]
            i = self.last_idx(r, day)
            if i < 0 or r["dn"][i] != day or day < o["earliest"]:
                continue
            L = o["limit"] if self.fill_mode == "limit" else float("inf")
            if r["o"][i] <= L:
                px = float(r["o"][i])
            elif r["l"][i] <= L:
                px = L
            else:
                self.res[r["ccy"]] -= o["n"] * L
                del self.pend[t]
                self.skip("limit not reached", day)
                continue
            self.res[r["ccy"]] -= o["n"] * o["limit"]
            f = self.fee(t, r["m"], "B", o["n"], px, day)
            cost = o["n"] * px + f
            self.cash[r["ccy"]] -= cost
            rt = self.rate(r["ccy"], day)
            self.add_cost("commission", f * rt, day)
            if self.anchor == "fill":
                o["entry"] = px
                if r["m"] == "CRYPTO":
                    o["stop0"] = px - 4.0 * o["atr0"]
                else:
                    o["stop0"] = max(px - 3.5 * o["atr0"], 0.88 * px) if self.floor else px - 3.5 * o["atr0"]
            self.pos[t] = {"sa": o["n"] * r["F"][i], "entry": o["entry"], "hw": o["entry"], "stop": o["stop0"],
                           "stamp": o["stamp"], "exit": None, "div_hkd": 0.0, "kind": o["kind"], "atr0": o["atr0"],
                           "rec": {"ticker": t, "market": r["m"], "kind": o["kind"], "signal_date": o["sig_day"],
                                   "order_run": o["run"], "fill_date": day, "fill_px": px, "shares": o["n"],
                                   "cost_hkd": cost * rt, "entry_fee_hkd": f * rt, "signal_close": o["entry"],
                                   "stop0": o["stop0"]}}
            del self.pend[t]
            if self.stop_mode == "intraday" and self.stop_attach:
                self._stop_check(t, self.pos[t], r, i, day)      # I2: the stop is attached to the buy, at stop0

    def _stop_check(self, t, p, r, i, day):
        """PREREG_1028_stop: the resting stop during bar i's session. Level = p['stop'] as set by the last run that
        decided the market. Open at or below it -> sold at the open (gap); else low at or below it -> sold at
        level x (1 - slip). Logs the event with its close-rule counterfactual (_cf_close)."""
        st = p["stop"]
        o, lo, c = float(r["o"][i]), float(r["l"][i]), float(r["c"][i])
        if o <= st:
            px, gap = o, True
        elif lo <= st:
            px, gap = st * (1.0 - self.stop_slip), False
        else:
            return
        state = dict(hw=p["hw"], stop=st, entry=p["entry"], stamp=p["stamp"])
        n = p["sa"] / r["F"][i]
        f = self.fee(t, r["m"], "S", n, px, day)
        proceeds = n * px - f
        self.cash[r["ccy"]] += proceeds
        rt = self.rate(r["ccy"], day)
        self.add_cost("commission", f * rt, day)
        tr = dict(p["rec"])
        tr.update(exit_signal=day, exit_date=day, exit_px=px, exit_shares=n, reason="cut-loss (intraday stop)",
                  proceeds_hkd=proceeds * rt, exit_fee_hkd=f * rt, div_hkd=p["div_hkd"])
        tr["pnl_hkd"] = tr["proceeds_hkd"] + tr["div_hkd"] - tr["cost_hkd"]
        tr["ret"] = tr["pnl_hkd"] / tr["cost_hkd"]
        tr["hold_weekdays"] = int(np.busday_count(np.datetime64(tr["fill_date"], "D"), np.datetime64(day, "D")))
        self.trades.append(tr)
        del self.pos[t]
        cf = self._cf_close(t, state, day, "U" if r["m"] == "US" else "N")
        self.stop_log.append(dict(kind="intraday", ticker=t, market=r["m"], day=day, stop=st, px=px, o=o, l=lo, c=c,
                                  gap=gap, shake=c > st, fill_day=day == p["rec"]["fill_date"], F=float(r["F"][i]),
                                  value_hkd=proceeds * rt, cf_day=cf["day"], cf_px=cf["px"], cf_F=cf["F"],
                                  cf_reason=cf["reason"], cf_decided=cf["decided"]))

    def _cf_close(self, t, st, day, after):
        """PREREG_1028_stop event-level counterfactual: the position the intraday stop sold in `day`'s session, kept
        under today's close rule (same exit loop, same runs, same decidable markets) until its own exit, sold at its
        market's next open. after = 'N' (sold in the morning fill: JP / HK / EU) or 'U' (the US fill)."""
        r = self.T[t]
        m = r["m"]
        hw, stop, entry, stamp = st["hw"], st["stop"], st["entry"], st["stamp"]
        PH = ("N", "0900", "U", "2335")
        k0 = PH.index(after) + 1
        pend = None
        d = day
        while d <= self.end:
            if (d + 3) % 7 < 5:                       # weekday (day 0 = 1970-01-01, a Thursday)
                for ph in PH[k0:]:
                    if ph in ("N", "U"):
                        if (ph == "U") != (m == "US") or pend is None or d < pend[2]:
                            continue
                        i = self.last_idx(r, d)
                        if i >= 0 and r["dn"][i] == d:
                            return {"day": d, "px": float(r["o"][i]), "F": float(r["F"][i]), "reason": pend[0], "decided": pend[1]}
                        continue
                    run = ph
                    if run == "2335" and r["dn"][-1] < d - 7 and r["dn"][-1] < self.end - 7:
                        return {"day": d, "px": float(r["c"][-1]), "F": float(r["F"][-1]),
                                "reason": "delisted / taken over (data ended)", "decided": d}
                    if pend is not None or not self.decidable(m, run, d):
                        continue
                    i = self.last_idx(r, self.cutoff(m, d, run))
                    if i < 0:
                        continue
                    c, a, s = float(r["c"][i]), float(r["atr_act"][i]), float(r["sma200_act"][i])
                    reason = None
                    if np.isfinite(s) and c < s:
                        reason = "regime (close < 200-day)"
                    else:
                        hw = max(hw, c)
                        k = 2.0 if (c >= entry + 1.5 * a or self.tight_now) else 3.5
                        stop = max(stop, hw - k * a) if a > 0 else stop
                        if c <= stop:
                            reason = "cut-loss (trailing stop)"
                        elif np.busday_count(np.datetime64(stamp + 1, "D"), np.datetime64(d + 1, "D")) >= 60:
                            reason = "time stop (60 weekdays)"
                        elif self.c6 and r.get("C6") is not None and r["C6"][i]:
                            reason = "bearish chart pattern"
                    if reason:
                        pend = (reason, d, d if (run == "0900" and m == "US") else d + 1)
            k0 = 0
            d += 1
        i = self.last_idx(r, self.end)
        return {"day": None, "px": float(r["c"][i]), "F": float(r["F"][i]), "reason": "held at end", "decided": None}

    def dividends(self, day, markets):
        for t, p in self.pos.items():
            r = self.T[t]
            if r["m"] not in markets:
                continue
            i = self.last_idx(r, day)
            if i >= 0 and r["dn"][i] == day and r["div"][i] > 0:
                amt = p["sa"] * r["div"][i] * (1 - (wht(t, r["m"]) if self.net_div else 0.0))
                self.cash[r["ccy"]] += amt
                if r["ccy"] == "HKD":
                    self.locked_hkd += amt      # not an execution: outside the bot's HKD pocket
                h = amt * self.rate(r["ccy"], day)
                p["div_hkd"] += h
                self.add_cost("div", h, day)

    def _cash_out(self, t, p, r, day):
        px = float(r["c"][-1])
        n = p["sa"] / r["F"][-1]
        f = self.fee(t, r["m"], "S", n, px, day)
        self.cash[r["ccy"]] += n * px - f
        rt = self.rate(r["ccy"], day)
        self.add_cost("commission", f * rt, day)
        tr = dict(p["rec"])
        tr.update(exit_signal=day, exit_date=day, exit_px=px, exit_shares=n, reason="delisted / taken over (data ended)",
                  proceeds_hkd=(n * px - f) * rt, exit_fee_hkd=f * rt, div_hkd=p["div_hkd"])
        tr["pnl_hkd"] = tr["proceeds_hkd"] + tr["div_hkd"] - tr["cost_hkd"]
        tr["ret"] = tr["pnl_hkd"] / tr["cost_hkd"]
        tr["hold_weekdays"] = int(np.busday_count(np.datetime64(tr["fill_date"], "D"), np.datetime64(day, "D")))
        self.trades.append(tr)
        del self.pos[t]

    # ---- risk policy (kill-switch review)
    def risk_state(self, day, run, nl):
        """Sets size_mult / max_pos / tight_now for this run; returns True when new buys are blocked."""
        import re
        pol = self.kill
        self.size_mult, self.max_pos, self.tight_now = 1.0, None, False
        hist = [v for _, v in self.nl_hist]
        block = False
        if run == "2335" and self.idx is not None:
            self.mkt_days += sum(index_below(self.idx, mk, day) for mk in ("US", "HK", "JP", "EU"))
        if pol == "off" or pol.startswith("mkt"):
            return False
        if pol == "strict" or pol.startswith("rerisk"):
            h = [v for d, v in self.nl_hist if d >= self.peak_since]
            peak = max(h + [nl])
            block = nl < 0.92 * peak
            if run == "2335":
                if block:
                    self.halt_days += 1
                    n = int(pol[6:]) if pol.startswith("rerisk") else None
                    if n is not None and self.halt_days >= n:
                        self.events.append((day, "re-risk: operator resets the kill-switch peak", nl, peak))
                        self.peak_since = day
                        self.halt_days = 0
                        block = False
                    else:
                        self.killed_days.append(day)
                else:
                    self.halt_days = 0
            return block
        m = re.match(r"(half_|slots8_|tight_)?roll(\d+)$", pol)
        if m:
            w = int(m.group(2))
            peak = max(hist[-w:] + [nl])
            dd = nl < 0.92 * peak
            if dd:
                kind = m.group(1)
                if kind is None:
                    block = True
                elif kind == "half_":
                    self.size_mult = 0.5
                elif kind == "slots8_":
                    self.max_pos = 8
                elif kind == "tight_":
                    self.size_mult = 0.5
                    self.tight_now = True
        elif pol == "ladder":
            peak = max(hist[-250:] + [nl])
            d = nl / peak - 1
            if d <= -0.16:
                block = True
            elif d <= -0.12:
                self.size_mult = 0.25
            elif d <= -0.08:
                self.size_mult = 0.5
        else:
            m = re.match(r"eqma(half)?(\d+)$", pol)
            if not m:
                raise ValueError(pol)
            w = int(m.group(2))
            if len(hist) >= w and nl < sum(hist[-w:]) / w:
                if m.group(1):
                    self.size_mult = 0.5
                else:
                    block = True
        if run == "2335":
            if block:
                self.killed_days.append(day)
            elif self.size_mult < 1 or self.max_pos is not None or self.tight_now:
                self.reduced_days.append(day)
        return block

    # ---- a trading run
    def run(self, day, run):
        self._ovr = self._hadc = False
        out = self._run_core(day, run)
        self.overflow_runs += self._ovr
        self.cand_runs += self._hadc
        if run == "2335" and self.park is not None:
            if self.park_bot:
                self.park_step_bot(day, self.curve[-1][1])
            else:
                self.park_step(day, self.curve[-1][1])
        return out

    def park_slot(self, free, day):
        """PREREG_1028_park: the bot's per-slot size at this run's sizing step (free cash / free slots, 0 when no
        slot is free), measured before the entries; the side balance is cash the bot would see."""
        if free <= 0:
            return 0.0
        cf = sum((v - self.res[cc] - (self.locked_hkd if cc == "HKD" else 0.0)) * self.rate(cc, day)
                 for cc, v in self.cash.items() if cc != "HKD" or self.fx_convert)
        cf += self.park_led * self.rate("USD", day)
        return max(0.0, cf) / free

    def park_step_bot(self, day, nl):
        """PREREG_1028_park: SGOV parking with the bot's rule (ib_bot.park_plan), once a day after the 23:35 run.
        U = unparked spendable USD = cash - reserved + side balance - parked. A buy / conversion that used parked
        money since the last step sold that SGOV in the same run (a 'draw'); then: park the excess over the buffer
        once >= thr (whole shares at px x 1.005); or, holding SGOV and U < buffer / 2, sell back to the buffer
        (a 'refill'). buffer = buf_slots x max(this run's per-slot size, NetLiq / 15) in USD."""
        P = PARK[self.park]
        usd = self.rate("USD", day)
        nav_usd = nl / usd
        dt = 0 if self.park_last is None else day - self.park_last
        self.park_last = day
        L = self.park_log
        Y = self.park_y.setdefault(int(pd.Timestamp(np.datetime64(day, "D")).year),
                                   {"income": 0.0, "ibint": 0.0, "costs": 0.0, "trades": 0, "share_sum": 0.0, "steps": 0,
                                    "nav_sum": 0.0})
        spend0 = self.cash["USD"] - self.res["USD"] + self.park_led
        if dt > 0:
            held = min(self.park_bal, max(0.0, spend0))       # money a buy took since the last step earns nothing
            if held > 0:
                inc = held * max(0.0, bill_yield(day) - P["fee"]) * dt / 365.0 * (1.0 - P["wht"])
                self.park_led += inc
                L["income"] += inc * usd; Y["income"] += inc * usd
            f = (1.0 if nav_usd > 100000.0 else 0.0) if day < dnum("2019-01-01") else min(1.0, nav_usd / 100000.0)
            ib = max(0.0, self.park_free_prev - 10000.0) * max(0.0, dff_rate(day) - 0.005) * f * dt / 360.0
            self.park_led += ib
            L["ibint"] += ib * usd; Y["ibint"] += ib * usd
        if P["sweep"]:
            px = P["px"]
            cost = lambda a: max(P["cmin"], P["cps"] * a / px) + P["hs"] * a

            def trade(a, kind):
                c = cost(a)
                self.park_led -= c
                L["costs"] += c * usd; Y["costs"] += c * usd
                L["trades"] += 1; Y["trades"] += 1; L[kind] += 1

            spend = self.cash["USD"] - self.res["USD"] + self.park_led
            free = spend - self.park_bal
            buf = P["buf_slots"] * max(self.park_per_slot, nl / self.slots) / usd
            if free < 0 and self.park_bal > 0:
                # sold in the same run as the buy that needed it: the shortfall and this sale's cost, whole shares
                a = min(self.park_bal, math.ceil((-free + cost(-free)) * 1.001 / px) * px)
                trade(a, "draws")
                if P["lag"]:
                    b = a * (dff_rate(day) + 0.015) * P["lag"] / 360.0
                    self.park_led -= b
                    L["lag_cost"] += b * usd; L["costs"] += b * usd; Y["costs"] += b * usd
                    L["lag_days"] += 1; L["lag_max"] = max(L["lag_max"], a * usd)
                self.park_bal -= a
                if self.park_bal < 1e-6:
                    self.park_bal = 0.0
                free = self.cash["USD"] - self.res["USD"] + self.park_led - self.park_bal
            excess = free - buf
            if excess >= P["thr"]:
                n = math.floor(excess / (px * (1.0 + PARK_LIMIT_BUFFER)))
                if n > 0:
                    self.park_bal += n * px
                    trade(n * px, "parks")
            elif self.park_bal > 0 and free < buf / 2.0:
                a = min(self.park_bal, math.ceil((buf - free) / px) * px)
                if a > 0:
                    trade(a, "refills")
                    self.park_bal -= a
                    if self.park_bal < 1e-6:
                        self.park_bal = 0.0
            L["buf_sum"] += buf * usd / nl
        sh = self.park_bal * usd / nl
        L["share_sum"] += sh; L["steps"] += 1; L["parked_hkd_sum"] += self.park_bal * usd
        Y["share_sum"] += sh; Y["steps"] += 1; Y["nav_sum"] += nl
        self.park_free_prev = self.cash["USD"] + self.park_led - self.park_bal

    def park_step(self, day, nl):
        P = PARK[self.park]
        usd = self.rate("USD", day)
        nav_usd = nl / usd
        dt = 0 if self.park_last is None else day - self.park_last
        self.park_last = day
        L = self.park_log
        spend0 = self.cash["USD"] - self.res["USD"]
        if dt > 0:
            held = min(self.park_bal, max(0.0, spend0))       # money spent by buys since the last step was sold
            if held > 0:
                inc = held * max(0.0, bill_yield(day) - P["fee"]) * dt / 365.0 * (1.0 - P["wht"])
                self.cash["USD"] += inc
                L["income"] += inc * usd
            # IBKR credit interest on the USD that was left in cash at the previous step
            f = (1.0 if nav_usd > 100000.0 else 0.0) if day < dnum("2019-01-01") else min(1.0, nav_usd / 100000.0)
            ib = max(0.0, self.park_free_prev - 10000.0) * max(0.0, dff_rate(day) - 0.005) * f * dt / 360.0
            self.cash["USD"] += ib
            L["ibint"] += ib * usd
        if not P["sweep"]:
            self.park_free_prev = self.cash["USD"]
            return
        cost = lambda a: max(P["cmin"], P["cps"] * a / P["px"], P["cbp"] * a) + P["hs"] * a
        # roll maturing bills (one new purchase per maturity)
        if P["roll_days"] and self.park_bal > 0 and self.park_roll is not None and day - self.park_roll >= P["roll_days"]:
            c = cost(self.park_bal)
            self.cash["USD"] -= c; L["costs"] += c * usd; L["trades"] += 1
            self.park_roll = day
        spend = self.cash["USD"] - self.res["USD"]
        buf = P["buf_slots"] * nav_usd / self.slots
        target = None
        if spend < self.park_bal:                       # buys used parked money: sell back to the buffer
            target = max(0.0, spend - buf)
            if P["lag"]:                                # sold at the next morning run, settles after the buy it funds
                d = (self.park_bal - max(0.0, spend)) * (dff_rate(day) + 0.015) * P["lag"] / 360.0
                self.cash["USD"] -= d; L["costs"] += d * usd
        elif spend - self.park_bal >= buf + P["thr"] and bill_yield(day) >= P["ymin"]:
            target = spend - buf
        elif self.park_bal > 0 and bill_yield(day) < P["ymin"]:
            target = 0.0                                # yields too low to cover costs: stop parking
        if target is None:
            self.park_free_prev = self.cash["USD"] - self.park_bal
            return
        if P["lot"]:
            target = (target // P["lot"]) * P["lot"]
            target = min(target, max(0.0, spend))
        amt = abs(target - self.park_bal)
        if amt < 1.0:
            self.park_free_prev = self.cash["USD"] - self.park_bal
            return
        c = cost(amt)
        self.cash["USD"] -= c
        if target > self.park_bal and (self.park_bal <= 0 or self.park_roll is None):
            self.park_roll = day
        self.park_bal = max(0.0, target)
        if self.park_bal <= 0:
            self.park_roll = None
        L["costs"] += c * usd
        L["trades"] += 1
        self.park_free_prev = self.cash["USD"] - self.park_bal

    def _run_core(self, day, run):
        self.fx_blocked = set()
        if self.fx_pending and not (run == "2335" and pd.Timestamp(np.datetime64(day, "D")).weekday() == 4):
            for c, amt in self.fx_pending:
                self.cash[c] += amt
            self.fx_pending = []
        nl = self.netliq(day, run) + sum(a * self.rate(c, day) for c, a in self.fx_pending)
        if run == "2335":
            self.nl_hist.append((day, nl))
            cash_hkd = sum(v * self.rate(c, day) for c, v in self.cash.items())
            self.curve.append((day, nl, len(self.pos), cash_hkd))
        killed = self.risk_state(day, run, nl)
        # holdings whose price history ended (taken over / delisted): cash out at the last close
        if run == "2335":
            for t, p in list(self.pos.items()):
                r = self.T[t]
                if r["dn"][-1] < day - 7 and r["dn"][-1] < self.end - 7:
                    self._cash_out(t, p, r, day)
            for t, o in list(self.pend.items()):
                if day > o["earliest"] + 6:              # a DAY order that never met a session
                    self.res[self.T[t]["ccy"]] -= o["n"] * o["limit"]
                    del self.pend[t]
                    self.skip("order expired (no session)", day)
        # exits
        for t, p in list(self.pos.items()):
            if p["exit"]:
                continue
            r = self.T[t]
            if not self.decidable(r["m"], run, day):
                continue
            i = self.last_idx(r, self.cutoff(r["m"], day, run))
            if i < 0:
                continue
            c, a, s = float(r["c"][i]), float(r["atr_act"][i]), float(r["sma200_act"][i])
            reason = None
            if np.isfinite(s) and c < s:
                reason = "regime (close < 200-day)"
            else:
                p["hw"] = max(p["hw"], c)
                mt_ = self.macro_tight and self.macro_on(day, run)
                if mt_:
                    self.loss_log["macro_tight_days"] += 1
                k = 2.0 if (c >= p["entry"] + 1.5 * a or self.tight_now or mt_) else 3.5
                trail = max(p["stop"], p["hw"] - k * a) if a > 0 else p["stop"]
                if self.be is not None and p["hw"] >= p["entry"] * (1.0 + self.be["trig"]):
                    be_lvl = p["entry"] * (1.0 + self.be["floor"])
                    if be_lvl > trail:
                        trail = be_lvl
                        self.loss_log["be_raise"] += 1
                p["stop"] = trail
                held = np.busday_count(np.datetime64(p["stamp"] + 1, "D"), np.datetime64(day + 1, "D"))
                if c <= trail:
                    reason = "cut-loss (trailing stop)"
                elif held >= 60:
                    reason = "time stop (60 weekdays)"
                elif self.c6 and r.get("C6") is not None and r["C6"][i]:
                    reason = "bearish chart pattern"
                if reason is None and self.ttw is not None and held >= self.ttw["n"] and not (
                        self.ttw.get("mode", "once") == "once" and p.get("ttw_done")):
                    p["ttw_done"] = True
                    if self.ttw.get("placebo") is not None:
                        # PREREG_1028_loss robustness: exit a random share of the positions that reach day n, whatever
                        # their P&L (one draw per position, independent stream per seed)
                        import zlib
                        bad = np.random.default_rng([int(self.ttw["seed"]), zlib.crc32(f"{t}|{p['stamp']}".encode())]).random() < self.ttw["placebo"]
                    elif self.ttw.get("need", "entry") == "entry":
                        bad = c < p["entry"] * (1.0 + self.ttw.get("thr", 0.0))
                    else:
                        bad = p["hw"] < p["entry"] + self.ttw["x"] * p["atr0"]
                    if bad:
                        reason = "time-to-work"
                        self.loss_log["ttw"] += 1
                if reason is None and self.news_exit and NEWS_EXIT is not None:
                    ev = NEWS_EXIT.get(t)
                    if ev is not None and len(ev):
                        k_ = int(np.searchsorted(ev, r["dn"][i], "left"))
                        if k_ < len(ev) and ev[k_] == r["dn"][i]:
                            reason = "news exit"
                            self.loss_log["news_exit"] += 1
                if reason is None and self.macro_exit and c < p["entry"] and self.macro_on(day, run):
                    reason = "macro shock exit"
                    self.loss_log["macro_exit"] += 1
                if reason is None and self.earn_exit is not None and EARN_RS is not None and i + 2 < len(r["dn"]):
                    rs = EARN_RS.get(t)
                    if rs is not None and len(rs):
                        k_ = int(np.searchsorted(rs, r["dn"][i + 1], "right"))
                        if k_ < len(rs) and rs[k_] <= r["dn"][i + 2] and c < p["entry"] * (1.0 + self.earn_exit):
                            reason = "pre-earnings exit"
                            self.loss_log["earn_exit"] += 1
            if reason:
                earliest = day if (run == "0900" and r["m"] == "US") else day + 1
                p["exit"] = {"reason": reason, "decided": day, "earliest": earliest}
                if self.stop_mode is not None:
                    p["exit"].update(stop=p["stop"], close=c)
        # entries
        if killed:
            self.skip("kill switch blocked a run", day)
            return
        cap_pos = self.slots if self.max_pos is None else min(self.slots, self.max_pos)
        if self.btc is not None:
            cap_pos = min(cap_pos, self.btc.get("stock_slots", 14))
        free = cap_pos - len(self.pos) - len(self.pend)
        if self.park_bot and run == "2335":
            self.park_per_slot = self.park_slot(free, day)
        cl = self.card_list(day, run)
        rday = day - 1 if run == "0900" else day
        for kind, t, i in cl:
            r = self.T[t]
            if not self.decidable(r["m"], run, day):
                continue
            if t in self.pos or t in self.pend:
                continue
            self._hadc = True
            if free <= 0:
                self._ovr = True
                self.skip("no free slot", day)
                continue
            if (not r["enterable"] and not self.all_enterable) or (r["m"] == "CRYPTO" and not self.crypto):
                self.skip("not tradeable live (venue / pence / crypto)", day)
                continue
            if r["m"] == "HK" and self.hk_from is not None and day < self.hk_from:
                self.skip("HK switched off", day)
                continue
            mkt_low = self.idx is not None and index_below(self.idx, r["m"], self.cutoff(r["m"], day, run))
            if mkt_low and not self.mkt_half:
                self.skip("home index below its long average", day)
                continue
            if self.sector_block is not None and t in self.sector_block:
                sd, sb = self.sector_block[t]
                k = int(np.searchsorted(sd, self.cutoff(r["m"], day, run), "right")) - 1
                if k >= 0 and sb[k]:
                    self.skip("industry composite below its long average", day)
                    continue
            if self.sec_cap is not None and SEC_OF is not None and SEC_OF.get(t) is not None:
                lab = SEC_OF[t]
                if sum(1 for u in list(self.pos) + list(self.pend) if SEC_OF.get(u) == lab) >= self.sec_cap:
                    self.skip("sector full", day)
                    self.loss_log["sec_skip"] += 1
                    continue
            if self.earn_block is not None and EARN_RS is not None and i + 1 < len(r["dn"]):
                rs = EARN_RS.get(t)
                if rs is not None and len(rs):
                    k_ = int(np.searchsorted(rs, r["dn"][i + 1], "right"))
                    hi_ = r["dn"][min(i + self.earn_block, len(r["dn"]) - 1)]
                    if k_ < len(rs) and rs[k_] <= hi_:
                        self.skip("earnings soon", day)
                        self.loss_log["earn_skip"] += 1
                        continue
            if self.macro_block and self.macro_on(day, run):
                self.skip("macro shock", day)
                self.loss_log["macro_skip"] += 1
                continue
            if (self.news_block is not None or self.rand_skip is not None) and (self.news_mkt is None or r["m"] == self.news_mkt):
                self.loss_log["news_cand"] += 1
                if self.news_block is not None and NEWS_EV is not None:
                    ev = NEWS_EV.get(t)
                    if ev is not None and len(ev):
                        lo_ = r["dn"][max(0, i - self.news_block + 1)]
                        k_ = int(np.searchsorted(ev, lo_, "left"))
                        if k_ < len(ev) and ev[k_] <= r["dn"][i]:
                            self.skip("news event", day)
                            self.loss_log["news_skip"] += 1
                            continue
                if self.rand_skip is not None:
                    import zlib
                    if np.random.default_rng([int(self.rand_skip["seed"]), zlib.crc32(f"{t}|{int(r['dn'][i])}".encode())]).random() < self.rand_skip["p"]:
                        self.skip("random skip (placebo)", day)
                        self.loss_log["rand_skip"] += 1
                        continue
            c = float(r["c"][i])
            fx = self.rate(r["ccy"], rday)
            if self.sizing == "cashfree":
                # spendable cash in every currency (HK dividends sit outside the bot's pocket; and
                # without the HKD fix HKD can only fund HK buys, so it does not count for others)
                cash_free = sum((v - self.res[cc] - (self.locked_hkd if cc == "HKD" else 0.0)) * self.rate(cc, rday)
                                for cc, v in self.cash.items()
                                if cc != "HKD" or self.fx_convert or r["ccy"] == "HKD")
                # the BTC slot(s): reserved while unused (stocks are capped below 15), occupied while held
                btc_held = self.btc is not None and (self.btc_units > 0 or self.btc_inv > 0)
                btc_sl = self.btc.get("reserve", 1) if btc_held else 0
                notional = max(0.0, cash_free) / max(1, self.slots - len(self.pos) - len(self.pend) - btc_sl)
                # never size beyond what can actually be funded: own spendable cash in the order's
                # currency plus the single largest other source (funding draws on ONE source),
                # less the conversion buffer, fee and the 0.5% limit (review 2026-10-25)
                spend = lambda c_: self.cash[c_] - self.res[c_] - (self.locked_hkd if c_ == "HKD" else 0.0)
                ccy = r["ccy"]
                own = max(0.0, spend(ccy)) * self.rate(ccy, rday)
                others = [max(0.0, spend(c_)) * self.rate(c_, rday) for c_ in self.cash
                          if c_ != ccy and (c_ != "HKD" or self.fx_convert)]
                buf = 1.03 if ccy == "HKD" else 1.02
                biggest = max(others) if others else 0.0
                fundable = own + max(0.0, biggest - 2.0 * self.rate("USD", rday)) / (buf * 1.0002)
                notional = min(notional, fundable * 0.994)
            else:
                notional = nl / self.slots
            if self.cap is not None:
                cap = self.cap
                if self.cap_mode == "relative":
                    cap = max(self.cap, self.cap_rel * nl / self.slots)
                elif self.cap_mode == "slow":
                    # guards against a sudden NetLiq jump (misread / unrecorded deposit) but grows with
                    # the account: 1.25 x the median day-end NetLiq of the last 60 weekdays / 15
                    ref = [v for _, v in self.nl_hist[-60:]] or [nl]
                    cap = 1.25 * float(np.median(ref)) / self.slots
                notional = min(notional, cap)
            notional *= self.size_mult * (0.5 if mkt_low else 1.0)
            if self.fractional:
                n = notional / fx / c
            else:
                n = int(notional / fx / c)
                lot = r["lot"] or 1 if r["m"] != "HK" else (r["lot"] or 500)
                if self.jp1000 and r["m"] == "JP" and day < dnum("2018-10-01"):
                    lot = 1000
                if r["m"] == "CRYPTO":
                    lot = 1
                if lot > 1:
                    n = (n // lot) * lot
            if n <= 0:
                self.skip("one share / lot costs more than a slot", day)
                continue
            if self.fx_pool:
                # one fungible pot, free conversions, but never more than the cash the account has
                free_hkd = sum((v - self.res[cc]) * self.rate(cc, rday) for cc, v in self.cash.items())
                if free_hkd < n * c * fx:
                    self.skip("no cash in a fundable currency", day)
                    continue
            elif not self.ensure(r["ccy"], n * c, rday, day, run):
                self.skip("no cash in a fundable currency", day)
                continue
            L = limit_price(c, r["m"])
            a = float(r["atr_act"][i])
            stop0 = round(c - 4.0 * a, 2) if r["m"] == "CRYPTO" else (round(max(c - 3.5 * a, 0.88 * c), 2) if self.floor else round(c - 3.5 * a, 2))
            earliest = day if (run == "0900" and r["m"] == "US") else day + 1
            self.pend[t] = {"n": n, "limit": L, "entry": c, "stop0": stop0, "atr0": a, "stamp": day, "earliest": earliest,
                            "kind": kind, "sig_day": int(r["dn"][i]), "run": run}
            self.res[r["ccy"]] += n * L
            free -= 1

    def rank_key(self, r, t, i, day, run):
        """Sort key (ascending) for a candidate under the PREREG_1026c ranking arms; 'score' is the live order."""
        sc = int(r["score"][i])
        if self.rank in ("mkt_demote", "mkt_minus25"):
            below = index_below(self.idx_rank, r["m"], self.cutoff(r["m"], day, run))
            return (1 if below else 0, -sc) if self.rank == "mkt_demote" else (-(sc - (25 if below else 0)),)
        if self.rank == "voladj":
            va = r.get("_voladj")
            if va is None:
                c = np.asarray(r["adj_c"], float)
                ret = np.diff(np.log(c), prepend=np.nan)
                vol = pd.Series(ret).rolling(90, min_periods=60).std().to_numpy() * np.sqrt(252)
                va = r["_voladj"] = np.asarray(r["mom"], float) / np.where(vol > 0, vol, np.nan)
            x = va[i]
            return (-(x if np.isfinite(x) else -1e9),)
        if self.rank == "rawmom":
            return (-float(r["mom"][i]),)
        if self.rank == "feat":
            p = self.fpct.get((t, i))
            return (-(0.5 if p is None or p != p else p), -float(r["mom"][i]))
        if self.rank == "blend":
            p, q = self.fpct.get((t, i)), self.mpct.get((t, i))
            p = 0.5 if p is None or p != p else p
            q = 0.5 if q is None or q != q else q
            return (-(0.5 * p + 0.5 * q), -float(r["mom"][i]))
        if self.rank == "score_tiep":                       # placebo: live capped score, ties by an independent random table
            p = self.fpct.get((t, i))
            return (-sc, -(0.5 if p is None or p != p else p))
        if self.rank == "random":
            import zlib
            return (zlib.crc32(f"{t}|{i}|{self.rank_seed}".encode()),)
        return (-sc,)

    def card_list(self, day, run):
        T = self.T
        base, extra = [], []
        seen = set()
        for g, gd in DATA["gdates"].items():
            m = DATA["gmarket"][g]
            j = np.searchsorted(gd, self.cutoff(m, day, run), "right") - 1
            if j < 0:
                continue
            for kind, t, i in self.cards.get((g, int(gd[j])), ()):
                r = T[t]
                if r["removed"] is not None and day >= dnum(r["removed"]):
                    continue
                if r["added"] is not None and day < dnum(r["added"]):
                    continue
                if kind == "E":
                    if self.e1 is not None and r["m"] == "US":
                        v = self.e1.get(t, {}).get(i)
                        if v is not None and not v:
                            continue
                    if self.min_score is not None and int(r["score"][i]) <= self.min_score:
                        continue
                    if self.fcut is not None and "E" in self.fcut_kinds:
                        p = self.fcut_tab.get((t, i))
                        if p is not None and p == p and p < self.fcut:
                            continue
                    key = -float(r["mom"][i]) if self.rank == "mom" else self.rank_key(r, t, i, day, run)
                    base.append((key, r["order"], kind, t, i))
                    seen.add(t)
                elif kind == "CRY":
                    base.append(((-int(r["score"][i]),) if self.rank != "mom" else -int(r["score"][i]),
                                 r["order"], kind, t, i))
                    seen.add(t)
                elif kind == "D3" and self.d3:
                    if self.fcut is not None and "D3" in self.fcut_kinds:
                        p = self.fcut_tab.get((t, i))
                        if p is not None and p == p and p < self.fcut:
                            continue
                    extra.append((self.rank_key(r, t, i, day, run) if self.rank != "mom" else -int(r["score"][i]),
                                  r["order"], kind, t, i))
        base.sort()
        extra = sorted(x for x in extra if x[3] not in seen)
        lst = [(k, t, i) for _, _, k, t, i in base + extra]
        return lst if self.rank == "mom" else lst[:20]

    def go(self):
        day = self.start
        while day <= self.end:
            wd = pd.Timestamp(np.datetime64(day, "D")).weekday()
            if day in self.flows:
                self.cash["USD"] += self.flows[day] / self.rate("USD", day)
            if self.btc is not None:
                self.btc_daily(day)
            if wd < 5:
                self.dividends(day, ("JP", "HK", "EU", "CRYPTO"))
                self.fill_market(day, ("CRYPTO", "JP", "HK", "EU"))
                if self.btc is not None:
                    self.btc_step(day)
                if not self.one_run:
                    self.run(day, "0900")
                self.dividends(day, ("US",))
                self.fill_market(day, ("US",))
                self.run(day, "2335")
            day += 1
        return self

    # ---- results
    def result(self):
        curve = pd.Series({pd.Timestamp(np.datetime64(d, "D")): v for d, v, _, _ in self.curve})
        npos = pd.Series({pd.Timestamp(np.datetime64(d, "D")): n for d, _, n, _ in self.curve})
        cashs = pd.Series({pd.Timestamp(np.datetime64(d, "D")): c for d, _, _, c in self.curve})
        tr = pd.DataFrame(self.trades)
        for c in ("signal_date", "fill_date", "exit_signal", "exit_date"):
            if c in tr:
                tr[c] = pd.to_datetime(tr[c].astype("int64"), unit="D")
        openp = []
        for t, p in self.pos.items():
            r = self.T[t]
            i = self.last_idx(r, self.end)
            rt = self.rate(r["ccy"], self.end)
            val = p["sa"] * r["raw_c"][i] * rt
            rec = dict(p["rec"]); rec.update(value_hkd=val, unreal_hkd=val + p["div_hkd"] - rec["cost_hkd"],
                                            stop_now=p["stop"], pending_exit=bool(p["exit"]))
            openp.append(rec)
        op = pd.DataFrame(openp)
        if len(op):
            for c in ("signal_date", "fill_date"):
                op[c] = pd.to_datetime(op[c].astype("int64"), unit="D")
        out = self._result_dict(curve, npos, cashs, tr, op)
        if self.park_bot:
            out["park_y"], out["park_led"], out["park_bal_end"] = self.park_y, self.park_led, self.park_bal
        if self.stop_mode is not None:
            out["stop_log"] = self.stop_log
        return out

    def _result_dict(self, curve, npos, cashs, tr, op):
        return {"curve": curve, "npos": npos, "cash": cashs, "costs_y": self.costs_y, "trades": tr, "open": op, "skips": self.skips, "costs": self.costs,
                "fx_count": self.fx_count, "reduced_days": len(self.reduced_days), "mkt_days": self.mkt_days, "killed_days": [str(pd.Timestamp(np.datetime64(d, "D")).date()) for d in self.killed_days],
                "events": [(str(pd.Timestamp(np.datetime64(d, "D")).date()), e, nl, pk) for d, e, nl, pk in self.events],
                "cash_end": dict(self.cash), "cfg": self.cfg, "overflow_runs": self.overflow_runs, "cand_runs": self.cand_runs, "park": dict(self.park_log) if self.park else None,
                "btc": dict(self.btc_log, end_units=self.btc_units, end_inv=self.btc_inv) if self.btc else None,
                "loss_log": dict(self.loss_log)}


def simulate(**cfg):
    return Sim(cfg).go().result()
