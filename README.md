# Coherence Bot — Polymarket

Detects **logical inconsistencies** between related Polymarket markets and
reports them on Discord.

The bot predicts nothing. It never says "buy" or "sell". It looks for prices
that **contradict each other**, and for each contradiction it builds a portfolio
whose payoff is positive whatever the outcome of the event. An alert is therefore
not an opinion: it is an arithmetic fact.

---

## The three contradictions it hunts

### 🟢🔵 Buckets — the sum must equal 1

On grouped markets (`negRisk`), outcomes are mutually exclusive and cover every
case: *Bitcoin price on August 14?* → `<56k`, `56-58k`, `58-60k`…
Exactly one will pay $1, so their prices must sum to 1.

- **Σ ask < 1** → buying YES on every outcome costs less than the guaranteed dollar.
- **Σ bid > 1** → buy NO on every outcome: only one will lose, the others pay.

Each outcome has its own order book and nothing mechanically enforces the sum:
that is where the gap appears.

### 🟣 Strike ladders — monotonicity

*Bitcoin above $60,000* cannot be **less** likely than *Bitcoin above
$70,000*: the second implies the first. When the order flips, the pair can be
arbitraged.

Also works on inverted ladders (*dip to $60,000*), whose monotonicity runs the
other way.

### 🟠 Date ladders — same principle

*Bitcoin hits $150k by December 31* ≥ *by June 30*. Any inversion can be exploited.

### The portfolio, in all three cases

If A implies B, then P(B) ≥ P(A). Buy YES on B (the broad one) and NO on A
(the narrow one):

| Outcome | YES B | NO A | Total |
|---|---|---|---|
| A true (so B true) | 1 | 0 | **1** |
| B true only | 1 | 1 | **2** |
| neither | 0 | 1 | **1** |

A guaranteed payoff of at least $1 for a cost of `ask(B) + 1 − bid(A)`. The trade is
profitable exactly when **`ask(B) < bid(A)`**, and the margin per share is
`bid(A) − ask(B)` — without any probability involved.

---

## What separates it from a toy

**Displayed prices are never used.** `outcomePrices` is a mid; an inversion on
mids disappears as soon as you look at the spread. Everything is computed on the
real order book, walking the levels one by one, which gives the size that can
actually be executed and the net profit at that size.

**Ladder orientation is inferred from the data, not guessed.** A keyword list
does not survive the variety of market wording: *"BTC above $60k"* and
*"ceasefire continues through Dec 31"* are both ladders, with opposite
monotonicity. A flipped sign doesn't produce a visible error — it produces a whole
ladder of very convincing fake opportunities (this happened during development).
The engine therefore reads the direction from the prices themselves (Kendall's
tau), and **stays silent** when the signal is ambiguous.

**Ranking is by APY, not ROI.** 0.3% that settles tomorrow is worth far more than
5% that settles in a year, since the capital gets recycled. Sorting by raw ROI
ranks things backwards.

**Partial sums are forbidden.** Dropping an outcome whose book is empty on one
side pushes the sum past 1 without any arb existing. The constraint only holds
over the exhaustive set.

---

## Installation

```bash
cd polymarket-coherence-bot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

Put your Discord bot token in `token.txt` (one line), or in
`DISCORD_BOT_TOKEN`. Optional: `DISCORD_GUILD_ID` to register commands
instantly instead of after about an hour.

```bash
./start_mac_linux.sh
```

## Without Discord

The engine is standalone — that's what makes it possible to check the numbers
without ever starting the bot:

```bash
./venv/bin/python coherence.py             # full scan
./venv/bin/python coherence.py --diagnose  # tightest margins, even those that hold
./venv/bin/python selftest.py              # 20 tests on synthetic order books
```

`--diagnose` exists for a specific reason: a scan that finds nothing is
indistinguishable from a broken engine. The diagnosis shows the constraints
closest to zero, which proves the pipeline is working and helps tune the thresholds.

## Discord commands

| Command | Effect |
|---|---|
| `/setup` | **Creates the full channel structure** and wires everything up (admin) |
| `/scan [limit]` | Immediate scan, top opportunities by annualized return |
| `/preview` | Posts a sample alert (checks formatting and permissions) |
| `/board` | Installs the live board, rewritten in place every cycle |
| `/track-board` | Installs the live track record board |
| `/watch [min_annualised] [min_profit]` | Subscribes the channel to alerts |
| `/unwatch` | Stops alerts in the channel |
| `/status` | Settings, thresholds and last scan |
| `/guide` | Posts the "how to read this channel" note (pin it) |

`/setup` creates the **POLYMARKET COHERENCE** category with four channels —
`coherence-guide` (pinned guide), `coherence-board` (live board),
`arb-alerts` (the feed) and `arb-discussion` (open) — the first three read-only
for members. The command is **idempotent**: running it again reuses the existing
channels and rewires everything, without creating duplicates.

Channel names are prefixed on purpose. This bot shares a server with the overlap
bot, whose `/setup` already creates `how-it-works` and `discussion`: with generic
names, each bot would think it recognized the other's channels and start writing
in them. The search for existing channels is also limited to this bot's own
category, not the whole server.

### The live board

Since "zero opportunities" is the normal state, a board that only listed arbs
would be permanently empty and wouldn't tell you whether the scanner is still
running. It shows the **market's coherence health** instead: the tightest
constraints, each with its gap at the top of the book **and the size that can
actually be executed**.

Both are essential. A real example: a **+7.10¢** gap on a temperature market,
backed by **0.03 shares** available on one of the legs. Showing the gap alone
would have been a permanent false promise.

## Settings

Everything is at the top of `coherence.py`:

| Setting | Default | Role |
|---|---|---|
| `MAX_EVENTS` | 400 | Events scanned (by 24h volume, descending) |
| `MIN_EDGE_CENTS` | 1.0 | Minimum margin per share, after fees |
| `MIN_APY_PCT` | 15.0 | The real decision filter |
| `MIN_PROFIT_USD` | 1.0 | Below this, gas eats the trade |
| `MAX_LEGS` | 15 | Beyond this, the capital tied up is absurd |
| `FEE_BPS` | 0.0 | **Check this** before trading for real |

A full scan covers ~1,900 markets in **~2 seconds**.

---

## Limitations — read before trading

- **Nothing is executed.** The bot detects and reports; it never places an order.
- **No atomic execution.** Polymarket has no all-or-nothing multi-leg orders.
  If one leg fills and the other doesn't, you are left with a directional bet —
  exactly what this tool is meant to avoid. That is the real risk.
- **The book moves within seconds.** Every number is valid for the moment it was
  sent.
- **Fees are assumed to be zero** (`FEE_BPS = 0`). Historically true on
  Polymarket, but fees have been introduced on some markets: check the current
  schedule before sizing a trade.
- **The `negRisk` mechanism** automatically closes part of the gaps on grouped
  markets, so "bucket" opportunities are rarer there than elsewhere.
- **Silence is the normal state.** A coherent market offers nothing. The bot only
  matters in the minutes when it isn't — if you want a constant stream of alerts,
  you built the wrong tool.

## License

[MIT](LICENSE)
