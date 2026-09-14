"""Poll/stream asks, run gap check, log GAP lines (gross + net edge).

REST poll path remains; WebSocket-first path lives in agents.watcher_agent
and long_run orchestration (config poll.use_websocket).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .arbitrage import Opportunity, compute_edge_breakdown, find_opportunity
from .fees import FeeConfig
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
    gross_edge: Optional[float] = None
    net_edge: Optional[float] = None
    fee_usdc: Optional[float] = None


class Watcher:
    def __init__(
        self,
        market: BtcUpDownMarket,
        clob: ClobRestClient,
        min_edge: float = 0.02,
        on_opportunity: Optional[OnOpp] = None,
        *,
        min_net_edge: Optional[float] = None,
        fee_cfg: Optional[FeeConfig] = None,
        shares: float = 1.0,
        as_taker: Optional[bool] = None,
    ) -> None:
        self.market = market
        self.clob = clob
        self.min_edge = min_edge
        self.min_net_edge = min_net_edge
        self.fee_cfg = fee_cfg
        self.shares = shares
        self.as_taker = as_taker
        self.on_opportunity = on_opportunity
        self.last: Optional[WatchSnapshot] = None
        self._last_log_key: Optional[tuple] = None
        self._last_log_ts: float = 0.0
        self.log_heartbeat_sec: float = 15.0

    def evaluate_asks(
        self, yes_ask: Optional[float], no_ask: Optional[float]
    ) -> WatchSnapshot:
        """Recompute opportunity from a Yes+No ask pair (REST or WS)."""
        use_fees = self.fee_cfg is not None
        opp = find_opportunity(
            yes_ask,
            no_ask,
            self.min_edge,
            min_net_edge=self.min_net_edge,
            shares=self.shares,
            fee_cfg=self.fee_cfg if use_fees else None,
            as_taker=self.as_taker if use_fees else None,
        )
        gross = net = fee = None
        if yes_ask is not None and no_ask is not None:
            br = compute_edge_breakdown(
                float(yes_ask),
                float(no_ask),
                shares=self.shares,
                fee_cfg=self.fee_cfg or FeeConfig(taker_rate=0.0, assume_taker=False),
                as_taker=self.as_taker,
            )
            gross, net, fee = br.gross_edge, br.net_edge, br.fee_usdc
        snap = WatchSnapshot(
            yes_ask=yes_ask,
            no_ask=no_ask,
            opportunity=opp,
            ts=time.time(),
            gross_edge=gross,
            net_edge=net,
            fee_usdc=fee,
        )
        self.last = snap
        self._log_gap(snap)
        if opp and self.on_opportunity:
            self.on_opportunity(opp, yes_ask or 0.0, no_ask or 0.0)
        return snap

    def poll_once(self) -> WatchSnapshot:
        yes_ask, no_ask = self.clob.get_asks_pair(
            self.market.yes_token_id, self.market.no_token_id
        )
        return self.evaluate_asks(yes_ask, no_ask)

    def _log_gap(self, snap: WatchSnapshot) -> None:
        y = snap.yes_ask
        n = snap.no_ask
        if y is None or n is None:
            key = ("MISSING", None, None)
            now = time.time()
            if key != self._last_log_key or (now - self._last_log_ts) >= self.log_heartbeat_sec:
                self._last_log_key = key
                self._last_log_ts = now
                logger.info(
                    "GAP market=%s yes_ask=%s no_ask=%s pair=? "
                    "gross_edge=? net_edge=? status=MISSING_ASK",
                    self.market.slug,
                    y,
                    n,
                )
            return
        pair = y + n
        gross = snap.gross_edge if snap.gross_edge is not None else (1.0 - pair)
        net = snap.net_edge if snap.net_edge is not None else gross
        status = "ARB" if snap.opportunity else "NO_ARB"
        floor = (
            self.min_net_edge
            if self.min_net_edge is not None
            else self.min_edge
        )
        key = (status, round(y, 4), round(n, 4))
        now = time.time()
        # Log on ask/status change, always on ARB, else heartbeat
        if (
            status == "ARB"
            or key != self._last_log_key
            or (now - self._last_log_ts) >= self.log_heartbeat_sec
        ):
            self._last_log_key = key
            self._last_log_ts = now
            logger.info(
                "GAP market=%s yes_ask=%.4f no_ask=%.4f pair=%.4f "
                "gross_edge=%.4f net_edge=%.4f fee_usdc=%.5f "
                "min_edge=%.4f min_net_edge=%s status=%s",
                self.market.slug,
                y,
                n,
                pair,
                gross,
                net,
                snap.fee_usdc or 0.0,
                self.min_edge,
                f"{floor:.4f}" if self.min_net_edge is not None else "None",
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
