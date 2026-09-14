"""Yes+No ask-sum gap detection.

Binary markets: owning one Yes + one No pays exactly $1 at resolution.
Opportunity when YES_ASK + NO_ASK < 1.0 and edge >= min_edge (default 0.02).
Uses ASK prices only — never bid or mid.

When fee config is supplied, the gap is tradeable only if
``edge_after_fees >= min_net_edge`` (or ``min_edge`` if min_net_edge unset).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .fees import FeeConfig, EdgeBreakdown, edge_after_fees, is_tradeable_after_fees


@dataclass(frozen=True)
class Opportunity:
    yes_ask: float
    no_ask: float
    pair_cost: float
    edge: float  # gross edge = 1 - pair_cost
    min_edge: float
    net_edge: float = 0.0
    fee_usdc: float = 0.0
    fee_per_share: float = 0.0
    min_net_edge: Optional[float] = None

    @property
    def is_actionable(self) -> bool:
        floor = self.min_edge if self.min_net_edge is None else self.min_net_edge
        return self.net_edge >= floor and self.pair_cost < 1.0

    @property
    def gross_edge(self) -> float:
        return self.edge


def find_opportunity(
    yes_ask: Optional[float],
    no_ask: Optional[float],
    min_edge: float = 0.02,
    *,
    min_net_edge: Optional[float] = None,
    shares: float = 1.0,
    fee_cfg: Optional[FeeConfig] = None,
    as_taker: Optional[bool] = None,
) -> Optional[Opportunity]:
    """Return an Opportunity if asks sum below 1.0 by enough net edge.

    Parameters
    ----------
    yes_ask, no_ask:
        Best ask prices in [0, 1]. None / non-positive / missing → no opp.
    min_edge:
        Minimum gross edge when fees are ignored / floor fallback.
    min_net_edge:
        If set, gate on net edge after fees instead of gross min_edge.
    shares:
        Size used to scale absolute fee USDC (per-share fee is size-invariant
        for the linear curve).
    fee_cfg:
        Fee curve parameters; default Crypto taker rate 0.07.
    """
    if yes_ask is None or no_ask is None:
        return None
    try:
        y = float(yes_ask)
        n = float(no_ask)
    except (TypeError, ValueError):
        return None
    if y <= 0 or n <= 0 or y > 1 or n > 1:
        return None
    if min_edge < 0:
        raise ValueError("min_edge must be >= 0")

    cfg = fee_cfg or FeeConfig(taker_rate=0.0, assume_taker=False)
    # Backward compatible: if caller passes no fee_cfg, keep gross-only gate
    # matching historical tests (fee_rate 0 → net == gross).
    if fee_cfg is None and as_taker is None:
        pair_cost = y + n
        edge = 1.0 - pair_cost
        if pair_cost >= 1.0 or edge < min_edge:
            return None
        return Opportunity(
            yes_ask=y,
            no_ask=n,
            pair_cost=pair_cost,
            edge=edge,
            min_edge=min_edge,
            net_edge=edge,
            fee_usdc=0.0,
            fee_per_share=0.0,
            min_net_edge=min_net_edge,
        )

    ok, br = is_tradeable_after_fees(
        y,
        n,
        min_edge=min_edge,
        min_net_edge=min_net_edge,
        shares=shares,
        fee_cfg=cfg,
        as_taker=as_taker,
    )
    if not ok:
        return None
    return Opportunity(
        yes_ask=y,
        no_ask=n,
        pair_cost=br.pair_cost,
        edge=br.gross_edge,
        min_edge=min_edge,
        net_edge=br.net_edge,
        fee_usdc=br.fee_usdc,
        fee_per_share=br.fee_per_share,
        min_net_edge=min_net_edge,
    )


def compute_edge_breakdown(
    yes_ask: float,
    no_ask: float,
    *,
    shares: float = 1.0,
    fee_cfg: Optional[FeeConfig] = None,
    as_taker: Optional[bool] = None,
) -> EdgeBreakdown:
    """Always compute gross/net for logging (even when not tradeable)."""
    return edge_after_fees(
        yes_ask, no_ask, shares=shares, fee_cfg=fee_cfg, as_taker=as_taker
    )
