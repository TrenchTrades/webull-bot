import tempfile
import unittest
from datetime import datetime, date
from pathlib import Path

import numpy as np
import pandas as pd

from bot import risk, strategy
from bot.broker import PaperBroker, parse_account, parse_positions, parse_orders, BrokerError
from bot.config import Settings, validate
from bot.engine import Engine, ET
from bot.state import State

SYMS = ["SPY", "QQQ", "GLD"]


def series(values, end="2026-09-28"):
    idx = pd.bdate_range(end=end, periods=len(values))
    return pd.Series(values, index=idx, dtype=float)


def trend_frame(slopes, n=260, end="2026-09-28", start=100.0):
    return pd.DataFrame({s: series(start * np.cumprod(np.full(n, 1 + k)), end) for s, k in zip(SYMS, slopes)})


class StrategyTests(unittest.TestCase):
    def test_buy_in_uptrend(self):
        c = series(np.linspace(100, 150, 250))
        self.assertEqual(strategy.evaluate("SPY", c, False, 200, 0.01, 0.01).action, strategy.BUY)

    def test_hold_when_already_long(self):
        c = series(np.linspace(100, 150, 250))
        self.assertEqual(strategy.evaluate("SPY", c, True, 200, 0.01, 0.01).action, strategy.HOLD)

    def test_sell_in_downtrend(self):
        c = series(np.linspace(150, 100, 250))
        self.assertEqual(strategy.evaluate("SPY", c, True, 200, 0.01, 0.01).action, strategy.SELL)

    def test_band_prevents_whipsaw(self):
        c = series([100.0] * 249 + [100.5])  # 0.5% above SMA, inside 1% band
        self.assertEqual(strategy.evaluate("SPY", c, False, 200, 0.01, 0.01).action, strategy.HOLD)

    def test_not_enough_history(self):
        sig = strategy.evaluate("SPY", series(np.linspace(1, 2, 50)), False, 200, 0.01, 0.01)
        self.assertEqual(sig.action, strategy.HOLD)


class RiskTests(unittest.TestCase):
    def test_sizing_caps(self):
        # 25% of 10k = 2500 -> 4 shares at 600
        self.assertEqual(risk.shares_to_buy(10000, 10000, 0, 600, 0.25, 0.75), 4)
        # total cap: already 7000 invested of 10k at 75% cap -> 500 left -> 0 shares at 600
        self.assertEqual(risk.shares_to_buy(10000, 3000, 7000, 600, 0.25, 0.75), 0)
        # cash cap
        self.assertEqual(risk.shares_to_buy(10000, 1000, 0, 300, 0.25, 0.75), 3)

    def test_limits(self):
        self.assertTrue(risk.check_limits(10000, 10000, 10000, 0.03, 0.15, False).can_buy)
        daily = risk.check_limits(9600, 10000, 10000, 0.03, 0.15, False)
        self.assertFalse(daily.can_buy)
        self.assertTrue(daily.can_trade)
        dd = risk.check_limits(8400, 8400, 10000, 0.03, 0.15, False)
        self.assertFalse(dd.can_trade)
        self.assertFalse(risk.check_limits(10000, 10000, 10000, 0.03, 0.15, True).can_trade)

    def test_prices(self):
        self.assertEqual(risk.stop_price(100, 0.08), 92.0)
        self.assertEqual(risk.buy_limit(100, 0.002), 100.2)
        self.assertEqual(risk.sell_limit(100, 0.002), 99.8)

    def test_cooldown(self):
        self.assertTrue(risk.in_cooldown("2026-09-20", date(2026, 9, 25), 10))
        self.assertFalse(risk.in_cooldown("2026-09-01", date(2026, 9, 25), 10))
        self.assertFalse(risk.in_cooldown(None, date(2026, 9, 25), 10))


class ConfigTests(unittest.TestCase):
    def test_live_needs_confirmation(self):
        with self.assertRaises(ValueError):
            validate(Settings(mode="live", app_key="k", app_secret="s"))
        validate(Settings(mode="live", app_key="k", app_secret="s", confirm_live=True))

    def test_no_margin(self):
        with self.assertRaises(ValueError):
            validate(Settings(max_invested=1.5))


