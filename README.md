# polymarket-btc-arb

Original Python bot for **Polymarket BTC Up/Down Yes+No ask-sum arbitrage**.

> **Disclaimer:** This is research / educational software. Trading prediction markets involves risk of loss, fees, latency, and smart-contract risk. **Default mode is dry-run** (paper). Do not use real funds unless you understand the code, fees, and legal constraints in your jurisdiction. No warranty.

## Strategy

Binary markets pay **$1** on the winning outcome and **$0** on the other. Holding **one Yes + one No** therefore equals **$1** at resolution (or after CTF merge).

**Arb condition (asks only):**

```
gross_edge = 1.0 - (YES_ASK + NO_ASK)
net_edge   = gross_edge - fee_per_share   # Crypto taker curve
trade when net_edge >= min_net_edge (or min_edge)
```

- Never use bid or mid for the gap check.
- Target markets: **BTC Up/Down** 5m / 15m only.
- One active market at a time; auto-discover via Gamma slug `btc-updown-{tf}-{unix}`.
- **Invariant: BOTH LEGS OR NEITHER.** If only one fill lands → **immediately unwind** the naked leg (actual `fill_size` only).
- After both fill → **merge** Yes+No → USDC immediately (do not wait for resolution).
- Prefer **maker / resting** orders when possible. **Taker fee risk:** crossing the book can erase thin edges.

## Parity with X guide (bored2boar-style)

| Guide step | Where in this repo |
|------------|--------------------|
| Watch Yes+No **asks** only | `watcher.py` / `agents/watcher_agent.py` |
| Gap when ask sum &lt; $1 | `arbitrage.find_opportunity` |
| **WebSocket-first** books + PING/10s | `markets/clob.ClobWsClient`, `poll.use_websocket: true` |
| REST fallback + reconnect | `WatcherAgent.run` (WS + REST tasks) |
| Fee-aware edge | `fees.py` + GAP logs `gross_edge` / `net_edge` |
| Two tiny agents (watcher / executor) | `agents/watcher_agent.py`, `agents/executor_agent.py`, orchestrated in `scripts/long_run.py` |
| Both legs or neither + unwind | `executor.Executor.execute_pair` |
| Real fill confirmation | `executor.confirm_order_fill` / `LiveClobBackend` |
| Merge for capital velocity | `merge.LiveMerger` (CTF `mergePositions` / SDK) |
| BTC only | `markets/gamma.discover_btc_up_down` |

## Layout

```
src/polymarket_btc_arb/
  arbitrage.py     # find_opportunity (+ net edge)
  fees.py          # documented CLOB fee curve
  watcher.py       # poll/evaluate asks, GAP logs
  agents/          # watcher_agent + executor_agent (guide split)
  risk.py          # size, daily loss, kill switch, dry_run
  executor.py      # both buys + fill confirm + unwind
  merge.py         # DryRunMerger + LiveMerger (CTF/web3)
  config.py
  main.py          # CLI (REST smoke)
  markets/
    gamma.py       # discovery
    clob.py        # REST books + WS client
    ws_book.py     # pure ask extraction
config/example.yaml
config/live-50.yaml
scripts/long_run.py
tests/
```

## Setup

```bash
cd /workspace/polymarket-btc-arb
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# optional live trading extras (CLOB + web3 merge):
# pip install -e ".[live]"
cp .env.example .env   # only if going live
```

## Run WS dry-run (default path)

```bash
# continuous two-agent loop (WS books → queue → paper execute/merge)
python scripts/long_run.py --config config/example.yaml
# or $50 sizing profile (still dry-run until --live):
python scripts/long_run.py --config config/live-50.yaml

# force REST-only:
python scripts/long_run.py --config config/live-50.yaml --no-websocket

# short smoke (REST CLI):
python -m polymarket_btc_arb --config config/example.yaml --smoke 3 --poll-interval 1
```

Live trading (not recommended until you review merge/signing carefully):

```bash
# set POLYMARKET_PRIVATE_KEY (+ optional POLYMARKET_RPC_URL) in .env
# pip install -e ".[live]"
python scripts/long_run.py --config config/live-50.yaml --live --i-understand-risk
```

Without a private key, `--live` is forced back to dry-run.

## Required approvals (live merge / trading)

Before live merge succeeds, your wallet must have:

1. **Conditional Tokens ERC-1155** `setApprovalForAll`:
   - CTF Exchange (for CLOB trading — usually set by Polymarket UI / `py-clob-client`)
   - CTF / `CtfCollateralAdapter` when merging via adapter
2. **Collateral ERC-20** `approve` as needed for split flows (merge *returns* USDC/pUSD)

Polygon addresses (common):

| Contract | Address |
|----------|---------|
| CTF | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| pUSD | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |
| CtfCollateralAdapter | `0xAdA100Db00Ca00073811820692005400218FcE1f` |

`LiveMerger` calls CTF `mergePositions(collateral, 0x0, conditionId, [1,2], amount)` via web3 when `POLYMARKET_RPC_URL` + key are set, or duck-types an SDK `merge_positions` on the injected client. **It never fakes success.**

## Fees

Documented Crypto curve (`https://docs.polymarket.com/trading/fees`):

```
fee = C × feeRate × p × (1 − p)     # feeRate=0.07 for Crypto
```

Config: `fees.taker_rate`, `fees.maker_rebate`, `fees.assume_taker`, `strategy.min_net_edge`.

## $50 live checklist

Tuned config: `config/live-50.yaml` (~$5 max size, $10 daily loss kill, 10 arbs/day, 30s cooldown). **`dry_run: true` stays in the file**; only CLI `--live` can flip it (and only after gates pass).

1. Fund a Polymarket wallet with a small USDC balance (start ~$50).
2. `cp .env.example .env` and set `POLYMARKET_PRIVATE_KEY` (never commit `.env`).
3. Optionally set `POLYMARKET_RPC_URL` for on-chain merge.
4. `pip install -e ".[live]"`.
5. Dry-run first: `python scripts/long_run.py --config config/live-50.yaml`
6. Only then: `python scripts/long_run.py --config config/live-50.yaml --live --i-understand-risk`

**Warnings:** wallet/CTF approvals are **operator-owned**. Polymarket has **geo restrictions**. Never commit `.env`. Start tiny. Kill switch trips on `max_daily_loss`.

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
| Ask watching | WS + REST public | WS + REST public |
| Orders | planned actions logged | `py-clob-client` + fill poll |
| Unwind | simulated | live SELL of actual fill_size |
| Merge | stub log | `LiveMerger` CTF/web3 or SDK — **approvals required** |

## Config

See `config/example.yaml`. Notable keys: `poll.use_websocket`, `fees.*`, `execution.fill_timeout_sec`, `strategy.min_net_edge`.

Env: `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_DRY_RUN`, `POLYMARKET_CONFIG`, `POLYMARKET_RPC_URL`.

## License

MIT — see `LICENSE`.

## 24/7 dry-run

```bash
bash scripts/supervise_long_run.sh
# default CONFIG=config/live-50.yaml (still dry-run until --live --i-understand-risk)
# logs: logs/long-run.log  pid: logs/supervise.pid
```

Auto-restarts on crash with backoff. Pair with an external keep-alive if the host can reboot.
