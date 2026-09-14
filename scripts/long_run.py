#!/usr/bin/env python3
"""Continuous BTC Up/Down arb runner with window rollover (dry-run default).

Two-agent orchestration (X guide):
  Watcher task — WS books (REST fallback) → opportunities queue
  Executor task — consume queue → both-legs-or-neither → merge
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

# package on path when run as: python scripts/long_run.py from repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket_btc_arb.agents.executor_agent import ExecutorAgent  # noqa: E402
from polymarket_btc_arb.agents.watcher_agent import WatcherAgent  # noqa: E402
from polymarket_btc_arb.config import load_config  # noqa: E402
from polymarket_btc_arb.executor import (  # noqa: E402
    DryRunBackend,
    Executor,
    LiveClobBackend,
)
from polymarket_btc_arb.fees import FeeConfig  # noqa: E402
from polymarket_btc_arb.live_gate import resolve_dry_run  # noqa: E402
from polymarket_btc_arb.markets.clob import ClobRestClient  # noqa: E402
from polymarket_btc_arb.markets.gamma import discover_btc_up_down  # noqa: E402
from polymarket_btc_arb.merge import (  # noqa: E402
    DryRunMerger,
    LiveMerger,
    merge_positions,
)
from polymarket_btc_arb.risk import RiskConfig, RiskManager  # noqa: E402
from polymarket_btc_arb.watcher import Watcher  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("polymarket_btc_arb.long_run")

STOP = False
PID_PATH = ROOT / "logs" / "long-run.pid"


def _handle_sig(_sig, _frame):
    global STOP
    STOP = True
    logger.info("stop requested")


def _window_secs(tf: str) -> int:
    return {"5m": 300, "15m": 900}[tf]


def _build_live_client(cfg: dict) -> Any:
    """Mirror main.py: ClobClient + create_or_derive_api_creds from cfg[_env]."""
    env = cfg.get("_env") or {}
    pk = env.get("private_key")
    if not pk:
        return None
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        logger.error("py-clob-client not installed; staying dry-run")
        return None
    client = ClobClient(
        env.get("host") or cfg["apis"]["clob_host"],
        key=pk,
        chain_id=env.get("chain_id", 137),
        signature_type=env.get("signature_type", 0),
        funder=env.get("funder"),
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def _write_pid() -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def _clear_pid() -> None:
    try:
        if PID_PATH.is_file():
            current = PID_PATH.read_text(encoding="utf-8").strip()
            if current == str(os.getpid()):
                PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _banner(
    *,
    dry_run: bool,
    reason: str,
    cfg: dict,
    size: float,
    max_daily_loss: float,
    max_arbs: Optional[int],
    cooldown: float,
) -> None:
    mode = "DRY_RUN" if dry_run else "LIVE"
    use_ws = bool(cfg["poll"].get("use_websocket", True))
    bankroll_hint = (
        "~$50 USDC path (live-50)"
        if "live-50" in str(cfg.get("_config_path", ""))
        else "per config"
    )
    lines = [
        "",
        "=" * 72,
        f"  MODE: {mode}",
        f"  reason: {reason}",
        f"  bankroll assumptions: {bankroll_hint}",
        f"  max_position_size: {size}",
        f"  max_daily_loss: {max_daily_loss}",
        f"  max_arbs_per_day: {max_arbs if max_arbs is not None else 'unlimited'}",
        f"  cooldown_sec_after_arb: {cooldown}",
        f"  timeframe: {cfg['strategy']['timeframe']}  min_edge: {cfg['strategy']['min_edge']}",
        f"  min_net_edge: {cfg['strategy'].get('min_net_edge')}",
        f"  use_websocket: {use_ws}  (watcher+executor agents)",
        f"  fees.taker_rate: {cfg.get('fees', {}).get('taker_rate')} "
        f"assume_taker: {cfg.get('fees', {}).get('assume_taker')}",
        f"  fill_timeout_sec: {cfg['execution'].get('fill_timeout_sec')}",
        "=" * 72,
        "",
    ]
    for line in lines:
        logger.info(line)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="long_run.py",
        description=(
            "Continuous Polymarket BTC Up/Down arb with window rollover. "
            "Default is dry-run; --live requires --i-understand-risk + key + py-clob-client."
        ),
    )
    p.add_argument(
        "-c",
        "--config",
        default="config/example.yaml",
        help="Path to YAML config (default: config/example.yaml)",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Attempt live trading (also requires --i-understand-risk)",
    )
    p.add_argument(
        "--i-understand-risk",
        dest="i_understand_risk",
        action="store_true",
        help="Acknowledge capital / fee / merge risks for --live",
    )
    p.add_argument(
        "--no-websocket",
        action="store_true",
        help="Force REST-only polling (overrides poll.use_websocket)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG logging",
    )
    return p


async def _run_two_agents_window(
    *,
    market: Any,
    cfg: dict,
    clob: ClobRestClient,
    backend: Any,
    merger: Any,
    risk: RiskManager,
    dry_run: bool,
    size: float,
    cooldown: float,
    fee_cfg: FeeConfig,
    ends_at: int,
    stop_flag: Callable[[], bool],
) -> int:
    """Run watcher + executor asyncio tasks until window end or stop."""
    min_edge = float(cfg["strategy"]["min_edge"])
    min_net_raw = cfg["strategy"].get("min_net_edge")
    min_net_edge = float(min_net_raw) if min_net_raw is not None else None
    use_ws = bool(cfg["poll"].get("use_websocket", True))
    interval = float(cfg["poll"].get("interval_sec") or 2.0)

    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    stop_event = asyncio.Event()

    watcher_agent = WatcherAgent(
        market=market,
        clob=clob,
        queue=queue,
        min_edge=min_edge,
        min_net_edge=min_net_edge,
        fee_cfg=fee_cfg,
        shares=size,
        as_taker=bool(fee_cfg.assume_taker),
        use_websocket=use_ws,
        ws_url=cfg["apis"]["ws_market"],
        rest_fallback_interval=interval,
    )
    executor = Executor(
        risk=risk,
        backend=backend,
        require_both_legs=bool(cfg["execution"]["require_both_legs"]),
        prefer_maker=bool(cfg["execution"]["prefer_maker"]),
    )
    executor_agent = ExecutorAgent(
        queue=queue,
        executor=executor,
        merger=merger,
        risk=risk,
        size=size,
        dry_run=dry_run,
        cooldown_sec=cooldown,
        require_merger=bool(cfg["agents"].get("merger", True)),
    )

    async def window_guard() -> None:
        while not stop_flag() and not risk.is_killed:
            now = int(time.time())
            if now >= ends_at - 5:
                logger.info("window ending — rolling to next market")
                stop_event.set()
                return
            await asyncio.sleep(0.5)
        stop_event.set()

    w_task = asyncio.create_task(watcher_agent.run(stop_event), name="agent-watcher")
    e_task = asyncio.create_task(executor_agent.run(stop_event), name="agent-executor")
    g_task = asyncio.create_task(window_guard(), name="window-guard")

    await g_task
    stop_event.set()
    watcher_agent.ws_client.stop()
    for t in (w_task, e_task):
        try:
            await asyncio.wait_for(t, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            t.cancel()
    return executor_agent.arb_hits


def _run_rest_legacy_window(
    *,
    market: Any,
    cfg: dict,
    clob: ClobRestClient,
    backend: Any,
    merger: Any,
    risk: RiskManager,
    dry_run: bool,
    size: float,
    cooldown: float,
    fee_cfg: FeeConfig,
    ends_at: int,
    stop_flag: Callable[[], bool],
    arb_hits: int,
    last_arb_ts: float,
    cycles_total: int,
) -> tuple[int, float, int]:
    """Synchronous REST-only path (used when asyncio agents disabled)."""
    min_edge = float(cfg["strategy"]["min_edge"])
    min_net_raw = cfg["strategy"].get("min_net_edge")
    min_net_edge = float(min_net_raw) if min_net_raw is not None else None
    interval = float(cfg["poll"].get("interval_sec") or 2.0)

    def on_opp(opp, _y, _n):
        nonlocal arb_hits, last_arb_ts
        if risk.is_killed:
            return
        if cooldown > 0 and last_arb_ts > 0:
            elapsed = time.time() - last_arb_ts
            if elapsed < cooldown:
                logger.info(
                    "cooldown: skip arb (%.1fs < %.1fs)", elapsed, cooldown
                )
                return
        ok, reason = risk.allow_trade(size)
        if not ok:
            logger.warning("skip arb: %s", reason)
            return
        report = Executor(
            risk=risk,
            backend=backend,
            require_both_legs=bool(cfg["execution"]["require_both_legs"]),
            prefer_maker=bool(cfg["execution"]["prefer_maker"]),
        ).execute_pair(
            opp,
            market.yes_token_id,
            market.no_token_id,
            size,
        )
        for line in report.planned_actions:
            logger.info("EXEC %s", line)
        if report.aborted:
            return
        if report.both_filled:
            mr = merge_positions(
                market.condition_id,
                report.size,
                dry_run=dry_run,
                backend=merger,
            )
            logger.info(
                "MERGE success=%s dry_run=%s detail=%s",
                mr.success,
                mr.dry_run,
                mr.detail,
            )
            if mr.success:
                risk.record_pnl(opp.net_edge * report.size)
                risk.record_arb()
                arb_hits += 1
                last_arb_ts = time.time()
                logger.info(
                    "ARB_HIT#%d gross_edge=%.4f net_edge=%.4f pair=%.4f "
                    "pnl=%.4f arbs_today=%d",
                    arb_hits,
                    opp.edge,
                    opp.net_edge,
                    opp.pair_cost,
                    opp.net_edge * report.size,
                    risk.arbs_today,
                )

    watcher = Watcher(
        market=market,
        clob=clob,
        min_edge=min_edge,
        min_net_edge=min_net_edge,
        fee_cfg=fee_cfg,
        shares=size,
        as_taker=bool(fee_cfg.assume_taker),
        on_opportunity=on_opp,
    )

    while not stop_flag() and not risk.is_killed:
        now = int(time.time())
        if now >= ends_at - 5:
            logger.info("window ending — rolling to next market")
            break
        try:
            watcher.poll_once()
            cycles_total += 1
        except Exception as exc:
            logger.warning("poll error: %s", exc)
        if cycles_total and cycles_total % 100 == 0:
            logger.info(
                "heartbeat cycles=%d arb_hits=%d daily_pnl=%.4f arbs_today=%d",
                cycles_total,
                arb_hits,
                risk.daily_pnl,
                risk.arbs_today,
            )
        time.sleep(interval)
    return arb_hits, last_arb_ts, cycles_total


def main(argv: Optional[list[str]] = None) -> int:
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    args = build_arg_parser().parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = ROOT / cfg_path
    cfg = load_config(str(cfg_path))
    cfg["_config_path"] = str(cfg_path)

    if args.no_websocket:
        cfg["poll"]["use_websocket"] = False

    dry_run, gate_reason = resolve_dry_run(args, cfg)
    cfg["execution"]["dry_run"] = dry_run

    timeframe = cfg["strategy"]["timeframe"]
    size = float(cfg["risk"]["max_position_size"])
    max_daily_loss = float(cfg["risk"]["max_daily_loss"])
    win = _window_secs(timeframe)

    max_arbs_raw = cfg["risk"].get("max_arbs_per_day")
    max_arbs: Optional[int] = int(max_arbs_raw) if max_arbs_raw is not None else None
    cooldown = float(cfg["risk"].get("cooldown_sec_after_arb") or 0)
    fee_cfg = FeeConfig.from_mapping(cfg.get("fees"))

    risk = RiskManager(
        RiskConfig(
            max_position_size=size,
            max_daily_loss=max_daily_loss,
            kill_switch=bool(cfg["risk"]["kill_switch"]),
            dry_run=dry_run,
            max_arbs_per_day=max_arbs,
        )
    )

    _banner(
        dry_run=dry_run,
        reason=gate_reason,
        cfg=cfg,
        size=size,
        max_daily_loss=max_daily_loss,
        max_arbs=max_arbs,
        cooldown=cooldown,
    )

    clob = ClobRestClient(host=cfg["apis"]["clob_host"])
    live_client = None
    fill_timeout = float(cfg["execution"].get("fill_timeout_sec") or 3.0)
    fill_poll = float(cfg["execution"].get("fill_poll_interval_sec") or 0.25)

    if dry_run:
        backend: Any = DryRunBackend(fill=True)
        merger: Any = DryRunMerger()
    else:
        live_client = _build_live_client(cfg)
        if live_client is None:
            logger.error(
                "live client build failed — forcing dry_run (%s)",
                gate_reason,
            )
            dry_run = True
            risk.config.dry_run = True
            cfg["execution"]["dry_run"] = True
            backend = DryRunBackend(fill=True)
            merger = DryRunMerger()
        else:
            backend = LiveClobBackend(
                live_client,
                fill_timeout_sec=fill_timeout,
                poll_interval_sec=fill_poll,
            )
            env = cfg.get("_env") or {}
            merge_cfg = cfg.get("merge") or {}
            merger = LiveMerger(
                live_client,
                private_key=env.get("private_key"),
                rpc_url=env.get("rpc_url"),
                collateral=merge_cfg.get("collateral"),
                use_adapter=bool(merge_cfg.get("use_adapter")),
                chain_id=int(env.get("chain_id") or 137),
            )

    arb_hits = 0
    cycles_total = 0
    last_arb_ts = 0.0
    exit_code = 0
    use_ws = bool(cfg["poll"].get("use_websocket", True))

    _write_pid()
    logger.info("pid=%s written to %s", os.getpid(), PID_PATH)

    try:
        while not STOP and not risk.is_killed:
            market = discover_btc_up_down(
                timeframe=timeframe,
                gamma_host=cfg["apis"]["gamma_host"],
            )
            if market is None:
                logger.warning("no open market — retry in 15s")
                time.sleep(15)
                continue

            ends_at = market.window_start + win
            logger.info(
                "watching slug=%s title=%r until=%s (in %ss) mode=%s",
                market.slug,
                market.title,
                ends_at,
                max(0, ends_at - int(time.time())),
                "ws+agents" if use_ws else "rest",
            )

            if use_ws:
                hits = asyncio.run(
                    _run_two_agents_window(
                        market=market,
                        cfg=cfg,
                        clob=clob,
                        backend=backend,
                        merger=merger,
                        risk=risk,
                        dry_run=dry_run,
                        size=size,
                        cooldown=cooldown,
                        fee_cfg=fee_cfg,
                        ends_at=ends_at,
                        stop_flag=lambda: STOP,
                    )
                )
                arb_hits += hits
                cycles_total += 1
            else:
                arb_hits, last_arb_ts, cycles_total = _run_rest_legacy_window(
                    market=market,
                    cfg=cfg,
                    clob=clob,
                    backend=backend,
                    merger=merger,
                    risk=risk,
                    dry_run=dry_run,
                    size=size,
                    cooldown=cooldown,
                    fee_cfg=fee_cfg,
                    ends_at=ends_at,
                    stop_flag=lambda: STOP,
                    arb_hits=arb_hits,
                    last_arb_ts=last_arb_ts,
                    cycles_total=cycles_total,
                )

        if risk.is_killed:
            logger.error(
                "exiting due to kill switch: %s",
                risk.kill_reason,
            )
            exit_code = 1
    finally:
        clob.close()
        _clear_pid()
        logger.info(
            "LONG RUN stop cycles=%d arb_hits=%d daily_pnl=%.4f killed=%s dry_run=%s",
            cycles_total,
            arb_hits,
            risk.daily_pnl,
            risk.is_killed,
            dry_run,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
