"""Broker adapters: PaperBroker (local simulation) and WebullBroker (sandbox or live).

Both expose the same small interface so the engine never cares which one it's using.
"""
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SANDBOX_ENDPOINT = "api.sandbox.webull.com"


class BrokerError(Exception):
    pass


@dataclass
class Account:
    equity: float
    cash: float


@dataclass
class Position:
    symbol: str
    qty: int
    avg_cost: float


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    order_type: str
    status: str = ""


def new_order_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Paper broker: simulates fills locally so you can watch the bot with fake money.
# ---------------------------------------------------------------------------
class PaperBroker:
    name = "paper"

    def __init__(self, data_dir: Path, starting_cash: float):
        self.path = data_dir / "paper_account.json"
        if self.path.exists():
            self.book = json.loads(self.path.read_text())
        else:
            self.book = {"cash": starting_cash, "positions": {}, "stops": {}}
        self.prices: dict[str, float] = {}

    def _save(self):
        self.path.write_text(json.dumps(self.book, indent=2))

    def update_prices(self, prices: dict[str, float]) -> list[str]:
        """Feed latest prices; triggers simulated stop orders. Returns symbols that stopped out."""
        self.prices.update(prices)
        stopped = []
        for oid, stop in list(self.book["stops"].items()):
            px = self.prices.get(stop["symbol"])
            if px is not None and px <= stop["stop_price"]:
                self._fill(stop["symbol"], "SELL", stop["qty"], px)
                del self.book["stops"][oid]
                stopped.append(stop["symbol"])
                log.info("[paper] STOP triggered %s %d @ %.2f", stop["symbol"], stop["qty"], px)
        self._save()
        return stopped

    def _fill(self, symbol, side, qty, price):
        pos = self.book["positions"].get(symbol, {"qty": 0, "avg_cost": 0.0})
        if side == "BUY":
            cost = qty * price
            if cost > self.book["cash"] + 1e-6:
                raise BrokerError(f"[paper] insufficient cash for {qty} {symbol}")
            total = pos["qty"] + qty
            pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + cost) / total
            pos["qty"] = total
            self.book["cash"] -= cost
        else:
            qty = min(qty, pos["qty"])
            pos["qty"] -= qty
            self.book["cash"] += qty * price
        if pos["qty"] > 0:
            self.book["positions"][symbol] = pos
        else:
            self.book["positions"].pop(symbol, None)

    def account(self) -> Account:
        mv = sum(p["qty"] * self.prices.get(s, p["avg_cost"]) for s, p in self.book["positions"].items())
        return Account(equity=self.book["cash"] + mv, cash=self.book["cash"])

    def positions(self) -> dict[str, Position]:
        return {s: Position(s, p["qty"], p["avg_cost"]) for s, p in self.book["positions"].items()}

    def open_orders(self) -> list[Order]:
        return [Order(oid, o["symbol"], "SELL", "STOP_LOSS", "WORKING") for oid, o in self.book["stops"].items()]

    def place_limit(self, symbol: str, side: str, qty: int, limit_price: float) -> str:
        px = self.prices.get(symbol, limit_price)
        marketable = px <= limit_price if side == "BUY" else px >= limit_price
        if not marketable:
            raise BrokerError(f"[paper] limit {limit_price} not marketable vs {px}")
        self._fill(symbol, side, qty, px)
        self._save()
        return new_order_id()

    def place_stop(self, symbol: str, qty: int, stop_price: float) -> str:
        oid = new_order_id()
        self.book["stops"][oid] = {"symbol": symbol, "qty": qty, "stop_price": stop_price}
        self._save()
        return oid

    def cancel(self, order_id: str) -> None:
        self.book["stops"].pop(order_id, None)
        self._save()


# ---------------------------------------------------------------------------
# Webull broker (official OpenAPI SDK). Works for the sandbox and live account.
# ---------------------------------------------------------------------------
EQUITY_KEYS = ["total_net_liquidation_value", "net_liquidation_value", "net_liquidation",
               "total_asset", "total_assets", "total_account_value", "account_value"]
CASH_KEYS = ["total_cash_balance", "cash_balance", "total_cash", "settled_cash",
             "cash_amount", "total_cash_amount", "cash"]
QTY_KEYS = ["quantity", "qty", "position", "holding_quantity", "total_quantity"]
COST_KEYS = ["cost_price", "average_cost", "avg_cost", "avg_price", "unit_cost", "cost_basis_price"]
OPEN_STATUSES = {"SUBMITTED", "WORKING", "PENDING", "PARTIAL_FILLED", "PARTIALLY_FILLED",
                 "NEW", "ACCEPTED", "OPEN", "PENDING_SUBMIT", "PENDING_NEW"}


def _find_number(obj, keys):
    """Depth-first search for the first key (in priority order) holding a numeric value."""
    for key in keys:
        found = _find_key(obj, key)
        if found is not None:
            try:
                return float(found)
            except (TypeError, ValueError):
                continue
    return None


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj and not isinstance(obj[key], (dict, list)):
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _collect(obj, must_have):
    """All dicts anywhere in obj that contain every key in must_have."""
    out = []
    if isinstance(obj, dict):
        if all(k in obj for k in must_have):
            out.append(obj)
        for v in obj.values():
            out.extend(_collect(v, must_have))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_collect(v, must_have))
    return out


