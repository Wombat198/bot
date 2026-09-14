"""Poll/stream asks, run gap check, log GAP lines."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .arbitrage import Opportunity, find_opportunity
from .markets.clob import ClobRestClient
from .markets.gamma import BtcUpDownMarket

logger = logging.getLogger(__name__)

OnOpp = Callable[[Opportunity, float, float], None]


@dataclass
class WatchSnapshot:
    yes_ask: Optional[float]
    no_ask: Optional[float]
    opportunity: Optional[Opportunity]
    ts: float


class Watcher:
    def __init__(
        self,
        market: BtcUpDownMarket,
        clob: ClobRestClient,
        min_edge: float = 0.02,
        on_opportunity: Optional[OnOpp] = None,
    ) -> None:
        self.market = market
        self.clob = clob
        self.min_edge = min_edge
        self.on_opportunity = on_opportunity
        self.last: Optional[WatchSnapshot] = None

    def poll_once(self) -> WatchSnapshot:
        yes_ask, no_ask = self.clob.get_asks_pair(
            self.market.yes_token_id, self.market.no_token_id
        )
        opp = find_opportunity(yes_ask, no_ask, self.min_edge)
        snap = WatchSnapshot(
            yes_ask=yes_ask, no_ask=no_ask, opportunity=opp, ts=time.time()
        )
        self.last = snap
        self._log_gap(snap)
        if opp and self.on_opportunity:
            self.on_opportunity(opp, yes_ask or 0.0, no_ask or 0.0)
        return snap

    def _log_gap(self, snap: WatchSnapshot) -> None:
        y = snap.yes_ask
        n = snap.no_ask
        if y is None or n is None:
            logger.info(
                "GAP market=%s yes_ask=%s no_ask=%s pair=? edge=? status=MISSING_ASK",
                self.market.slug,
                y,
                n,
            )
            return
        pair = y + n
        edge = 1.0 - pair
        status = "ARB" if snap.opportunity else "NO_ARB"
        logger.info(
            "GAP market=%s yes_ask=%.4f no_ask=%.4f pair=%.4f edge=%.4f "
            "min_edge=%.4f status=%s",
            self.market.slug,
            y,
            n,
            pair,
            edge,
            self.min_edge,
            status,
        )

    def run_poll_loop(
        self,
        interval_sec: float = 2.0,
        max_cycles: int = 0,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> list[WatchSnapshot]:
        """Poll forever (max_cycles=0) or for a fixed smoke count."""
        snaps: list[WatchSnapshot] = []
        cycles = 0
        while True:
            if should_stop and should_stop():
                break
            try:
                snaps.append(self.poll_once())
            except Exception as exc:
                logger.warning("poll error: %s", exc)
            cycles += 1
            if max_cycles and cycles >= max_cycles:
                break
            time.sleep(interval_sec)
        return snaps
