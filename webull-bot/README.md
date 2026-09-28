# Webull Trend Bot: SPY, QQQ, GLD

A long-only trading bot for Webull that runs once a day and has hard safety limits built in. It starts in **paper mode** (fake money).

## What it does

**Rule (per ETF, checked once a day at 3:45 pm ET):**
- **Buy** when the price closes more than 1% above its 200-day average (the trend is up).
- **Sell** when it closes more than 1% below the average (the trend is down).
- Inside that ±1% band it does nothing, which cuts down on back-and-forth trades.

**Why these three:**

| ETF | What it is | Role |
|-----|------------|------|
| SPY | S&P 500 | Core US market |
| QQQ | Nasdaq-100 | Growth / tech |
| GLD | Gold | Often moves differently from stocks, which spreads out risk |

**Safety limits (all set in `.env`):**
- Long-only, no margin, no options, no shorting.
- 25% of capital per position, never more than 75% invested in total.
- A real **stop-loss order sits at Webull** 8% below your cost, so it protects you even if the droplet goes down.
- After a stop-out, it waits 10 days before buying that ETF again.
- After a 3% down day, it makes no new buys that day.
- If the account falls 15% below its peak, it **halts everything** and only restarts when you run `python main.py resume`.
- `BOT_CAPITAL` caps how much of the account the bot uses.
- Kill switch: `python main.py kill`. Emergency sell-all: `python main.py flatten`.
- Orders are limit orders set 0.2% past the last price. It never uses market orders.
- Going live takes **two** settings: `MODE=live` and `CONFIRM_LIVE=yes`.

## 1. Install on your droplet

```bash
cd /opt
git clone <your repo url> webull-bot     # or: scp -r webull-bot root@<droplet-ip>:/opt/
cd webull-bot
apt install -y python3-venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

## 2. Backtest it (see how the rules did historically)

```bash
venv/bin/python main.py backtest              # 2005 -> today
venv/bin/python main.py backtest 2015-01-01   # custom start
```

This compares the bot with SPY buy-and-hold and with the same three ETFs held without ever selling. Expect the bot to **give up some return in exchange for much smaller drawdowns**. That's the trade-off with trend-following.

## 3. Paper trade (fake money, no Webull keys needed)

```bash
venv/bin/python main.py once     # run a decision right now
venv/bin/python main.py status   # equity, positions, stops, signals
```

## 4. Get Webull API access

1. Log in at webull.com, then go to **Account Center → OpenAPI Management** (https://www.webull.com/center#openApiManagement) and apply. Review usually takes 1–2 business days.
2. Once approved, create an **App Key** and **App Secret**.
3. Put them in `.env`. Try `MODE=sandbox` first if Webull gives you test credentials.
4. Test the connection **in a terminal**, since the first run needs 2FA:
   ```bash
   venv/bin/python main.py check
   ```
   When it waits, open the **Webull app → Menu → Messages → OpenAPI Notifications** and enter the SMS code within 5 minutes. The token is saved in `data/token_*` and reused after that.
5. `check` prints your raw balance and positions, then the parsed values. **Make sure the parsed equity and cash match your account.** If they don't, send me the output with the account numbers blurred so I can map the fields.

Note: market data comes from Yahoo Finance (free). Webull's own data API needs a separate paid OpenAPI subscription, and daily signals don't need real-time quotes.

## 5. Run it 24/7

```bash
cp deploy/webull-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now webull-bot
journalctl -u webull-bot -f            # live logs (also in data/bot.log)
```

Trades are recorded in `data/trades.csv`. For phone alerts, set `DISCORD_WEBHOOK_URL`.

## 6. Going live (when you're ready)

Run paper, then sandbox, for **at least a few weeks**. Then:
1. Set `BOT_CAPITAL` to an amount you're comfortable with, e.g. `2000`.
2. Set `MODE=live` and `CONFIRM_LIVE=yes`.
3. Run `python main.py check`, then `systemctl restart webull-bot`.

## Commands

| Command | What it does |
|---------|--------------|
| `run` | Scheduler loop (used by systemd) |
| `once` | Run the decision now |
| `status` | Account, positions, stops, signals (no trading) |
| `check` | Webull connection test (no trading) |
| `backtest [start]` | Historical test |
| `kill` / `resume` | Stop / allow new orders (stops stay in place) |
| `flatten` | Sell all bot positions, then kill |

## Things to know

- **Use an account (or a `BOT_CAPITAL` amount) dedicated to the bot.** It manages any SPY, QQQ or GLD shares in the account and ignores everything else.
- **Whole shares only.** SPY and QQQ cost several hundred dollars a share, so each 25% slice needs to cover at least one share. With a small account, use fewer symbols or a larger `ALLOC_PER_SYMBOL` (max 0.5).
- **Cash accounts:** sale proceeds settle the next business day. The bot rarely sells and buys on the same day, but it can happen, so watch for good-faith-violation warnings from Webull.
- **Token expiry:** if Webull asks for 2FA again, the bot's API calls will fail and you'll get an alert. Run `main.py check` in a terminal to approve it, then restart the service.
- **Stop timing:** stops are placed on the next 15-minute check after a buy fills, so a new position can briefly have no stop.
- **No guarantees.** Backtests use simplified fills, and past results don't predict future returns. This is not financial advice.
- Tests: `python -m unittest discover -s tests`
