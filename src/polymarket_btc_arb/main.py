"""CLI entry: discover market → watch asks → optional execute/merge."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from .arbitrage import Opportunity
from .config import load_config
from .executor import DryRunBackend, Executor, LiveClobBackend
from .fees import FeeConfig
from .markets.clob import ClobRestClient
from .markets.gamma import discover_btc_up_down
from .merge import DryRunMerger, LiveMerger, merge_positions
from .risk import RiskConfig, RiskManager
from .watcher import Watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("polymarket_btc_arb")


def _build_live_client(cfg: dict):
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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polymarket-btc-arb",
        description="Polymarket BTC Up/Down Yes+No ask-sum arbitrage (dry-run default)",
    )
    p.add_argument(
        "-c",
        "--config",
        default="config/example.yaml",
        help="Path to YAML config",
    )
    p.add_argument(
        "--timeframe",
        choices=("5m", "15m"),
        default=None,
        help="Override strategy.timeframe",
    )
    p.add_argument(
        "--min-edge",
        type=float,
        default=None,
        help="Override strategy.min_edge",
    )
    p.add_argument(
        "--smoke",
        type=int,
        nargs="?",
        const=3,
        default=None,
        help="Smoke mode: N poll cycles then exit (default 3 if flag alone)",
    )
    p.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Seconds between REST polls",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Attempt live trading (requires key; still gated by risk)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG logging",
    )
    return p


def run(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg = load_config(args.config)
    if args.timeframe:
        cfg["strategy"]["timeframe"] = args.timeframe
    if args.min_edge is not None:
        cfg["strategy"]["min_edge"] = args.min_edge
    if args.poll_interval is not None:
        cfg["poll"]["interval_sec"] = args.poll_interval
    if args.smoke is not None:
        cfg["poll"]["smoke_max_cycles"] = args.smoke
    if args.live:
        cfg["execution"]["dry_run"] = False
        if not (cfg.get("_env") or {}).get("private_key"):
            logger.warning("POLYMARKET_PRIVATE_KEY missing — forcing dry_run")
            cfg["execution"]["dry_run"] = True

    dry_run = bool(cfg["execution"]["dry_run"])
    timeframe = cfg["strategy"]["timeframe"]
    min_edge = float(cfg["strategy"]["min_edge"])
    min_net_raw = cfg["strategy"].get("min_net_edge")
    min_net_edge = float(min_net_raw) if min_net_raw is not None else None
    size = float(cfg["risk"]["max_position_size"])
    fee_cfg = FeeConfig.from_mapping(cfg.get("fees"))

    logger.info(
        "starting dry_run=%s timeframe=%s min_edge=%s min_net_edge=%s max_size=%s",
        dry_run,
        timeframe,
        min_edge,
        min_net_edge,
        size,
    )
    if cfg.get("_forced_dry_run"):
        logger.warning("live requested without private key — dry_run forced")

    risk = RiskManager(
        RiskConfig(
            max_position_size=float(cfg["risk"]["max_position_size"]),
            max_daily_loss=float(cfg["risk"]["max_daily_loss"]),
            kill_switch=bool(cfg["risk"]["kill_switch"]),
            dry_run=dry_run,
        )
    )

    market = discover_btc_up_down(
        timeframe=timeframe,
        gamma_host=cfg["apis"]["gamma_host"],
    )
    if market is None:
        logger.error(
            "Could not discover an open BTC Up/Down market "
            "(network blocked or no open window). Exiting."
        )
        return 2

    logger.info(
        "market slug=%s title=%r condition=%s up=%s… down=%s…",
        market.slug,
        market.title,
        market.condition_id[:18] + "…",
        market.yes_token_id[:16],
        market.no_token_id[:16],
    )

    clob = ClobRestClient(host=cfg["apis"]["clob_host"])
    fill_timeout = float(cfg["execution"].get("fill_timeout_sec") or 3.0)
    fill_poll = float(cfg["execution"].get("fill_poll_interval_sec") or 0.25)
    live_client = None if dry_run else _build_live_client(cfg)
    if dry_run or live_client is None:
        backend = DryRunBackend(fill=True)
        merger = DryRunMerger()
        if not dry_run and live_client is None:
            risk.config.dry_run = True
            dry_run = True
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

    executor = Executor(
        risk=risk,
        backend=backend,
        require_both_legs=bool(cfg["execution"]["require_both_legs"]),
        prefer_maker=bool(cfg["execution"]["prefer_maker"]),
    )

    executed_once = False

    def on_opp(opp: Opportunity, _y: float, _n: float) -> None:
        nonlocal executed_once
        if not cfg["agents"].get("executor", True):
            return
        if risk.is_killed:
            logger.error("skip — kill switch: %s", risk.kill_reason)
            return
        if executed_once and dry_run and cfg["poll"]["smoke_max_cycles"]:
            return
        report = executor.execute_pair(
            opp,
            market.yes_token_id,
            market.no_token_id,
            size,
        )
        executed_once = True
        for line in report.planned_actions:
            logger.info("EXEC %s", line)
        if report.both_filled and cfg["agents"].get("merger", True):
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
            if dry_run and mr.success:
                risk.record_pnl(opp.net_edge * report.size)

    watcher = Watcher(
        market=market,
        clob=clob,
        min_edge=min_edge,
        min_net_edge=min_net_edge,
        fee_cfg=fee_cfg,
        shares=size,
        as_taker=bool(fee_cfg.assume_taker),
        on_opportunity=on_opp if cfg["agents"].get("watcher", True) else None,
    )

    max_cycles = int(cfg["poll"].get("smoke_max_cycles") or 0)
    interval = float(cfg["poll"].get("interval_sec") or 2.0)

    try:
        snaps = watcher.run_poll_loop(
            interval_sec=interval,
            max_cycles=max_cycles,
            should_stop=lambda: risk.is_killed,
        )
        logger.info("done cycles=%d last=%s", len(snaps), snaps[-1] if snaps else None)
        return 0 if snaps else 1
    finally:
        clob.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