def parse_account(raw) -> Account:
    equity = _find_number(raw, EQUITY_KEYS)
    cash = _find_number(raw, CASH_KEYS)
    if equity is None or cash is None:
        raise BrokerError("could not read equity/cash from balance response - run `python main.py check` "
                          "and share the output so the field names can be mapped")
    return Account(equity=equity, cash=cash)


def parse_positions(raw) -> dict[str, Position]:
    out = {}
    for d in _collect(raw, ["symbol"]):
        qty = _find_number({k: d[k] for k in d if k in QTY_KEYS}, QTY_KEYS)
        if qty is None or qty == 0:
            continue
        cost = _find_number({k: d[k] for k in d if k in COST_KEYS}, COST_KEYS) or 0.0
        sym = str(d["symbol"]).upper()
        if sym in out:  # duplicate nesting (e.g. summary + legs) - keep first
            continue
        out[sym] = Position(sym, int(qty), cost)
    return out


def parse_orders(raw) -> list[Order]:
    orders, seen = [], set()
    for d in _collect(raw, ["client_order_id", "symbol"]):
        oid = str(d["client_order_id"])
        if oid in seen:
            continue
        seen.add(oid)
        orders.append(Order(oid, str(d["symbol"]).upper(), str(d.get("side", "")).upper(),
                            str(d.get("order_type", "")).upper(), str(d.get("status", d.get("order_status", ""))).upper()))
    return orders


class WebullBroker:
    def __init__(self, app_key, app_secret, region, account_id, data_dir: Path, sandbox: bool):
        from webull.core.client import ApiClient
        from webull.trade.trade_client import TradeClient

        self.name = "sandbox" if sandbox else "live"
        api = ApiClient(app_key, app_secret, region)
        if sandbox:
            api.add_endpoint(region, SANDBOX_ENDPOINT)
        token_dir = data_dir / f"token_{self.name}"
        token_dir.mkdir(parents=True, exist_ok=True)
        api.set_token_dir(str(token_dir))  # 2FA token lives here (keep it private)
        try:
            # On first use with 2FA on, this waits until you approve in the Webull app.
            self.client = TradeClient(api)
        except Exception as e:
            raise BrokerError(f"could not connect to Webull ({self.name}): {e}") from e
        self.account_id = account_id or self._first_account_id()

    # -- raw helpers -------------------------------------------------------
    def _call(self, what, fn, *args, **kwargs):
        try:
            res = fn(*args, **kwargs)
        except Exception as e:  # SDK raises ClientException / ServerException
            raise BrokerError(f"{what} failed: {e}") from e
        if getattr(res, "status_code", 200) != 200:
            raise BrokerError(f"{what} failed: HTTP {res.status_code} {getattr(res, 'text', '')}")
        try:
            return res.json()
        except Exception:
            return {}

    def raw_accounts(self):
        return self._call("account list", self.client.account_v2.get_account_list)

    def raw_balance(self):
        return self._call("balance", self.client.account_v2.get_account_balance, self.account_id)

    def raw_positions(self):
        return self._call("positions", self.client.account_v2.get_account_position, self.account_id)

    def raw_open_orders(self):
        return self._call("open orders", self.client.order_v3.get_order_open, account_id=self.account_id)

    def _first_account_id(self) -> str:
        raw = self.raw_accounts()
        accts = _collect(raw, ["account_id"])
        if not accts:
            raise BrokerError(f"no accounts returned: {raw}")
        if len(accts) > 1:
            log.warning("multiple accounts found; using %s. Set WEBULL_ACCOUNT_ID to choose.", accts[0]["account_id"])
        return str(accts[0]["account_id"])

    # -- interface ---------------------------------------------------------
    def update_prices(self, prices):  # stops are real broker orders - nothing to simulate
        return []

    def account(self) -> Account:
        return parse_account(self.raw_balance())

    def positions(self) -> dict[str, Position]:
        return parse_positions(self.raw_positions())

    def open_orders(self) -> list[Order]:
        return [o for o in parse_orders(self.raw_open_orders()) if not o.status or o.status in OPEN_STATUSES]

    def _order(self, symbol, side, qty, order_type, tif, **price):
        oid = new_order_id()
        order = {
            "combo_type": "NORMAL",
            "client_order_id": oid,
            "symbol": symbol,
            "instrument_type": "EQUITY",
            "market": "US",
            "order_type": order_type,
            "quantity": str(int(qty)),
            "support_trading_session": "CORE",
            "side": side,
            "time_in_force": tif,
            "entrust_type": "QTY",
            **{k: f"{v:.2f}" for k, v in price.items()},
        }
        self._call(f"{side} {order_type} {symbol}", self.client.order_v3.place_order, self.account_id, [order])
        return oid

    def place_limit(self, symbol: str, side: str, qty: int, limit_price: float) -> str:
        return self._order(symbol, side, qty, "LIMIT", "DAY", limit_price=limit_price)

    def place_stop(self, symbol: str, qty: int, stop_price: float) -> str:
        return self._order(symbol, "SELL", qty, "STOP_LOSS", "GTC", stop_price=stop_price)

    def cancel(self, order_id: str) -> None:
        self._call("cancel", self.client.order_v3.cancel_order, self.account_id, order_id)


def make_broker(s):
    if s.mode == "paper":
        return PaperBroker(s.data_dir, s.paper_starting_cash)
    return WebullBroker(s.app_key, s.app_secret, s.region, s.account_id, s.data_dir, sandbox=(s.mode == "sandbox"))
