"""Gamma API discovery for BTC Up/Down 5m/15m markets."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "polymarket-btc-arb/0.1 (+research; dry-run friendly)"


@dataclass(frozen=True)
class BtcUpDownMarket:
    slug: str
    title: str
    condition_id: str
    yes_token_id: str  # "Up"
    no_token_id: str  # "Down"
    timeframe: str
    window_start: int
    accepting_orders: bool
    closed: bool
    outcomes: tuple[str, str]

    @property
    def asset_ids(self) -> list[str]:
        return [self.yes_token_id, self.no_token_id]


def _parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _window_starts(timeframe: str, now: Optional[int] = None) -> list[int]:
    secs = {"5m": 300, "15m": 900}[timeframe]
    t = int(now if now is not None else time.time())
    base = (t // secs) * secs
    # Prefer current, then next, then recent past (still open sometimes)
    return [base + i * secs for i in (0, 1, -1, 2, -2, 3)]


def slug_for(timeframe: str, window_start: int) -> str:
    return f"btc-updown-{timeframe}-{window_start}"


def _map_event(ev: dict, timeframe: str, window_start: int) -> Optional[BtcUpDownMarket]:
    markets = ev.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    tids = _parse_json_field(m.get("clobTokenIds"))
    outcomes = _parse_json_field(m.get("outcomes"))
    if not isinstance(tids, list) or len(tids) < 2:
        return None
    if not isinstance(outcomes, list) or len(outcomes) < 2:
        outcomes = ["Up", "Down"]

    # Map Up→yes, Down→no by outcome label (do not assume index order blindly)
    yes_idx, no_idx = 0, 1
    lowered = [str(o).lower() for o in outcomes]
    if "up" in lowered:
        yes_idx = lowered.index("up")
    elif "yes" in lowered:
        yes_idx = lowered.index("yes")
    if "down" in lowered:
        no_idx = lowered.index("down")
    elif "no" in lowered:
        no_idx = lowered.index("no")
    if yes_idx == no_idx:
        yes_idx, no_idx = 0, 1

    condition_id = m.get("conditionId") or m.get("condition_id") or ""
    if not condition_id:
        return None

    return BtcUpDownMarket(
        slug=ev.get("slug") or slug_for(timeframe, window_start),
        title=ev.get("title") or m.get("question") or "",
        condition_id=str(condition_id),
        yes_token_id=str(tids[yes_idx]),
        no_token_id=str(tids[no_idx]),
        timeframe=timeframe,
        window_start=window_start,
        accepting_orders=bool(m.get("acceptingOrders", True)),
        closed=bool(ev.get("closed") or m.get("closed")),
        outcomes=(str(outcomes[yes_idx]), str(outcomes[no_idx])),
    )


def discover_btc_up_down(
    timeframe: str = "15m",
    gamma_host: str = "https://gamma-api.polymarket.com",
    prefer_open: bool = True,
    client: Optional[httpx.Client] = None,
    now: Optional[int] = None,
) -> Optional[BtcUpDownMarket]:
    """Discover the current (or nearest open) BTC Up/Down market.

    Slug pattern: ``btc-updown-{5m|15m}-{unix_window_start}``.
    Falls back to public-search if slug lookup fails.
    """
    if timeframe not in ("5m", "15m"):
        raise ValueError("timeframe must be '5m' or '15m'")

    own = client is None
    http = client or httpx.Client(
        timeout=20.0,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        candidates: list[BtcUpDownMarket] = []
        for ws in _window_starts(timeframe, now=now):
            slug = slug_for(timeframe, ws)
            url = f"{gamma_host.rstrip('/')}/events"
            try:
                r = http.get(url, params={"slug": slug})
                r.raise_for_status()
                data = r.json()
            except Exception as exc:
                logger.debug("gamma slug miss %s: %s", slug, exc)
                continue
            if not data:
                continue
            ev = data[0] if isinstance(data, list) else data
            mapped = _map_event(ev, timeframe, ws)
            if mapped:
                candidates.append(mapped)
                logger.info(
                    "gamma: %s closed=%s accepting=%s",
                    mapped.slug,
                    mapped.closed,
                    mapped.accepting_orders,
                )

        if prefer_open:
            open_ones = [c for c in candidates if not c.closed and c.accepting_orders]
            if open_ones:
                # Prefer window containing now
                t = int(now if now is not None else time.time())
                open_ones.sort(key=lambda c: (abs(c.window_start - t), c.window_start))
                return open_ones[0]

        if candidates:
            return candidates[0]

        # Fallback search
        return _search_fallback(http, gamma_host, timeframe)
    finally:
        if own:
            http.close()


def _search_fallback(
    http: httpx.Client, gamma_host: str, timeframe: str
) -> Optional[BtcUpDownMarket]:
    url = f"{gamma_host.rstrip('/')}/public-search"
    try:
        r = http.get(
            url,
            params={"q": "Bitcoin Up or Down", "limit_per_type": 20},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("gamma search fallback failed: %s", exc)
        return None

    needle = f"btc-updown-{timeframe}-"
    for ev in data.get("events") or []:
        slug = str(ev.get("slug") or "")
        if needle not in slug:
            continue
        try:
            ws = int(slug.rsplit("-", 1)[-1])
        except ValueError:
            ws = 0
        mapped = _map_event(ev, timeframe, ws)
        if mapped and not mapped.closed and mapped.accepting_orders:
            logger.info("gamma search hit: %s", mapped.slug)
            return mapped
    return None
