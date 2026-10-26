"""Central configuration for the Multi-Product Strategy Engine.

Honesty rules (from METHODOLOGY.md) apply everywhere:
- no look-ahead: decide on close(t), act at open(t+1)
- costs charged on every fill
- report net-of-cost numbers only
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
UNIVERSE_FILE = DATA_DIR / "universe.json"

# ---- LIVE PRODUCT "D" = P1 + SMA200 + score>60 gate + 15 positions ----
# (user-selected 2026-07-11 after the 4-way re-validation on the full 998-pool)
# Two sleeves, monthly-rebalanced:
#   DIP 70%: equity dip engine, 13 slots, US/HK/JP/EU; ENTRY REQUIRES SCORE>60
#            (= 90d momentum >= +30%); SMA200 regime exit; chandelier trail.
#   CRY 30%: crypto trend engine (2 slots, BTC/ETH).
# Validated 11.2y net (data/revalidation.json, tag D): win 52.5%, CAGR 30.8%,
# 150k -> 3.04M, maxDD -29.0% (gate PASS), Sharpe 0.88, ~131 trades/yr.
# Alternatives measured: original pool A (CAGR 30.5%, DD -26.4%), full-pool
# B/C breach the 30% DD gate. Survivorship caveat documented on dashboard.
# Since 2026-10-26 (owner decisions) the stock signals are MODEL "D3E1" below:
# these D dip rules, with an earnings filter on US dips, plus a second entry
# signal ranked after every dip. The bot runs ONE 15-slot stock pool; the CRY
# sleeve stays a research figure only (crypto entries are blocked in the bot).
# The PROD values below are unchanged by that decision.
SLEEVES = {
    "DIP": {"weight": 0.70, "slots": 13, "markets": ("US", "HK", "JP", "EU")},
    "CRY": {"weight": 0.30, "slots": 2, "markets": ("CRYPTO",)},
}
SCORE_ENTRY_GATE = 60         # BUY only when momentum score > 60 (mom90 >= +30%)

# Live equity-engine (DIP sleeve) parameters — the C1 balanced config + SMA200 exit.
PROD = {
    "sma_trend": 200, "sma_fast": 50, "rsi_period": 3, "atr_period": 14,
    "rsi_entry": 25,          # buy a short-term dip (RSI3<25) inside an uptrend
    "near_high": 0.88,        # only strong stocks (within 12% of 52-wk high)
    "min_mom": 0.30,          # D config: score>60 gate = 90d momentum >= +30%
    "min_atr_pct": 0.012,     # move must clear costs
    "K": 3.5,                 # chandelier trail (ATR mult), wide early
    "K_tight": 2.0,           # tightens once +1.5 ATR in profit
    "tighten_at": 1.5,
    "regime_exit_sma": 200,   # exit if close falls below its own 200-day SMA
    "target_atr_mult": 3.0,   # informational target for display (exit is trailing)
    "hard_stop_pct": 0.12,
}
# ---- Stock model: which entry signals the build publishes (owner, 2026-10-26) ----
# "D3E1": the PROD dip above (signal "E", tier 0) with E1 on US names, plus D3
#         BUY cards (tier 1) that rank after every E row. Exits are unchanged.
# "E"   : the PROD dip alone, every card exactly as before - the one-line
#         rollback (commit MODEL = "E"; Pages rebuilds within minutes).
# Research behind both (2026-10-24..26, kept outside the repo): D3 was a
# "lead only" in its own study and E1 failed its bootstrap; together the
# live-faithful simulation gained about +2.5-3pp a year over E, not the
# research gap.
MODEL = "D3E1"
# D3 = a pullback that held the 0.618 retracement of a recent swing, inside
# the same trend the dip needs. Swing high = highest High of the last 40 bars
# (the first on ties), swing low = lowest Low of the 120 bars up to that high,
# and the swing must be >= 10%. Signal bar: the 5-bar low touched the 0.618
# level (within 1%) without breaking the 0.886 floor, and the close is back
# above the level and above the previous close. Trend: close > SMA200,
# SMA50 > SMA200, 90-bar momentum > 30%, ATR(14) > 1.2% of the close (per-market
# floor from MIN_ATR_BY_MARKET). These are the research definition
# (ideas/features.py fib + fib_hold), matched bar for bar by model_d3e1.py.
D3 = {"look_hi": 40, "look_lo": 120, "min_swing": 0.10, "level": 0.618,
      "floor": 0.886, "tol": 0.01, "look": 5, "min_mom": 0.30,
      "min_atr_pct": 0.012, "markets": ("US", "HK", "JP", "EU")}
# E1 = keep a US dip only if trailing-12-month EPS (SEC EDGAR, diluted, basic
# when diluted has fewer than min_rows usable rows) grew against the 12 months
# before, as known on the signal day. A name that cannot be scored - no SEC
# data, irregular quarters, or a newest quarter older than max_age_days - is
# KEPT, i.e. plain E. Facts come from engine/sec_fetch.py (its own CI step).
E1 = {"max_age_days": 200, "min_rows": 8}
# SEC EDGAR fair-access limits (10 requests/s per their policy): one request
# every spacing_s, each timed out at timeout_s and retried once, and the whole
# fetch stopped after budget_s so the CI step ends well inside its 3 minutes.
SEC = {"spacing_s": 0.15, "timeout_s": 10, "budget_s": 75}
# Crypto-engine (CRY sleeve): long while uptrend, exit on 2 closes < SMA50 or trail.
CRY_TREND = {"sma_trend": 200, "sma_fast": 50, "trail_atr_mult": 4.0, "exit_consec": 2}
MAX_POSITIONS = 15            # 13 + 2 (the agreed system-wide cap)
START_CAPITAL_HKD = 150_000
# Dynamic sizing per sleeve: new position = sleeve_cash / (sleeve_slots - held).
SIZING = "dynamic"
POSITION_HKD = 10_000         # legacy fixed-stake mode (SIZING = "fixed")
TARGET_WIN_RATE = 0.70        # ASPIRATIONAL ONLY — the shipped product (D)
                              # measures 52.5%; this gate is not enforced
                              # anywhere in the live path (see
                              # SYSTEMS_OVERFIT_REVIEW.md on its renegotiation)
MAX_DRAWDOWN = 0.30           # portfolio-level requirement
LONG_ONLY = True

# ---- Costs (charged per side, on notional) ----
# stocks/index-ETF proxies: commission + spread; crypto: exchange taker; futures proxies wider
COST_BP = {
    "US": 10,      # 0.10% per side (commission+slippage, generous for top-50 caps)
    "HK": 25,      # HK stamp duty 0.13% + commission + spread
    "JP": 15,
    "INDEX": 10,
    "COMMODITY": 15,
    "CRYPTO": 20,
    "LEV": 12,     # leveraged ETFs: liquid, slightly wider spread + expense drag
    "BOND": 8,     # bond ETFs: very liquid, tight spreads
    "EU": 15,      # EU large caps: commission + spread (no stamp except a few)
    "MACRO": 10,   # copper/USD-index ETFs (CPER/UUP): liquid
    "FX": 3,       # FX majors: very tight spreads (~1-2 pip)
    "ETF": 10,     # broad/sector/commodity ETFs: liquid
}

# Per-market minimum ATR% floor for entries. FX daily vol is ~5-8x lower than
# equities, so it needs its own floor or the equity 1.2% floor excludes all FX.
MIN_ATR_BY_MARKET = {"FX": 0.003}   # 0.3% for FX; others use STRAT["min_atr_pct"]

# ---- Backtest window ----
BACKTEST_YEARS = 10

# ---- DIP sleeve parameters (= X4 dip engine, validated rounds 5-6) ----
# Standalone: win 68.4%, PF 1.36, CAGR 16.7%, maxDD -23.2% at 7 slots;
# runs at 5 slots inside R2. Round 7-8 additions: shorts REJECTED (mirror-dip
# shorts lose -122k..-184k even bear-gated); margin on X4 caps at m=1.25
# (18.3%, DD -28.8%) before DD gate breaks; options REJECTED (see RESULTS.md).
STRAT = {
    "sma_trend": 200,          # regime: close > SMA200
    "sma_fast": 50,            # and SMA50 > SMA200 (established uptrend)
    "rsi_period": 3,
    "rsi_entry": 15,           # deep oversold pullback inside uptrend
    "rsi_exit": 70,            # backup signal exit if target not yet hit
    "atr_period": 14,
    "stop_atr_mult": 3.5,      # catastrophe stop distance in ATRs
    "hard_stop_pct": 0.10,     # never risk more than 10% of a position
    "target_atr_mult": 2.0,    # profit target: entry + 2.0 ATR (resting GTC order)
    "tp_exit": True,           # target fills intraday
    "max_hold_days": 25,       # LEGACY X4 value — unused by the live bot,
                               # which uses MAX_HOLD_BARS=60 (ib_bot.py), the
                               # validated engine_rr default restored 2026-08-01
    "min_price": 1.0,
    "min_mom": 0.0,            # require positive 90-day momentum
    "min_atr_pct": 0.012,      # expected move must clear round-trip costs
    # monitor-only: futures-roll artifacts (COMMODITY), untradeable (INDEX),
    # rejected sleeves (LEV round 4/8, BOND round 8 - bonds stay monitored),
    # crypto handled by its own trend sleeve
    "exclude_markets": ["COMMODITY", "INDEX", "LEV", "BOND", "CRYPTO"],
}

# ---- CRY sleeve parameters (crypto trend engine, validated rounds 7-8) ----
# Standalone: win 45.2%, CAGR 43.7%, maxDD -61.2% (BTC/ETH, 2 slots).
# Long while close > SMA200 and SMA50 > SMA200; exit on 2 consecutive closes
# below SMA50 or chandelier 4.0*ATR ratchet trail; fills at next open.
CRY_STRAT = {
    "sma_trend": 200,
    "sma_fast": 50,
    "trail_atr_mult": 4.0,
    "exit_consecutive": 2,
}
