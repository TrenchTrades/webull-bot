"""Backtest the live rules on daily data from Yahoo Finance.

    python main.py backtest            # 2005 -> today with your .env settings
    python backtest.py 2015-01-01      # custom start date

Simplifications (be aware): trades fill at the day's close plus 0.05% slippage; stop-losses fill at
the stop price, or at the open if the market gaps below it. No dividends on cash, no taxes.
"""
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from bot import risk, strategy
from bot.config import load_settings

SLIP = 0.0005


@dataclass
class Result:
    equity: pd.Series
    trades: list = field(default_factory=list)
    halt_days: list = field(default_factory=list)
    exposure: float = 0.0


def run_backtest(ohlc: dict, s, start_cash: float = 10000.0) -> Result:
    """ohlc: {symbol: DataFrame[Open, Low, Close]} on a shared date index."""
    syms = list(ohlc)
    idx = ohlc[syms[0]].index
    closes = pd.DataFrame({x: ohlc[x]["Close"] for x in syms})
    cash, pos = start_cash, {}          # pos: sym -> {qty, cost, stop}
    stopped_on, peak, curve, trades, halts, invested_days = {}, start_cash, [], [], [], 0

    for i, day in enumerate(idx):
        # 1) stops trigger intraday
        for x in list(pos):
            bar = ohlc[x].loc[day]
            p = pos[x]
            if bar["Low"] <= p["stop"]:
                fill = min(bar["Open"], p["stop"]) * (1 - SLIP)
                cash += p["qty"] * fill
                trades.append((day, x, "STOP", p["qty"], fill))
                stopped_on[x] = day.date().isoformat()
                del pos[x]

        px = closes.loc[day]
        equity = cash + sum(p["qty"] * px[x] for x, p in pos.items())
        day_start = curve[-1] if curve else equity
        peak = max(peak, equity)
        status = risk.check_limits(equity, day_start, peak, s.daily_loss_limit, s.max_drawdown_halt, False)
        if not status.can_trade:
            halts.append(day)

        # 2) signals at the close (backtest keeps trading after a halt so you can see what it would do)
        hist = closes.iloc[: i + 1]
        for x in list(pos):
            sig = strategy.evaluate(x, hist[x], True, s.sma_days, s.entry_band, s.exit_band)
            if sig.action == strategy.SELL:
                fill = px[x] * (1 - SLIP)
                cash += pos[x]["qty"] * fill
                trades.append((day, x, "SELL", pos[x]["qty"], fill))
                del pos[x]
        if status.can_buy:
            invested = sum(p["qty"] * px[x] for x, p in pos.items())
            for x in syms:
                if x in pos or np.isnan(px[x]):
                    continue
                if risk.in_cooldown(stopped_on.get(x), day.date(), s.cooldown_days):
                    continue
                sig = strategy.evaluate(x, hist[x], False, s.sma_days, s.entry_band, s.exit_band)
                if sig.action != strategy.BUY:
                    continue
                qty = risk.shares_to_buy(equity, cash, invested, px[x], s.alloc_per_symbol, s.max_invested)
                if qty < 1:
                    continue
                fill = px[x] * (1 + SLIP)
                cash -= qty * fill
                invested += qty * px[x]
                pos[x] = {"qty": qty, "cost": fill, "stop": risk.stop_price(fill, s.stop_loss_pct)}
                trades.append((day, x, "BUY", qty, fill))

        if pos:
            invested_days += 1
        curve.append(cash + sum(p["qty"] * px[x] for x, p in pos.items()))

    return Result(pd.Series(curve, index=idx), trades, halts, invested_days / max(1, len(idx)))


def stats(curve: pd.Series) -> dict:
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1 if years > 0 else 0
    dd = (curve / curve.cummax() - 1).min()
    rets = curve.pct_change().dropna()
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
    return {"CAGR": cagr, "MaxDD": dd, "Sharpe": sharpe, "Final": curve.iloc[-1]}


def load_ohlc(symbols, start):
    import yfinance as yf
    df = yf.download(symbols, start=start, auto_adjust=True, progress=False, threads=False)
    df = df.dropna()
    return {x: pd.DataFrame({"Open": df["Open"][x], "Low": df["Low"][x], "Close": df["Close"][x]}) for x in symbols}


def main():
    args = [a for a in sys.argv[1:] if a != "backtest"]
    start = args[0] if args else "2005-01-01"
    s = load_settings()
    print(f"Downloading {', '.join(s.symbols)} from {start}...")
    ohlc = load_ohlc(s.symbols, start)
    res = run_backtest(ohlc, s)
    # benchmarks over the period the strategy was actually able to trade (after the SMA warm-up)
    warm = res.equity.index[s.sma_days]
    bot = stats(res.equity.loc[warm:])
    spy = ohlc["SPY"]["Close"].loc[warm:] if "SPY" in ohlc else None

    print(f"\nPeriod: {warm.date()} -> {res.equity.index[-1].date()}  (after {s.sma_days}-day warm-up)")
    print(f"{'':22}{'CAGR':>8}{'Max DD':>9}{'Sharpe':>8}")
    print(f"{'Bot':22}{bot['CAGR']:>8.1%}{bot['MaxDD']:>9.1%}{bot['Sharpe']:>8.2f}")
    if spy is not None:
        b = stats(spy / spy.iloc[0] * 10000)
        print(f"{'SPY buy & hold':22}{b['CAGR']:>8.1%}{b['MaxDD']:>9.1%}{b['Sharpe']:>8.2f}")
        mix = sum(ohlc[x]["Close"].loc[warm:] / ohlc[x]["Close"].loc[warm] for x in s.symbols) / len(s.symbols)
        mix = s.max_invested * mix + (1 - s.max_invested)
        m = stats(mix * 10000)
        print(f"{'Same mix, never sells':22}{m['CAGR']:>8.1%}{m['MaxDD']:>9.1%}{m['Sharpe']:>8.2f}")
    n_stop = sum(1 for t in res.trades if t[2] == "STOP")
    print(f"\nTrades: {len(res.trades)} ({n_stop} stop-outs)   Time invested: {res.exposure:.0%}")
    if res.halt_days:
        print(f"Drawdown halt would have triggered on {res.halt_days[0].date()} (live bot would stop and alert you)")
    print("Past results don't guarantee future returns.")


if __name__ == "__main__":
    main()
