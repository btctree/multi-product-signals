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

# ---- LIVE PRODUCT "P1 + SMA200" (locked 2026-07-05) ----
# Two sleeves, monthly-rebalanced:
#   DIP 70%: C1 balanced equity engine (5 slots, US/HK/JP/EU) + per-product
#            SMA200 regime exit — dip entry, let-winners-run trailing exit.
#   CRY 30%: crypto trend engine (2 slots, BTC/ETH).
# Validated 11.2y (net): win 51.7%, CAGR 29.8%, 150k -> 2.78M, maxDD -24.3%,
# Calmar 1.16; positive every year except 2018 (-6%) and 2022 (-5%).
SLEEVES = {
    "DIP": {"weight": 0.70, "slots": 5, "markets": ("US", "HK", "JP", "EU")},
    "CRY": {"weight": 0.30, "slots": 2, "markets": ("CRYPTO",)},
}

# Live equity-engine (DIP sleeve) parameters — the C1 balanced config + SMA200 exit.
PROD = {
    "sma_trend": 200, "sma_fast": 50, "rsi_period": 3, "atr_period": 14,
    "rsi_entry": 25,          # buy a short-term dip (RSI3<25) inside an uptrend
    "near_high": 0.88,        # only strong stocks (within 12% of 52-wk high)
    "min_mom": 0.0,           # positive 90-day momentum
    "min_atr_pct": 0.012,     # move must clear costs
    "K": 3.5,                 # chandelier trail (ATR mult), wide early
    "K_tight": 2.0,           # tightens once +1.5 ATR in profit
    "tighten_at": 1.5,
    "regime_exit_sma": 200,   # exit if close falls below its own 200-day SMA
    "target_atr_mult": 3.0,   # informational target for display (exit is trailing)
    "hard_stop_pct": 0.12,
}
# Crypto-engine (CRY sleeve): long while uptrend, exit on 2 closes < SMA50 or trail.
CRY_TREND = {"sma_trend": 200, "sma_fast": 50, "trail_atr_mult": 4.0, "exit_consec": 2}
MAX_POSITIONS = 7             # 5 + 2 (mandate max 15 respected)
START_CAPITAL_HKD = 150_000
# Dynamic sizing per sleeve: new position = sleeve_cash / (sleeve_slots - held).
SIZING = "dynamic"
POSITION_HKD = 10_000         # legacy fixed-stake mode (SIZING = "fixed")
TARGET_WIN_RATE = 0.70        # portfolio-level requirement
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
    "max_hold_days": 25,       # time stop (rarely binds)
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
