"""CLOB REST order books and market WebSocket client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "polymarket-btc-arb/0.1 (+research; dry-run friendly)"

# Docs: bids ascending, asks descending → best ask / bid are LAST entries.
def best_ask_from_book(book: dict[str, Any]) -> Optional[float]:
    asks = book.get("asks") or []
    if not asks:
        return None
    level = asks[-1]
    price = level.get("price") if isinstance(level, dict) else level[0]
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def best_bid_from_book(book: dict[str, Any]) -> Optional[float]:
    bids = book.get("bids") or []
    if not bids:
        return None
    level = bids[-1]
    price = level.get("price") if isinstance(level, dict) else level[0]
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


class ClobRestClient:
    def __init__(
        self,
        host: str = "https://clob.polymarket.com",
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.host = host.rstrip("/")
        self._own = client is None
        self._http = client or httpx.Client(
            timeout=20.0,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def close(self) -> None:
        if self._own:
            self._http.close()

    def __enter__(self) -> "ClobRestClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def get_book(self, token_id: str) -> dict[str, Any]:
        r = self._http.get(f"{self.host}/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()

    def get_best_ask(self, token_id: str) -> Optional[float]:
        return best_ask_from_book(self.get_book(token_id))

    def get_asks_pair(
        self, yes_token_id: str, no_token_id: str
    ) -> tuple[Optional[float], Optional[float]]:
        return self.get_best_ask(yes_token_id), self.get_best_ask(no_token_id)

    def get_price_buy(self, token_id: str) -> Optional[float]:
        """BUY side price = lowest ask (what you pay to buy)."""
        r = self._http.get(
            f"{self.host}/price",
            params={"token_id": token_id, "side": "BUY"},
        )
        r.raise_for_status()
        data = r.json()
        raw = data.get("price") if isinstance(data, dict) else data
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


OnPrice = Callable[[str, Optional[float], Optional[float]], Awaitable[None] | None]


class ClobWsClient:
    """Market channel WS: subscribe assets_ids, PING every 10s.

    Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
    """

    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ping_interval: float = 10.0,
    ) -> None:
        self.ws_url = ws_url
        self.ping_interval = ping_interval
        self._best_ask: dict[str, Optional[float]] = {}
        self._best_bid: dict[str, Optional[float]] = {}
        self._running = False

    @property
    def best_asks(self) -> dict[str, Optional[float]]:
        return dict(self._best_ask)

    def get_ask(self, asset_id: str) -> Optional[float]:
        return self._best_ask.get(asset_id)

    async def run(
        self,
        asset_ids: list[str],
        on_update: Optional[OnPrice] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets package required for WS mode") from exc

        self._running = True
        stop = stop_event or asyncio.Event()
        backoff = 1.0

        while self._running and not stop.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,  # app-level PING/PONG
                    open_timeout=15,
                    close_timeout=5,
                    additional_headers={"User-Agent": USER_AGENT},
                ) as ws:
                    sub = {
                        "assets_ids": list(asset_ids),
                        "type": "market",
                        "custom_feature_enabled": True,
                    }
                    await ws.send(json.dumps(sub))
                    logger.info("ws subscribed to %d assets", len(asset_ids))
                    backoff = 1.0

                    async def heartbeat() -> None:
                        while not stop.is_set():
                            await asyncio.sleep(self.ping_interval)
                            try:
                                await ws.send("PING")
                            except Exception:
                                return

                    hb = asyncio.create_task(heartbeat())
                    try:
                        async for raw in ws:
                            if stop.is_set():
                                break
                            if raw == "PONG":
                                continue
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            await self._handle(msg, on_update)
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop.is_set() or not self._running:
                    break
                logger.warning("ws reconnect in %.1fs: %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

        self._running = False

    def stop(self) -> None:
        self._running = False

    async def _handle(self, msg: Any, on_update: Optional[OnPrice]) -> None:
        if isinstance(msg, list):
            for item in msg:
                await self._handle(item, on_update)
            return
        if not isinstance(msg, dict):
            return

        et = msg.get("event_type") or msg.get("type")
        if et == "book":
            aid = str(msg.get("asset_id") or msg.get("assetId") or "")
            ask = best_ask_from_book(msg)
            bid = best_bid_from_book(msg)
            if aid:
                self._best_ask[aid] = ask
                self._best_bid[aid] = bid
                if on_update:
                    res = on_update(aid, ask, bid)
                    if asyncio.iscoroutine(res):
                        await res
        elif et == "price_change":
            for pc in msg.get("price_changes") or []:
                aid = str(pc.get("asset_id") or "")
                ba = pc.get("best_ask")
                bb = pc.get("best_bid")
                if aid and ba is not None:
                    try:
                        self._best_ask[aid] = float(ba)
                    except (TypeError, ValueError):
                        pass
                if aid and bb is not None:
                    try:
                        self._best_bid[aid] = float(bb)
                    except (TypeError, ValueError):
                        pass
                if aid and on_update:
                    res = on_update(
                        aid, self._best_ask.get(aid), self._best_bid.get(aid)
                    )
                    if asyncio.iscoroutine(res):
                        await res
        elif et == "best_bid_ask":
            aid = str(msg.get("asset_id") or "")
            if aid:
                if msg.get("best_ask") is not None:
                    self._best_ask[aid] = float(msg["best_ask"])
                if msg.get("best_bid") is not None:
                    self._best_bid[aid] = float(msg["best_bid"])
                if on_update:
                    res = on_update(
                        aid, self._best_ask.get(aid), self._best_bid.get(aid)
                    )
                    if asyncio.iscoroutine(res):
                        await res
