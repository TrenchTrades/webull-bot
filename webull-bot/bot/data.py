"""Daily price data from Yahoo Finance (free). Webull's own market-data API needs a paid
OpenAPI data subscription, and daily signals don't need real-time quotes."""
import logging
import time

import pandas as pd

log = logging.getLogger(__name__)


def daily_closes(symbols: list[str], period: str = "2y", retries: int = 3) -> pd.DataFrame:
    """DataFrame indexed by date, one column of split/dividend-adjusted closes per symbol.
    During market hours the last row is today's in-progress bar (latest price)."""
    import yfinance as yf

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            df = yf.download(symbols, period=period, interval="1d", auto_adjust=True,
                             progress=False, threads=False)
            if df is None or df.empty:
                raise RuntimeError("empty price data")
            closes = df["Close"]
            if isinstance(closes, pd.Series):  # single symbol
                closes = closes.to_frame(symbols[0])
            closes.index = pd.to_datetime(closes.index).tz_localize(None)
            missing = [s for s in symbols if s not in closes.columns or closes[s].dropna().empty]
            if missing:
                raise RuntimeError(f"no data for {missing}")
            return closes
        except Exception as e:  # network hiccups are common; retry
            last_err = e
            log.warning("price download failed (attempt %d/%d): %s", attempt, retries, e)
            time.sleep(5 * attempt)
    raise RuntimeError(f"could not download prices: {last_err}")


def latest_prices(closes: pd.DataFrame) -> dict[str, float]:
    return {s: float(closes[s].dropna().iloc[-1]) for s in closes.columns}


def has_bar_for(closes: pd.DataFrame, day) -> bool:
    """True if Yahoo has a bar dated `day` - i.e. the market traded that day (holiday check)."""
    return pd.Timestamp(day) in set(closes.index.normalize())
