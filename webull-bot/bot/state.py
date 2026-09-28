"""Small JSON state file + CSV trade journal."""
import csv
import json
from datetime import datetime
from pathlib import Path

DEFAULT_STATE = {
    "peak_equity": 0.0,
    "day": None,                # trading day the day_start_equity belongs to
    "day_start_equity": 0.0,
    "last_decision_day": None,  # so we only run the decision once per day
    "symbols": {},              # per-symbol: stop_order_id, stop_price, stopped_out_on, last_order_day
    "halted": False,
    "halt_reason": "",
}


class State:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "state.json"
        self.journal = data_dir / "trades.csv"
        self.data = json.loads(json.dumps(DEFAULT_STATE))
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def sym(self, symbol: str) -> dict:
        return self.data["symbols"].setdefault(symbol, {})

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        tmp.replace(self.path)

    def log_trade(self, mode: str, symbol: str, side: str, qty: int, price: float, kind: str, note: str = "") -> None:
        new = not self.journal.exists()
        with self.journal.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "mode", "symbol", "side", "qty", "price", "kind", "note"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), mode, symbol, side, qty, price, kind, note])
