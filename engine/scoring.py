"""Action score (0-100) — ranks today's BUY candidates by expected
return-per-risk. Weights per the role-review design (2026-07-09):
  Trend 40  : 90-day momentum, full points at +30% (the validated engine's
              own ranking factor - keeps ordering anchored to the backtest)
  Entry 20  : dip depth, RSI(3) below 25 (deeper oversold = better entry)
  Strength 15: proximity to 52-week high (within the 12% quality gate)
  Risk  25  : tightness of the effective cut-loss (4% -> full, 12% -> zero)
Adopted for LIVE ordering only after passing the backtest gate (win/CAGR/DD
vs momentum-ranking must hold; see research_score_gate.py output in RESULTS).
"""


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def composite(mom, rsi, off_high_pct, stop_pct, trend_ride=False):
    """All inputs in natural units: mom=0.32 (=+32%), rsi=14,
    off_high_pct=-5.0 (5% below 52w high), stop_pct=7.5 (stop 7.5% below).
    trend_ride=True (crypto trend sleeve): dip-entry not applicable -> neutral.
    Returns (total 0-100 int, parts dict)."""
    trend = clamp(mom / 0.30) * 40
    entry = 10.0 if trend_ride else clamp((25 - rsi) / 25) * 20
    strength = clamp(1 + off_high_pct / 12.0) * 15
    risk = clamp((12.0 - stop_pct) / 8.0) * 25
    parts = {"trend": round(trend), "entry": round(entry),
             "strength": round(strength), "risk": round(risk)}
    return int(round(trend + entry + strength + risk)), parts


def band(score):
    return ("Strong" if score >= 65 else
            "Moderate" if score >= 50 else "Marginal")


# GATE RESULT 2026-07-09 (research_score_gate.py): using the composite above to
# ORDER candidates collapsed the 11y backtest (CAGR 26.7% -> 5.1%, win 54.5% ->
# 49.6%) - the risk/entry/strength weights out-vote momentum and pick weak dips.
# REJECTED for ordering. The LIVE score is therefore the validated ranking
# factor alone: trend momentum, scaled 0-100 (full points at +50% / 90 days).
def momentum_score(mom):
    return int(round(clamp(mom / 0.50) * 100))
