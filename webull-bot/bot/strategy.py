"""Trend-following rules. Pure functions so they're easy to test and backtest.

Rule per symbol (daily closes):
  * Trend is UP   when close > SMA(n) * (1 + entry_band)  -> want to hold
  * Trend is DOWN when close < SMA(n) * (1 - exit_band)   -> want to be flat
  * In between (inside the band) -> keep whatever we have (reduces whipsaw trades)
"""
from dataclasses import dataclass

import pandas as pd

HOLD, BUY, SELL = "HOLD", "BUY", "SELL"


@dataclass
class Signal:
    symbol: str
    action: str          # BUY / SELL / HOLD
    close: float
    sma: float
    reason: str


def sma(closes: pd.Series, days: int) -> pd.Series:
    return closes.rolling(days, min_periods=days).mean()


def evaluate(symbol: str, closes: pd.Series, holding: bool, sma_days: int,
             entry_band: float, exit_band: float) -> Signal:
    closes = closes.dropna()
    if len(closes) < sma_days:
        return Signal(symbol, HOLD, float(closes.iloc[-1]) if len(closes) else 0.0, float("nan"),
                      f"not enough history ({len(closes)}/{sma_days} days)")
    avg = float(sma(closes, sma_days).iloc[-1])
    close = float(closes.iloc[-1])
    upper, lower = avg * (1 + entry_band), avg * (1 - exit_band)

    if not holding and close > upper:
        return Signal(symbol, BUY, close, avg, f"close {close:.2f} > SMA{sma_days}+band {upper:.2f}")
    if holding and close < lower:
        return Signal(symbol, SELL, close, avg, f"close {close:.2f} < SMA{sma_days}-band {lower:.2f}")
    state = "holding" if holding else "flat"
    return Signal(symbol, HOLD, close, avg, f"{state}; close {close:.2f}, SMA{sma_days} {avg:.2f}")
