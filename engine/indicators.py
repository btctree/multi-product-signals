"""Indicator library. Pure functions of past data only — callers are responsible
for the act-at-next-open discipline (no look-ahead)."""
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def roc(close: pd.Series, n: int) -> pd.Series:
    return close.pct_change(n)


def add_features(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Attach all strategy features. Everything is computed from data up to and
    including each row's own close — signals must therefore be executed at the
    NEXT bar's open (handled by the backtester)."""
    out = df.copy()
    c = out["Close"]
    out["sma_trend"] = sma(c, p["sma_trend"])
    out["sma_fast"] = sma(c, p["sma_fast"])
    out["rsi"] = rsi(c, p["rsi_period"])
    out["atr"] = atr(out, p["atr_period"])
    out["mom_90"] = roc(c, 90)          # ranking score: 90-day momentum
    out["vol20"] = out["Volume"].rolling(20).mean()
    out["vol_surge"] = out["Volume"] / out["vol20"]
    out["hi_52w"] = c.rolling(252).max()
    out["prev_high"] = out["High"].shift(1)
    out["sma_250"] = sma(c, 250)
    out["sma200_slope"] = out["sma_trend"] - out["sma_trend"].shift(21)   # ~1mo slope
    out["sma50_slope"] = out["sma_fast"] - out["sma_fast"].shift(10)
    out["ext_atr"] = (c - out["sma_trend"]) / out["atr"]   # extension above SMA200 in ATRs
    return out
