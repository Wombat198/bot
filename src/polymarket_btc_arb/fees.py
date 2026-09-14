"""Polymarket CLOB taker fee curve (crypto markets).

Official formula (docs):
    fee = C × feeRate × p × (1 − p)

where C = shares traded, p = share price in [0, 1], fee in USDC.
Rounded to 5 decimal places; amounts below 0.00001 USDC round to zero.

Sources:
  - https://docs.polymarket.com/trading/fees
  - https://help.polymarket.com/en/articles/13364478-trading-fees
  - https://docs.polymarket.com/v2-migration (fd.r / fd.e / fd.to via getClobMarketInfo)

Category defaults (Crypto for BTC Up/Down):
  taker feeRate = 0.07, maker feeRate = 0, maker rebate pool share = 20%.

Uncertainty / overrides:
  - Market-level ``fd.e`` (curve exponent) appears in CLOB market info; the
    public fee tables for Crypto match the simple p×(1−p) form with rate 0.07
    (no extra exponent). We implement that documented formula.
  - Optional config ``fees.exponent`` (default 1.0) applies
    ``(p*(1-p))**exponent`` if operators want to experiment; leave at 1.0.
  - Maker rebates are paid daily from the rebate pool and are **not** netted
    into the tradeable edge by default (``fees.apply_maker_rebate=false``).
  - Override ``fees.taker_rate`` / ``fees.maker_rebate`` in YAML when markets
    differ or docs change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# Crypto category default from https://docs.polymarket.com/trading/fees
DEFAULT_TAKER_RATE = 0.07
DEFAULT_MAKER_REBATE = 0.20  # pool share; not a per-trade credit by default
FEE_PRECISION = 5
MIN_FEE_USDC = 1e-5


@dataclass(frozen=True)
class FeeConfig:
    taker_rate: float = DEFAULT_TAKER_RATE
    maker_rebate: float = DEFAULT_MAKER_REBATE
    exponent: float = 1.0
    assume_taker: bool = True
    apply_maker_rebate: bool = False
    category: str = "crypto"

    @classmethod
    def from_mapping(cls, raw: Optional[dict[str, Any]]) -> "FeeConfig":
        d = raw or {}
        return cls(
            taker_rate=float(d.get("taker_rate", DEFAULT_TAKER_RATE)),
            maker_rebate=float(d.get("maker_rebate", DEFAULT_MAKER_REBATE)),
            exponent=float(d.get("exponent", 1.0)),
            assume_taker=bool(d.get("assume_taker", True)),
            apply_maker_rebate=bool(d.get("apply_maker_rebate", False)),
            category=str(d.get("category", "crypto")),
        )


def round_fee(fee: float) -> float:
    """Round to 5 decimals; clamp tiny positive dust to 0."""
    if fee <= 0:
        return 0.0
    rounded = round(fee, FEE_PRECISION)
    return 0.0 if rounded < MIN_FEE_USDC else rounded


def taker_fee_usdc(
    shares: float,
    price: float,
    fee_rate: float = DEFAULT_TAKER_RATE,
    *,
    exponent: float = 1.0,
) -> float:
    """USDC taker fee for ``shares`` at price ``p``.

    fee = C × feeRate × (p × (1 − p)) ** exponent
    With exponent=1 this is the documented Crypto curve.
    """
    c = float(shares)
    p = float(price)
    if c <= 0 or p <= 0 or p >= 1:
        return 0.0
    base = p * (1.0 - p)
    if exponent != 1.0:
        base = base**exponent
    return round_fee(c * float(fee_rate) * base)


def pair_taker_fees_usdc(
    yes_ask: float,
    no_ask: float,
    shares: float,
    fee_rate: float = DEFAULT_TAKER_RATE,
    *,
    exponent: float = 1.0,
) -> float:
    """Sum of taker fees on both Yes and No buys."""
    return taker_fee_usdc(
        shares, yes_ask, fee_rate, exponent=exponent
    ) + taker_fee_usdc(shares, no_ask, fee_rate, exponent=exponent)


@dataclass(frozen=True)
class EdgeBreakdown:
    gross_edge: float
    fee_usdc: float
    fee_per_share: float
    net_edge: float
    pair_cost: float
    assume_taker: bool


def edge_after_fees(
    yes_ask: float,
    no_ask: float,
    *,
    shares: float = 1.0,
    fee_cfg: Optional[FeeConfig] = None,
    as_taker: Optional[bool] = None,
) -> EdgeBreakdown:
    """Gross ask-sum edge vs net edge after modeled fees.

    Gross: ``1 - (yes_ask + no_ask)``.
    If assuming taker: subtract fee_usdc / shares.
    If maker path and ``apply_maker_rebate``: optionally credit
    ``maker_rebate * hypothetical_taker_fee`` (off by default — rebates are
    daily pool distributions, not guaranteed per fill).
    """
    cfg = fee_cfg or FeeConfig()
    y, n = float(yes_ask), float(no_ask)
    pair = y + n
    gross = 1.0 - pair
    take = cfg.assume_taker if as_taker is None else bool(as_taker)
    shares = max(float(shares), 1e-12)

    hypo_fee = pair_taker_fees_usdc(
        y, n, shares, cfg.taker_rate, exponent=cfg.exponent
    )

    if take:
        fee = hypo_fee
    elif cfg.apply_maker_rebate:
        # Optimistic credit only when explicitly enabled
        fee = -hypo_fee * cfg.maker_rebate
    else:
        fee = 0.0

    fee_ps = fee / shares
    net = gross - fee_ps
    return EdgeBreakdown(
        gross_edge=gross,
        fee_usdc=fee,
        fee_per_share=fee_ps,
        net_edge=net,
        pair_cost=pair,
        assume_taker=take,
    )


def is_tradeable_after_fees(
    yes_ask: float,
    no_ask: float,
    *,
    min_edge: float,
    min_net_edge: Optional[float] = None,
    shares: float = 1.0,
    fee_cfg: Optional[FeeConfig] = None,
    as_taker: Optional[bool] = None,
) -> tuple[bool, EdgeBreakdown]:
    """Gap is tradeable iff pair < 1 and net_edge >= threshold.

    Threshold is ``min_net_edge`` if set, else ``min_edge``.
    """
    br = edge_after_fees(
        yes_ask, no_ask, shares=shares, fee_cfg=fee_cfg, as_taker=as_taker
    )
    floor = float(min_edge if min_net_edge is None else min_net_edge)
    ok = br.pair_cost < 1.0 and br.net_edge >= floor
    return ok, br
