"""All settings come from environment variables (loaded from .env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so we don't need python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0]  # allow inline comments
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class Settings:
    # mode: "paper" = local simulation, "sandbox" = Webull test env, "live" = real money
    mode: str = "paper"
    confirm_live: bool = False

    # Webull credentials
    app_key: str = ""
    app_secret: str = ""
    account_id: str = ""
    region: str = "us"

    # universe
    symbols: list = field(default_factory=lambda: ["SPY", "QQQ", "GLD"])

    # strategy (200-day trend filter with a band to cut whipsaws)
    sma_days: int = 200
    entry_band: float = 0.01   # buy when close > SMA * (1 + band)
    exit_band: float = 0.01    # sell when close < SMA * (1 - band)

    # risk
    bot_capital: float = 0.0         # cap on account equity the bot sizes from (0 = whole account)
    alloc_per_symbol: float = 0.25   # % of equity per position
    max_invested: float = 0.75       # never more than this % of equity in the market
    stop_loss_pct: float = 0.08      # broker-side stop, % below entry cost
    cooldown_days: int = 10          # after a stop-out, wait this many days before re-entering
    daily_loss_limit: float = 0.03   # no new buys if equity drops this much today
    max_drawdown_halt: float = 0.15  # halt ALL trading if equity falls this far from its peak
    limit_slippage: float = 0.002    # marketable-limit offset (0.2%)

    # schedule (US/Eastern)
    decision_time: str = "15:45"     # when to evaluate signals and trade
    housekeeping_minutes: int = 15

    # paper mode
    paper_starting_cash: float = 10000.0

    # misc
    data_dir: Path = Path("data")
    discord_webhook: str = ""

    @property
    def kill_file(self) -> Path:
        return self.data_dir / "KILL"


def load_settings(env_file: str = ".env") -> Settings:
    _load_dotenv(Path(env_file))
    s = Settings(
        mode=os.getenv("MODE", "paper").strip().lower(),
        confirm_live=_bool("CONFIRM_LIVE", False),
        app_key=os.getenv("WEBULL_APP_KEY", ""),
        app_secret=os.getenv("WEBULL_APP_SECRET", ""),
        account_id=os.getenv("WEBULL_ACCOUNT_ID", ""),
        region=os.getenv("WEBULL_REGION", "us"),
        symbols=[x.strip().upper() for x in os.getenv("SYMBOLS", "SPY,QQQ,GLD").split(",") if x.strip()],
        sma_days=_int("SMA_DAYS", 200),
        entry_band=_float("ENTRY_BAND", 0.01),
        exit_band=_float("EXIT_BAND", 0.01),
        bot_capital=_float("BOT_CAPITAL", 0),
        alloc_per_symbol=_float("ALLOC_PER_SYMBOL", 0.25),
        max_invested=_float("MAX_INVESTED", 0.75),
        stop_loss_pct=_float("STOP_LOSS_PCT", 0.08),
        cooldown_days=_int("COOLDOWN_DAYS", 10),
        daily_loss_limit=_float("DAILY_LOSS_LIMIT", 0.03),
        max_drawdown_halt=_float("MAX_DRAWDOWN_HALT", 0.15),
        limit_slippage=_float("LIMIT_SLIPPAGE", 0.002),
        decision_time=os.getenv("DECISION_TIME", "15:45"),
        housekeeping_minutes=_int("HOUSEKEEPING_MINUTES", 15),
        paper_starting_cash=_float("PAPER_STARTING_CASH", 10000),
        data_dir=Path(os.getenv("DATA_DIR", "data")),
        discord_webhook=os.getenv("DISCORD_WEBHOOK_URL", ""),
    )
    validate(s)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def validate(s: Settings) -> None:
    if s.mode not in ("paper", "sandbox", "live"):
        raise ValueError(f"MODE must be paper, sandbox or live (got {s.mode!r})")
    if s.mode == "live" and not s.confirm_live:
        raise ValueError("MODE=live also requires CONFIRM_LIVE=yes in .env")
    if s.mode in ("sandbox", "live") and not (s.app_key and s.app_secret):
        raise ValueError("WEBULL_APP_KEY and WEBULL_APP_SECRET are required for sandbox/live")
    if not 0 < s.alloc_per_symbol <= 0.5:
        raise ValueError("ALLOC_PER_SYMBOL must be between 0 and 0.5")
    if not 0 < s.max_invested <= 1.0:
        raise ValueError("MAX_INVESTED must be between 0 and 1 (no margin)")
    if not 0.01 <= s.stop_loss_pct <= 0.3:
        raise ValueError("STOP_LOSS_PCT must be between 0.01 and 0.30")
    if not s.symbols:
        raise ValueError("SYMBOLS is empty")
