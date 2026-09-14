#!/usr/bin/env python3
"""Continuous BTC Up/Down arb runner with window rollover (dry-run default)."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

# package on path when run as: python scripts/long_run.py from repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket_btc_arb.config import load_config  # noqa: E402
from polymarket_btc_arb.executor import (  # noqa: E402
    DryRunBackend,
    Executor,
    LiveClobBackend,
)
from polymarket_btc_arb.live_gate import resolve_dry_run  # noqa: E402
from polymarket_btc_arb.markets.clob import ClobRestClient  # noqa: E402
from polymarket_btc_arb.markets.gamma import discover_btc_up_down  # noqa: E402
from polymarket_btc_arb.merge import (  # noqa: E402
    DryRunMerger,
    LiveMergeStub,
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
    bankroll_hint = (
        "~$50 USDC path (live-50)" if "live-50" in str(cfg.get("_config_path", "")) else "per config"
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
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG logging",
    )
    return p


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

    dry_run, gate_reason = resolve_dry_run(args, cfg)
    cfg["execution"]["dry_run"] = dry_run

    timeframe = cfg["strategy"]["timeframe"]
    min_edge = float(cfg["strategy"]["min_edge"])
    size = float(cfg["risk"]["max_position_size"])
    max_daily_loss = float(cfg["risk"]["max_daily_loss"])
    interval = float(cfg["poll"].get("interval_sec") or 2.0)
    win = _window_secs(timeframe)

    max_arbs_raw = cfg["risk"].get("max_arbs_per_day")
    max_arbs: Optional[int] = int(max_arbs_raw) if max_arbs_raw is not None else None
    cooldown = float(cfg["risk"].get("cooldown_sec_after_arb") or 0)

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
            backend = LiveClobBackend(live_client)
            merger = LiveMergeStub(live_client)

    arb_hits = 0
    cycles_total = 0
    last_arb_ts = 0.0
    exit_code = 0

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
                "watching slug=%s title=%r until=%s (in %ss)",
                market.slug,
                market.title,
                ends_at,
                max(0, ends_at - int(time.time())),
            )

            def on_opp(opp, _y, _n):
                nonlocal arb_hits, last_arb_ts
                if risk.is_killed:
                    return
                # cooldown after a successful arb attempt
                if cooldown > 0 and last_arb_ts > 0:
                    elapsed = time.time() - last_arb_ts
                    if elapsed < cooldown:
                        logger.info(
                            "cooldown: skip arb (%.1fs < %.1fs)",
                            elapsed,
                            cooldown,
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
                        risk.record_pnl(opp.edge * report.size)
                        risk.record_arb()
                        arb_hits += 1
                        last_arb_ts = time.time()
                        logger.info(
                            "ARB_HIT#%d edge=%.4f pair=%.4f pnl=%.4f arbs_today=%d",
                            arb_hits,
                            opp.edge,
                            opp.pair_cost,
                            opp.edge * report.size,
                            risk.arbs_today,
                        )

            watcher = Watcher(
                market=market,
                clob=clob,
                min_edge=min_edge,
                on_opportunity=on_opp,
            )

            while not STOP and not risk.is_killed:
                now = int(time.time())
                # roll a bit before hard close so we catch the next window
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
