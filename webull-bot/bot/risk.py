"""Position sizing and safety checks. Pure functions, no I/O."""
import math
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class RiskStatus:
    can_buy: bool
    can_trade: bool
    reason: str = ""


def check_limits(equity: float, day_start_equity: float, peak_equity: float,
                 daily_loss_limit: float, max_drawdown_halt: float, killed: bool) -> RiskStatus:
    """Decide whether new buys / any trading are allowed right now."""
    if killed:
        return RiskStatus(False, False, "kill switch is ON")
    if peak_equity > 0 and equity < peak_equity * (1 - max_drawdown_halt):
        dd = 1 - equity / peak_equity
        return RiskStatus(False, False,
                          f"max drawdown halt: equity {equity:,.2f} is {dd:.1%} below peak {peak_equity:,.2f}")
    if day_start_equity > 0 and equity < day_start_equity * (1 - daily_loss_limit):
        loss = 1 - equity / day_start_equity
        return RiskStatus(False, True, f"daily loss limit hit ({loss:.1%} today) - no new buys")
    return RiskStatus(True, True)


def shares_to_buy(equity: float, cash: float, invested_value: float, price: float,
                  alloc_per_symbol: float, max_invested: float) -> int:
    """Whole shares for one new position, respecting per-symbol and total caps and available cash."""
    if price <= 0 or equity <= 0:
        return 0
    budget = min(
        equity * alloc_per_symbol,                         # per-position cap
        max(0.0, equity * max_invested - invested_value),  # total exposure cap
        cash * 0.99,                                       # never spend all cash (fees/price moves)
    )
    return max(0, math.floor(budget / price))


def stop_price(entry_price: float, stop_loss_pct: float) -> float:
    return round(entry_price * (1 - stop_loss_pct), 2)


def buy_limit(price: float, slippage: float) -> float:
    return round(price * (1 + slippage), 2)


def sell_limit(price: float, slippage: float) -> float:
    return round(price * (1 - slippage), 2)


def in_cooldown(stopped_out_on: str | None, today: date, cooldown_days: int) -> bool:
    if not stopped_out_on:
        return False
    return today < date.fromisoformat(stopped_out_on) + timedelta(days=cooldown_days)
