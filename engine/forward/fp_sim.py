"""The simulator in mode FP: IB's 5% hold on BUY-side conversions, and the bot's once-per-run sizing.

Ported from the FX review's fxsim.py (modes B and FP), without its event logging. With fp=False nothing differs from
live_sim.Sim; that is what the identity check runs. With fp=True:

  * the hold check: a conversion that BUYS the pair's base with a quote balance below cost x 1.05 + HK$16 is refused,
    no cash moves, and the next balance is tried (execution/ib_bot.py _fx_buys_pair_base, IB_FX_BUY_HOLD);
  * the clamp allows for that hold, so the order is cut and still goes out if a share or lot is left (_fx_payable_base);
  * the slot size is worked out once per run, after the exits and before the candidates (per_slot in ib_bot.py), not
    re-divided per candidate.

Pair direction: EUR > GBP > USD > HKD > JPY (_FX_BASE_ORDER); the BUY side is a target that is the pair's base.
"""
import inspect
import textwrap

import numpy as np
import pandas as pd

from . import live_sim as LS

HOLD, FEE_HKD = 1.05, 16.0
FX_ORDER = ("EUR", "GBP", "USD", "HKD", "JPY")


def buys_base(src, dst):
    """True when src -> dst BUYS the pair's base and pays in its quote. A currency outside the table counts as BUY side, as in the bot."""
    s, d = str(src).upper(), str(dst).upper()
    if s in FX_ORDER and d in FX_ORDER:
        return FX_ORDER.index(d) < FX_ORDER.index(s)
    return True


# The only edits to Sim._run_core. Taken from the frozen source at import time; each must match exactly once.
_PATCHES = [
    ('        rday = day - 1 if run == "0900" else day\n        for kind, t, i in cl:\n',
     '        rday = day - 1 if run == "0900" else day\n        self._per_slot0 = self._free_cash0(rday) / max(1, free)\n        for kind, t, i in cl:\n'),
    ('                notional = max(0.0, cash_free) / max(1, self.slots - len(self.pos) - len(self.pend) - btc_sl)\n',
     '                notional = max(0.0, cash_free) / max(1, self.slots - len(self.pos) - len(self.pend) - btc_sl)\n'
     '                if self.fp:\n'
     '                    notional = self._per_slot0\n'),
    ('                notional = min(notional, fundable * 0.994)\n',
     '                if self.fp:\n'
     '                    fundable = own + self._best_after_hold(ccy, rday) / (buf * 1.0002)\n'
     '                notional = min(notional, fundable * 0.994)\n'),
]


def _patched_run_core():
    src = inspect.getsource(LS.Sim._run_core).replace("\r\n", "\n")
    for old, new in _PATCHES:
        assert src.count(old) == 1, old
        src = src.replace(old, new)
    ns = {}
    exec(compile(textwrap.dedent(src), "patched_run_core", "exec"), vars(LS), ns)
    return ns["_run_core"]


class ForwardSim(LS.Sim):
    """cards: a card table to use instead of the real one (a random twin's)."""

    def __init__(self, cfg, fp=True, cards=None):
        super().__init__(cfg)
        assert self.sizing == "cashfree" and self.btc is None and not self.fractional and not self.jp1000 and not self.fx_pool
        self.fp = fp
        self._per_slot0 = 0.0
        if cards is not None:
            self.cards = cards

    _run_core = _patched_run_core()

    def _spend(self, c):
        return self.cash[c] - self.res[c] - (self.locked_hkd if c == "HKD" else 0.0)

    def _free_cash0(self, rday):
        """_free_cash_base: every currency net of reservations."""
        return max(0.0, sum(self._spend(cc) * self.rate(cc, rday) for cc in self.cash if cc != "HKD" or self.fx_convert))

    def _best_after_hold(self, ccy, rday):
        """The largest source after the hold: _fx_payable_base on a BUY-side pair; on a SELL-side pair what the simulator's
        clamp already takes (the balance less USD 2)."""
        usd2 = 2.0 * self.rate("USD", rday)
        best = 0.0
        for c_ in self.cash:
            if c_ == ccy or (c_ == "HKD" and not self.fx_convert):
                continue
            v = max(0.0, self._spend(c_)) * self.rate(c_, rday)
            best = max(best, (v - FEE_HKD) / HOLD if buys_base(c_, ccy) else v - usd2)
        return best

    def ensure(self, ccy, need, rday, day=None, run=None):
        """The simulator's ensure(), plus the hold check when fp is on."""
        avail = self._spend(ccy)
        if avail >= need:
            return True
        if ccy in self.fx_blocked:
            return False
        short = need - avail
        buf = 1.03 if ccy == "HKD" else 1.02
        srcs = [c for c in self.cash if c != ccy and (c != "HKD" or self.fx_convert) and self._spend(c) > 0]
        srcs.sort(key=lambda c: -self._spend(c) * self.rate(c, rday))
        for s in srcs:
            rs = self.rate(s, rday)
            need_src = short * buf * self.rate(ccy, rday) / rs
            fee_src = (2.0 * self.rate("USD", rday) + 0.0002 * short * buf * self.rate(ccy, rday)) / rs
            if self._spend(s) >= need_src + fee_src:
                if self.fp and buys_base(s, ccy) and self._spend(s) < need_src * HOLD + FEE_HKD / rs:
                    continue
                self.cash[s] -= need_src + fee_src
                if (self.fx_friday and run == "2335" and day is not None
                        and pd.Timestamp(np.datetime64(day, "D")).weekday() == 4):
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
