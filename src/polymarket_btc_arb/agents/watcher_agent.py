"""Watcher agent: WS books (REST fallback) → opportunity queue.

Guide architecture: one tiny agent watches Yes+No asks and emits gaps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..arbitrage import Opportunity
from ..fees import FeeConfig
from ..markets.clob import ClobRestClient, ClobWsClient
from ..markets.gamma import BtcUpDownMarket
from ..watcher import Watcher, WatchSnapshot

logger = logging.getLogger(__name__)


@dataclass
class OpportunityEvent:
    opportunity: Opportunity
    market: BtcUpDownMarket
    yes_ask: float
    no_ask: float
    ts: float
    source: str  # "ws" | "rest"


class WatcherAgent:
    """Push opportunities onto an asyncio.Queue as books update."""

    def __init__(
        self,
        market: BtcUpDownMarket,
        clob: ClobRestClient,
        queue: asyncio.Queue,
        *,
        min_edge: float = 0.02,
        min_net_edge: Optional[float] = None,
        fee_cfg: Optional[FeeConfig] = None,
        shares: float = 1.0,
        as_taker: Optional[bool] = None,
        use_websocket: bool = True,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        rest_fallback_interval: float = 2.0,
        ws_stale_sec: float = 15.0,
    ) -> None:
        self.market = market
        self.clob = clob
        self.queue = queue
        self.use_websocket = use_websocket
        self.ws_url = ws_url
        self.rest_fallback_interval = rest_fallback_interval
        self.ws_stale_sec = ws_stale_sec
        self._watcher = Watcher(
            market=market,
            clob=clob,
            min_edge=min_edge,
            min_net_edge=min_net_edge,
            fee_cfg=fee_cfg,
            shares=shares,
            as_taker=as_taker,
            on_opportunity=None,  # we enqueue ourselves
        )
        self._yes_ask: Optional[float] = None
        self._no_ask: Optional[float] = None
        self._last_enqueue_key: Optional[tuple] = None
        self._last_ws_update = 0.0
        self.ws_client = ClobWsClient(ws_url=ws_url)

    def _on_book_pair(self, source: str) -> Optional[WatchSnapshot]:
        snap = self._watcher.evaluate_asks(self._yes_ask, self._no_ask)
        if snap.opportunity:
            key = (
                round(snap.opportunity.yes_ask, 4),
                round(snap.opportunity.no_ask, 4),
                round(snap.opportunity.net_edge, 4),
            )
            # Debounce identical consecutive emits
            if key != self._last_enqueue_key:
                self._last_enqueue_key = key
                evt = OpportunityEvent(
                    opportunity=snap.opportunity,
                    market=self.market,
                    yes_ask=float(snap.yes_ask or 0),
                    no_ask=float(snap.no_ask or 0),
                    ts=time.time(),
                    source=source,
                )
                try:
                    self.queue.put_nowait(evt)
                except asyncio.QueueFull:
                    logger.warning("opportunity queue full — dropping event")
        return snap

    async def _on_ws_update(
        self, asset_id: str, ask: Optional[float], bid: Optional[float]
    ) -> None:
        self._last_ws_update = time.time()
        if asset_id == self.market.yes_token_id:
            self._yes_ask = ask
        elif asset_id == self.market.no_token_id:
            self._no_ask = ask
        # Recompute immediately on each Yes/No book update
        self._on_book_pair("ws")

    async def run_ws(self, stop_event: asyncio.Event) -> None:
        assets = self.market.asset_ids
        logger.info(
            "watcher_agent WS mode assets=%s url=%s",
            [a[:12] + "…" for a in assets],
            self.ws_url,
        )
        await self.ws_client.run(
            assets, on_update=self._on_ws_update, stop_event=stop_event
        )

    async def run_rest_only(self, stop_event: asyncio.Event) -> None:
        logger.info(
            "watcher_agent REST poll interval=%.2fs", self.rest_fallback_interval
        )
        while not stop_event.is_set():
            try:
                y, n = await asyncio.to_thread(
                    self.clob.get_asks_pair,
                    self.market.yes_token_id,
                    self.market.no_token_id,
                )
                self._yes_ask, self._no_ask = y, n
                self._on_book_pair("rest")
            except Exception as exc:
                logger.warning("REST poll error: %s", exc)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.rest_fallback_interval
                )
            except asyncio.TimeoutError:
                pass

    async def run_rest_fallback_when_stale(
        self, stop_event: asyncio.Event
    ) -> None:
        """Poll REST only when WS has gone silent (true fallback)."""
        logger.info(
            "watcher_agent REST fallback armed (stale>%.1fs)", self.ws_stale_sec
        )
        while not stop_event.is_set():
            silent = (time.time() - self._last_ws_update) > self.ws_stale_sec
            if silent or self._last_ws_update == 0.0:
                try:
                    y, n = await asyncio.to_thread(
                        self.clob.get_asks_pair,
                        self.market.yes_token_id,
                        self.market.no_token_id,
                    )
                    self._yes_ask, self._no_ask = y, n
                    self._on_book_pair("rest")
                except Exception as exc:
                    logger.warning("REST fallback poll error: %s", exc)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.rest_fallback_interval
                )
            except asyncio.TimeoutError:
                pass

    async def run(self, stop_event: asyncio.Event) -> None:
        """WS-first with REST fallback when WS is stale; REST-only if disabled."""
        if not self.use_websocket:
            await self.run_rest_only(stop_event)
            return

        ws_task = asyncio.create_task(self.run_ws(stop_event), name="watcher-ws")
        rest_task = asyncio.create_task(
            self.run_rest_fallback_when_stale(stop_event),
            name="watcher-rest-fallback",
        )
        try:
            done, pending = await asyncio.wait(
                {ws_task, rest_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_event.is_set():
                for t in pending:
                    t.cancel()
                return
            for t in done:
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    logger.warning("watcher subtask ended: %s", exc)
            await asyncio.wait(pending | done, return_when=asyncio.ALL_COMPLETED)
        finally:
            self.ws_client.stop()
            for t in (ws_task, rest_task):
                if not t.done():
                    t.cancel()
