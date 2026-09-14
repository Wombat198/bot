"""Pure helpers for extracting best ask/bid from WS book / price_change msgs."""

from __future__ import annotations

from typing import Any, Optional


def best_ask_from_levels(asks: Any) -> Optional[float]:
    """Best ask from a levels list.

    Polymarket CLOB books: asks sorted descending → lowest ask is LAST.
    Also accepts ascending lists by taking min positive price.
    """
    if not asks:
        return None
    prices: list[float] = []
    for level in asks:
        if isinstance(level, dict):
            raw = level.get("price")
        elif isinstance(level, (list, tuple)) and level:
            raw = level[0]
        else:
            raw = level
        try:
            p = float(raw)
        except (TypeError, ValueError):
            continue
        if p > 0:
            prices.append(p)
    if not prices:
        return None
    # Prefer last level when it looks like descending Polymarket order
    last = prices[-1]
    if last == min(prices):
        return last
    return min(prices)


def best_bid_from_levels(bids: Any) -> Optional[float]:
    if not bids:
        return None
    prices: list[float] = []
    for level in bids:
        if isinstance(level, dict):
            raw = level.get("price")
        elif isinstance(level, (list, tuple)) and level:
            raw = level[0]
        else:
            raw = level
        try:
            p = float(raw)
        except (TypeError, ValueError):
            continue
        if p > 0:
            prices.append(p)
    if not prices:
        return None
    last = prices[-1]
    if last == max(prices):
        return last
    return max(prices)


def best_ask_from_ws_book(msg: dict[str, Any]) -> Optional[float]:
    """Extract best ask from a WS ``book`` event payload."""
    return best_ask_from_levels(msg.get("asks") or [])


def best_bid_from_ws_book(msg: dict[str, Any]) -> Optional[float]:
    return best_bid_from_levels(msg.get("bids") or [])


def best_ask_from_price_change(pc: dict[str, Any]) -> Optional[float]:
    """Extract best_ask from a single price_changes[] entry."""
    ba = pc.get("best_ask")
    if ba is None:
        return None
    try:
        p = float(ba)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None
