"""Webull trend bot - SPY / QQQ / GLD, long-only, with hard risk limits.

Commands:
  python main.py run        start the scheduler loop (what the systemd service runs)
  python main.py once       run housekeeping + a decision right now (ignores the clock)
  python main.py status     show account, positions, stops and today's signals (no trading)
  python main.py check      Webull connection test: prints raw account/balance/positions (no trading)
  python main.py backtest   backtest the strategy on history (needs internet)
  python main.py kill       stop all new orders (protective stops stay)
  python main.py resume     clear the kill switch / drawdown halt
  python main.py flatten    EMERGENCY: sell all SPY/QQQ/GLD positions the bot manages
"""
import json
import logging
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from bot import strategy
from bot.broker import make_broker
from bot.config import load_settings
from bot.data import daily_closes
from bot.engine import Engine, now_et, parse_hhmm
from bot.state import State

log = logging.getLogger("bot")


def setup_logging(data_dir):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(data_dir / "bot.log", maxBytes=5_000_000, backupCount=5)
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("yfinance").setLevel(logging.WARNING)


def build():
    s = load_settings()
    setup_logging(s.data_dir)
    engine = Engine(s, make_broker(s), State(s.data_dir), daily_closes)
    return s, engine


def cmd_run():
    s, engine = build()
    log.info("starting bot in %s mode: %s", s.mode.upper(), ", ".join(s.symbols))
    engine._say(f"bot started ({', '.join(s.symbols)})")
    decision_at = parse_hhmm(s.decision_time)
    open_t, close_t, after_t = parse_hhmm("09:30"), parse_hhmm("16:00"), parse_hhmm("16:30")
    last_hk, failures = 0.0, 0

    while True:
        now = now_et()
        t, weekday = now.time(), now.weekday() < 5
        try:
            if weekday and decision_at <= t < close_t and engine.state.data.get("last_decision_day") != now.date().isoformat():
                engine.decide(now)
                last_hk = time.time()
            elif weekday and open_t <= t <= after_t and time.time() - last_hk >= s.housekeeping_minutes * 60:
                engine.housekeeping(now)
                last_hk = time.time()
            failures = 0
        except Exception as e:
            failures += 1
            log.exception("cycle failed")
            if failures in (1, 5) or failures % 20 == 0:
                engine._say(f"ERROR ({failures}x in a row): {e}")
            time.sleep(min(600, 30 * failures))
            continue
        time.sleep(60)


def cmd_once():
    s, engine = build()
    actions = engine.decide(now_et())
    print("actions:", actions or "none")


def cmd_status():
    s, engine = build()
    snap = engine.housekeeping(now_et())
    a = snap["account"]
    print(f"\nMode: {s.mode.upper()}   Equity: ${a.equity:,.2f}   Cash: ${a.cash:,.2f}")
    print(f"Risk: {snap['risk'].reason or 'OK'}   Kill switch: {'ON' if engine.killed() else 'off'}")
    for sym in s.symbols:
        pos = snap["positions"].get(sym)
        sig = strategy.evaluate(sym, engine.closes[sym], bool(pos), s.sma_days, s.entry_band, s.exit_band)
        st = engine.state.sym(sym)
        held = f"{pos.qty} sh @ {pos.avg_cost:.2f}" if pos else "flat"
        stop = f"stop {st.get('stop_price')}" if st.get("stop_order_id") else "no stop"
        print(f"  {sym:5} {held:22} {stop:14} signal={sig.action:4} ({sig.reason})")


def cmd_check():
    s = load_settings()
    setup_logging(s.data_dir)
    if s.mode == "paper":
        print("MODE=paper - nothing to check. Set MODE=sandbox (or live) plus your app key/secret in .env.")
        return
    print(f"Connecting to Webull ({s.mode}). If 2FA is on, open the Webull app -> Menu -> Messages -> "
          "OpenAPI Notifications and enter the SMS code within 5 minutes...\n")
    b = make_broker(s)
    for label, fn in [("ACCOUNTS", b.raw_accounts), ("BALANCE", b.raw_balance),
                      ("POSITIONS", b.raw_positions), ("OPEN ORDERS", b.raw_open_orders)]:
        print(f"=== {label} ===")
        print(json.dumps(fn(), indent=2)[:4000])
    print(f"\nUsing account_id: {b.account_id}")
    print("Parsed:", b.account(), b.positions())


def cmd_kill():
    s = load_settings()
    s.kill_file.write_text(datetime.now().isoformat())
    print("Kill switch ON - no new orders. Protective stops stay. `python main.py resume` to undo.")


def cmd_resume():
    s = load_settings()
    s.kill_file.unlink(missing_ok=True)
    st = State(s.data_dir)
    st.data.update(halted=False, halt_reason="", peak_equity=0.0)
    st.save()
    print("Kill switch off and halt cleared (peak equity reset).")


def cmd_flatten():
    if input("Sell ALL bot-managed positions now? type YES: ") != "YES":
        return print("cancelled")
    s, engine = build()
    engine.flatten(now_et())
    cmd_kill()


def cmd_backtest():
    from backtest import main as bt
    bt()


COMMANDS = {"run": cmd_run, "once": cmd_once, "status": cmd_status, "check": cmd_check,
            "kill": cmd_kill, "resume": cmd_resume, "flatten": cmd_flatten, "backtest": cmd_backtest}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
