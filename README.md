# polymarket-btc-arb

Original Python bot for **Polymarket BTC Up/Down Yes+No ask-sum arbitrage**.

> **Disclaimer:** This is research / educational software. Trading prediction markets involves risk of loss, fees, latency, and smart-contract risk. **Default mode is dry-run** (paper). Do not use real funds unless you understand the code, fees, and legal constraints in your jurisdiction. No warranty.

## Strategy

Binary markets pay **$1** on the winning outcome and **$0** on the other. Holding **one Yes + one No** therefore equals **$1** at resolution (or after CTF merge).

**Arb condition (asks only):**

```
edge = 1.0 - (YES_ASK + NO_ASK)
trade when edge >= min_edge   # default 0.02
```

- Never use bid or mid for the gap check.
- Target markets: **BTC Up/Down** 5m / 15m only.
- One active market at a time; auto-discover via Gamma slug `btc-updown-{tf}-{unix}`.
- **Invariant: BOTH LEGS OR NEITHER.** If only one fill lands → **immediately unwind** the naked leg.
- After both fill → **merge** Yes+No → USDC immediately (do not wait for resolution).
- Prefer **maker / resting** orders when possible. **Taker fee risk:** crossing the book can erase thin edges.

## Layout

```
src/polymarket_btc_arb/
  arbitrage.py     # find_opportunity
  watcher.py       # poll asks, GAP logs
  risk.py          # size, daily loss, kill switch, dry_run
  executor.py      # both buys + unwind
  merge.py         # dry-run + live merge interface
  config.py
  main.py          # CLI
  markets/
    gamma.py       # discovery
    clob.py        # REST books + WS client
config/example.yaml
tests/
```

## Setup

```bash
cd /workspace/polymarket-btc-arb
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# optional live trading extras:
# pip install -e ".[live]"
cp .env.example .env   # only if going live
```

## Run (dry-run first)

```bash
# smoke: discover market, fetch live asks, a few GAP lines, exit cleanly
python -m polymarket_btc_arb --config config/example.yaml --smoke 3 --poll-interval 1

# or installed console script
polymarket-btc-arb --smoke 3

# 5m markets
python -m polymarket_btc_arb --timeframe 5m --smoke 5
```

Live trading (not recommended until you review merge/signing carefully):

```bash
# set POLYMARKET_PRIVATE_KEY in .env, install py-clob-client, then:
python -m polymarket_btc_arb --live
```

Without a private key, `--live` is forced back to dry-run.


## $50 live checklist

Tuned config: `config/live-50.yaml` (~$5 max size, $10 daily loss kill, 10 arbs/day, 30s cooldown). **`dry_run: true` stays in the file**; only CLI `--live` can flip it (and only after gates pass).

1. Fund a Polymarket wallet with a small USDC balance (start ~$50).
2. `cp .env.example .env` and set `POLYMARKET_PRIVATE_KEY` (never commit `.env`).
3. `pip install -e ".[live]"` (needs `py-clob-client`).
4. Dry-run first with the live-50 sizing (paper fills against live books):
   ```bash
   python scripts/long_run.py --config config/live-50.yaml
   ```
5. Only then, if you accept the risks:
   ```bash
   python scripts/long_run.py --config config/live-50.yaml --live --i-understand-risk
   ```

**Warnings:** live merge is **not fully turnkey** (wallet/CTF approvals / SDK wiring). Polymarket has **geo restrictions**. Never commit `.env`. Start tiny. Kill switch trips on `max_daily_loss`. Operator should `tee` stdout to a log file.

## Tests

```bash
pytest -q
```

## APIs used

| Purpose | Endpoint |
|--------|----------|
| Discover | `https://gamma-api.polymarket.com/events?slug=btc-updown-…` |
| Books | `https://clob.polymarket.com/book?token_id=…` |
| WS | `wss://ws-subscriptions-clob.polymarket.com/ws/market` (`assets_ids`, `PING` every 10s) |

## Live vs dry-run

| Feature | dry_run=true (default) | dry_run=false + key |
|--------|-------------------------|---------------------|
| Gamma discovery | live public | live public |
| Ask polling | live public | live public |
| Orders | planned actions logged | `py-clob-client` |
| Unwind | simulated | live SELL |
| Merge | stub log | SDK/`LiveMergeStub` — **you must wire wallet/CTF** |

Merge/signing limitations: live merge depends on wallet type (EOA / proxy / Safe), collateral approvals, and SDK version (`py-clob-client`, `py-sdk`, or `poly-web3`). The bot exposes a clear interface; production merge is **not** fully turnkey without your credentials and approvals.

## Config

See `config/example.yaml`. Env: `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_DRY_RUN`, `POLYMARKET_CONFIG`.

## License

MIT — see `LICENSE`.
