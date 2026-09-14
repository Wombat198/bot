"""Executor agent: consume opportunity queue → both-legs-or-neither → merge.

Guide architecture: second tiny agent executes; never leaves a naked leg.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ..executor import Executor
from ..merge import MergeBackend, merge_positions
from ..risk import RiskManager
from .watcher_agent import OpportunityEvent

logger = logging.getLogger(__name__)


class ExecutorAgent:
    def __init__(
        self,
        queue: asyncio.Queue,
        executor: Executor,
        merger: MergeBackend,
        risk: RiskManager,
        *,
        size: float,
        dry_run: bool = True,
        cooldown_sec: float = 0.0,
        require_merger: bool = True,
    ) -> None:
        self.queue = queue
        self.executor = executor
        self.merger = merger
        self.risk = risk
        self.size = size
        self.dry_run = dry_run
        self.cooldown_sec = cooldown_sec
        self.require_merger = require_merger
        self.arb_hits = 0
        self.last_arb_ts = 0.0

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info("executor_agent started (both-legs-or-neither)")
        while not stop_event.is_set() and not self.risk.is_killed:
            try:
                evt: OpportunityEvent = await asyncio.wait_for(
                    self.queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            try:
                await asyncio.to_thread(self._handle, evt)
            except Exception as exc:
                logger.exception("executor_agent handle failed: %s", exc)
            finally:
                self.queue.task_done()
        logger.info("executor_agent stopped arb_hits=%d", self.arb_hits)

    def _handle(self, evt: OpportunityEvent) -> None:
        if self.risk.is_killed:
            return
        if self.cooldown_sec > 0 and self.last_arb_ts > 0:
            elapsed = time.time() - self.last_arb_ts
            if elapsed < self.cooldown_sec:
                logger.info(
                    "cooldown: skip arb (%.1fs < %.1fs)",
                    elapsed,
                    self.cooldown_sec,
                )
                return
        ok, reason = self.risk.allow_trade(self.size)
        if not ok:
            logger.warning("skip arb: %s", reason)
            return

        market = evt.market
        opp = evt.opportunity
        logger.info(
            "EXECUTOR take source=%s gross_edge=%.4f net_edge=%.4f pair=%.4f",
            evt.source,
            opp.edge,
            opp.net_edge,
            opp.pair_cost,
        )
        report = self.executor.execute_pair(
            opp,
            market.yes_token_id,
            market.no_token_id,
            self.size,
        )
        for line in report.planned_actions:
            logger.info("EXEC %s", line)
        if report.aborted:
            return
        if report.both_filled and self.require_merger:
            mr = merge_positions(
                market.condition_id,
                report.size,
                dry_run=self.dry_run,
                backend=self.merger,
            )
            logger.info(
                "MERGE success=%s dry_run=%s detail=%s tx=%s",
                mr.success,
                mr.dry_run,
                mr.detail,
                mr.tx_hash,
            )
            if mr.success:
                pnl = opp.net_edge * report.size
                self.risk.record_pnl(pnl)
                self.risk.record_arb()
                self.arb_hits += 1
                self.last_arb_ts = time.time()
                logger.info(
                    "ARB_HIT#%d gross_edge=%.4f net_edge=%.4f pair=%.4f "
                    "pnl=%.4f arbs_today=%d",
                    self.arb_hits,
                    opp.edge,
                    opp.net_edge,
                    opp.pair_cost,
                    pnl,
                    self.risk.arbs_today,
                )
