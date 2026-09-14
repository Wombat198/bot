"""Yes+No ask-sum gap detection.

Binary markets: owning one Yes + one No pays exactly $1 at resolution.
Opportunity when YES_ASK + NO_ASK < 1.0 and edge >= min_edge (default 0.02).
Uses ASK prices only — never bid or mid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Opportunity:
    yes_ask: float
    no_ask: float
    pair_cost: float
    edge: float
    min_edge: float

    @property
    def is_actionable(self) -> bool:
        return self.edge >= self.min_edge and self.pair_cost < 1.0


def find_opportunity(
    yes_ask: Optional[float],
    no_ask: Optional[float],
    min_edge: float = 0.02,
) -> Optional[Opportunity]:
    """Return an Opportunity if asks sum below 1.0 by at least min_edge.

    Parameters
    ----------
    yes_ask, no_ask:
        Best ask prices in [0, 1]. None / non-positive / missing → no opp.
    min_edge:
        Minimum required edge = 1 - (yes_ask + no_ask). Default 0.02.
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

    pair_cost = y + n
    edge = 1.0 - pair_cost
    # Strict: pair must cost less than $1, and edge must meet floor
    if pair_cost >= 1.0 or edge < min_edge:
        return None
    return Opportunity(
        yes_ask=y,
        no_ask=n,
        pair_cost=pair_cost,
        edge=edge,
        min_edge=min_edge,
    )
