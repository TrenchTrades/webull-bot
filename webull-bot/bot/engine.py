"""The trading loop: housekeeping (stops, risk checks) + one decision per trading day."""
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from . import risk, strategy
from .broker import BrokerError
from .notify import notify

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class Engine:
    def __init__(self, settings, broker, state, price_fn):
        self.s = settings
        self.broker = broker
        self.state = state
        self.price_fn = price_fn   # symbols -> DataFrame of daily closes
        self.closes = None

    # ------------------------------------------------------------------ helpers
    def _say(self, msg):
        notify(self.s.discord_webhook, f"[{self.s.mode.upper()}] {msg}")

    def killed(self) -> bool:
        return self.s.kill_file.exists() or self.state.data.get("halted", False)

    def _sizing_equity(self, equity: float) -> float:
        return min(equity, self.s.bot_capital) if self.s.bot_capital > 0 else equity

    def refresh_prices(self):
        self.closes = self.price_fn(self.s.symbols)
        latest = {s: float(self.closes[s].dropna().iloc[-1]) for s in self.s.symbols}
        return latest

    # ------------------------------------------------------------- housekeeping
    def housekeeping(self, now: datetime) -> dict:
        """Update prices, track equity, enforce risk limits, keep a stop order on every position."""
        today = now.date().isoformat()
        prices = self.refresh_prices()
        for sym in self.broker.update_prices(prices):  # paper broker simulates stop fills
            self.state.sym(sym).update(stopped_out_on=today, stop_order_id=None)
            self.state.log_trade(self.s.mode, sym, "SELL", 0, prices[sym], "stop", "paper stop triggered")
            self._say(f"STOP-LOSS hit on {sym} @ {prices[sym]:.2f}")

        acct = self.broker.account()
        positions = self.broker.positions()
        open_orders = self.broker.open_orders()
        d = self.state.data

        if d.get("day") != today:
            d["day"], d["day_start_equity"] = today, acct.equity
        d["peak_equity"] = max(d.get("peak_equity") or 0.0, acct.equity)

        status = risk.check_limits(acct.equity, d["day_start_equity"], d["peak_equity"],
                                   self.s.daily_loss_limit, self.s.max_drawdown_halt, self.s.kill_file.exists())
        if not status.can_trade and not d.get("halted") and "drawdown" in status.reason:
            d["halted"], d["halt_reason"] = True, status.reason
            self._say(f"TRADING HALTED - {status.reason}. Stops stay in place. Run `python main.py resume` to restart.")

        self._reconcile_stops(positions, open_orders, prices, today)
        self.state.save()
        log.info("equity %.2f cash %.2f | positions %s | %s", acct.equity, acct.cash,
                 {s: p.qty for s, p in positions.items() if s in self.s.symbols} or "none",
                 status.reason or "risk OK")
        return {"account": acct, "positions": positions, "open_orders": open_orders,
                "prices": prices, "risk": status}

    def _reconcile_stops(self, positions, open_orders, prices, today):
        open_ids = {o.order_id for o in open_orders}
        for sym in self.s.symbols:
            st = self.state.sym(sym)
            pos = positions.get(sym)
            stop_id = st.get("stop_order_id")

            if not pos or pos.qty <= 0:
                if stop_id and stop_id not in open_ids:
                    # we had a stop, it's gone, and so is the position -> stop-loss filled
                    st.update(stopped_out_on=today, stop_order_id=None)
                    self.state.log_trade(self.s.mode, sym, "SELL", 0, prices.get(sym, 0), "stop", "stop filled at broker")
                    self._say(f"STOP-LOSS filled on {sym} (cooldown {self.s.cooldown_days} days)")
                elif stop_id:  # position gone but stop still open (shouldn't happen) -> clean up
                    self._safe_cancel(stop_id)
                    st["stop_order_id"] = None
                continue

            has_stop = stop_id in open_ids and st.get("stop_qty") == pos.qty
            if has_stop:
                continue
            if any(o.symbol == sym and o.side == "SELL" and o.order_id != stop_id for o in open_orders):
                continue  # a sell order is already working for this position
            # note: stops are placed even when the kill switch is on - they only reduce risk
            if stop_id and stop_id in open_ids:  # quantity changed -> replace
                self._safe_cancel(stop_id)
            cost = pos.avg_cost if pos.avg_cost > 0 else prices[sym]
            sp = risk.stop_price(cost, self.s.stop_loss_pct)
            try:
                oid = self.broker.place_stop(sym, pos.qty, sp)
                st.update(stop_order_id=oid, stop_price=sp, stop_qty=pos.qty)
                log.info("placed stop %s %d @ %.2f", sym, pos.qty, sp)
            except BrokerError as e:
                self._say(f"WARNING could not place stop for {sym}: {e}")

    def _safe_cancel(self, order_id):
        try:
            self.broker.cancel(order_id)
        except BrokerError as e:
            log.warning("cancel %s failed: %s", order_id, e)

    # ------------------------------------------------------------------ decide
    def decide(self, now: datetime) -> list:
        """Evaluate signals and trade. Called once per trading day at DECISION_TIME."""
        today = now.date()
        snap = self.housekeeping(now)
        d = self.state.data
        d["last_decision_day"] = today.isoformat()

        if self.closes is None or today not in set(self.closes.index.date):
            log.info("no bar for %s - market holiday? skipping decision", today)
            self.state.save()
            return []
        if self.killed():
            log.warning("kill switch / halt active - no trades today (%s)", d.get("halt_reason") or "KILL file")
            self.state.save()
            return []

        acct, positions, prices, status = snap["account"], snap["positions"], snap["prices"], snap["risk"]
        open_buys = {o.symbol for o in snap["open_orders"] if o.side == "BUY"}
        equity = self._sizing_equity(acct.equity)
        invested = sum(p.qty * prices.get(s, p.avg_cost) for s, p in positions.items() if s in self.s.symbols)
        cash = min(acct.cash, max(0.0, equity - invested))
        actions = []

        # sells first (frees cash, reduces risk)
        for sym in self.s.symbols:
            pos = positions.get(sym)
            sig = strategy.evaluate(sym, self.closes[sym], bool(pos and pos.qty > 0),
                                    self.s.sma_days, self.s.entry_band, self.s.exit_band)
            log.info("%s: %s (%s)", sym, sig.action, sig.reason)
            if sig.action == strategy.SELL and pos:
                st = self.state.sym(sym)
                if st.get("stop_order_id"):
                    self._safe_cancel(st["stop_order_id"])
                    st["stop_order_id"] = None
                limit = risk.sell_limit(prices[sym], self.s.limit_slippage)
                try:
                    self.broker.place_limit(sym, "SELL", pos.qty, limit)
                    st["last_order_day"] = today.isoformat()
                    self.state.log_trade(self.s.mode, sym, "SELL", pos.qty, limit, "signal", sig.reason)
                    self._say(f"SELL {pos.qty} {sym} limit {limit:.2f} - {sig.reason}")
                    actions.append(("SELL", sym, pos.qty, limit))
                    invested -= pos.qty * prices[sym]
                    cash += pos.qty * prices[sym]
                except BrokerError as e:
                    self._say(f"ERROR selling {sym}: {e} - re-placing stop next cycle")

        for sym in self.s.symbols:
            pos = positions.get(sym)
            if pos and pos.qty > 0:
                continue
            sig = strategy.evaluate(sym, self.closes[sym], False, self.s.sma_days, self.s.entry_band, self.s.exit_band)
            if sig.action != strategy.BUY:
                continue
            st = self.state.sym(sym)
            if not status.can_buy:
                log.info("%s buy signal skipped: %s", sym, status.reason)
                continue
            if risk.in_cooldown(st.get("stopped_out_on"), today, self.s.cooldown_days):
                log.info("%s buy signal skipped: cooldown after stop-out on %s", sym, st["stopped_out_on"])
                continue
            if sym in open_buys or st.get("last_order_day") == today.isoformat():
                log.info("%s buy skipped: already ordered today / open buy order", sym)
                continue
            qty = risk.shares_to_buy(equity, cash, invested, prices[sym], self.s.alloc_per_symbol, self.s.max_invested)
            if qty < 1:
                log.info("%s buy signal but budget too small for 1 share at %.2f", sym, prices[sym])
                continue
            limit = risk.buy_limit(prices[sym], self.s.limit_slippage)
            try:
                self.broker.place_limit(sym, "BUY", qty, limit)
                st["last_order_day"] = today.isoformat()
                self.state.log_trade(self.s.mode, sym, "BUY", qty, limit, "signal", sig.reason)
                self._say(f"BUY {qty} {sym} limit {limit:.2f} - {sig.reason}")
                actions.append(("BUY", sym, qty, limit))
                invested += qty * prices[sym]
                cash -= qty * limit
            except BrokerError as e:
                self._say(f"ERROR buying {sym}: {e}")

        # paper fills are instant, so protect new positions right away
        if actions:
            self._reconcile_stops(self.broker.positions(), self.broker.open_orders(), prices, today.isoformat())
        self.state.save()
        return actions

    # ------------------------------------------------------------------ flatten
    def flatten(self, now: datetime) -> None:
        """Emergency: cancel bot stops and sell every bot-managed position."""
        prices = self.refresh_prices()
        self.broker.update_prices(prices)
        for sym, pos in self.broker.positions().items():
            if sym not in self.s.symbols:
                continue
            st = self.state.sym(sym)
            if st.get("stop_order_id"):
                self._safe_cancel(st["stop_order_id"])
                st["stop_order_id"] = None
            limit = risk.sell_limit(prices[sym], self.s.limit_slippage * 2)
            self.broker.place_limit(sym, "SELL", pos.qty, limit)
            self.state.log_trade(self.s.mode, sym, "SELL", pos.qty, limit, "flatten", "manual flatten")
            self._say(f"FLATTEN: SELL {pos.qty} {sym} limit {limit:.2f}")
        self.state.save()