class ParseTests(unittest.TestCase):
    def test_parse_account_nested(self):
        raw = {"account_id": "A1", "total_asset": "12500.50",
               "account_currency_assets": [{"currency": "USD", "cash_balance": "4000.25"}]}
        a = parse_account(raw)
        self.assertEqual((a.equity, a.cash), (12500.50, 4000.25))

    def test_parse_account_missing(self):
        with self.assertRaises(BrokerError):
            parse_account({"foo": 1})

    def test_parse_positions(self):
        raw = {"holdings": [{"symbol": "spy", "quantity": "3", "cost_price": "600.10"},
                            {"symbol": "QQQ", "quantity": "0", "cost_price": "1"}]}
        p = parse_positions(raw)
        self.assertEqual(list(p), ["SPY"])
        self.assertEqual((p["SPY"].qty, p["SPY"].avg_cost), (3, 600.10))

    def test_parse_orders(self):
        raw = {"orders": [{"client_order_id": "x1", "symbol": "SPY", "side": "SELL",
                           "order_type": "STOP_LOSS", "status": "SUBMITTED"}]}
        o = parse_orders(raw)
        self.assertEqual((o[0].order_id, o[0].side), ("x1", "SELL"))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.s = Settings(data_dir=self.tmp, symbols=SYMS)
        self.frame = trend_frame([0.002, 0.002, 0.002])
        self.broker = PaperBroker(self.tmp, 10000)
        self.engine = Engine(self.s, self.broker, State(self.tmp), lambda syms: self.frame)
        self.now = datetime(2026, 9, 28, 15, 45, tzinfo=ET)

    def test_uptrend_buys_with_stops(self):
        actions = self.engine.decide(self.now)
        self.assertEqual({a[1] for a in actions if a[0] == "BUY"}, set(SYMS))
        acct = self.broker.account()
        invested = acct.equity - acct.cash
        self.assertLessEqual(invested, 0.75 * acct.equity + 1)
        self.assertEqual(len(self.broker.open_orders()), 3)  # one stop per position
        # running again the same day must not double-buy
        self.assertEqual(self.engine.decide(self.now), [])

    def test_stop_out_then_cooldown(self):
        self.engine.decide(self.now)
        crash = self.frame.copy()
        crash.loc[pd.Timestamp("2026-09-29")] = crash.iloc[-1] * 0.85
        self.frame = crash
        nxt = datetime(2026, 9, 29, 15, 45, tzinfo=ET)
        self.engine.housekeeping(nxt)
        self.assertEqual(self.broker.positions(), {})
        self.assertEqual(self.engine.state.sym("SPY")["stopped_out_on"], "2026-09-29")
        rebound = crash.copy()
        rebound.loc[pd.Timestamp("2026-09-30")] = self.frame.iloc[-2] * 1.05
        self.frame = rebound
        acts = self.engine.decide(datetime(2026, 9, 30, 15, 45, tzinfo=ET))
        self.assertFalse(any(a[0] == "BUY" for a in acts), "cooldown should block re-entry")

    def test_downtrend_sells(self):
        self.engine.decide(self.now)
        down = trend_frame([-0.004, 0.002, 0.002], n=261, end="2026-09-29", start=200)
        # keep the stop from firing so we test the signal exit: raise stop far below
        for oid in list(self.broker.book["stops"]):
            self.broker.book["stops"][oid]["stop_price"] = 1.0
        self.frame = down
        acts = self.engine.decide(datetime(2026, 9, 29, 15, 45, tzinfo=ET))
        self.assertIn(("SELL", "SPY"), {(a[0], a[1]) for a in acts})
        self.assertNotIn("SPY", self.broker.positions())
        self.assertEqual(sum(1 for o in self.broker.open_orders() if o.symbol == "SPY"), 0)

    def test_kill_switch_blocks_orders(self):
        self.s.kill_file.write_text("x")
        self.assertEqual(self.engine.decide(self.now), [])
        self.assertEqual(self.broker.positions(), {})

    def test_holiday_skips(self):
        acts = self.engine.decide(datetime(2026, 10, 3, 15, 45, tzinfo=ET))  # no bar that day
        self.assertEqual(acts, [])

    def test_bot_capital_cap(self):
        self.s.bot_capital = 2000
        self.engine.decide(self.now)
        acct = self.broker.account()
        self.assertLessEqual(acct.equity - acct.cash, 2000 * 0.75 + 1)

    def test_flatten(self):
        self.engine.decide(self.now)
        self.engine.flatten(self.now)
        self.assertEqual(self.broker.positions(), {})
        self.assertEqual(self.broker.open_orders(), [])


class BacktestTests(unittest.TestCase):
    def test_runs(self):
        from backtest import run_backtest, stats
        rng = np.random.default_rng(0)
        n = 1500
        ohlc = {}
        for sym in SYMS:
            close = series(100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n))))
            ohlc[sym] = pd.DataFrame({"Open": close.shift(1).fillna(100), "Low": close * 0.99, "Close": close})
        res = run_backtest(ohlc, Settings())
        self.assertEqual(len(res.equity), n)
        self.assertTrue(res.trades)
        st = stats(res.equity)
        self.assertGreater(st["Final"], 0)
        self.assertGreaterEqual(st["MaxDD"], -1)


if __name__ == "__main__":
    unittest.main()
