"""Both-legs-or-neither execution with unwind on one-sided fills.

Prefer maker/resting orders when configured. Document taker fee risk:
crossing the spread (taker) can erase thin edges via fees.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

from .arbitrage import Opportunity
from .risk import RiskManager

logger = logging.getLogger(__name__)


class LegSide(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass
class LegResult:
    side: LegSide
    token_id: str
    price: float
    size: float
    filled: bool
    order_id: Optional[str] = None
    fill_size: float = 0.0
    dry_run: bool = True
    error: Optional[str] = None


@dataclass
class ExecutionReport:
    opportunity: Opportunity
    size: float
    yes: LegResult
    no: LegResult
    both_filled: bool
    unwound: bool = False
    unwind_legs: list[LegResult] = field(default_factory=list)
    planned_actions: list[str] = field(default_factory=list)
    dry_run: bool = True
    aborted: bool = False
    abort_reason: Optional[str] = None


class OrderBackend(Protocol):
    def buy(
        self, token_id: str, price: float, size: float, *, maker: bool
    ) -> LegResult: ...

    def sell(
        self, token_id: str, price: float, size: float, *, maker: bool
    ) -> LegResult: ...


class DryRunBackend:
    """Simulates fills for paper trading against live quotes."""

    def __init__(self, fill: bool = True) -> None:
        self.fill = fill
        self.actions: list[str] = []

    def buy(
        self, token_id: str, price: float, size: float, *, maker: bool
    ) -> LegResult:
        oid = f"dry-buy-{uuid.uuid4().hex[:8]}"
        action = (
            f"BUY token={token_id[:12]}… price={price:.4f} size={size} "
            f"maker={maker} order_id={oid}"
        )
        self.actions.append(action)
        logger.info("[dry_run] %s", action)
        return LegResult(
            side=LegSide.YES,  # caller overwrites side
            token_id=token_id,
            price=price,
            size=size,
            filled=self.fill,
            order_id=oid,
            fill_size=size if self.fill else 0.0,
            dry_run=True,
        )

    def sell(
        self, token_id: str, price: float, size: float, *, maker: bool
    ) -> LegResult:
        oid = f"dry-sell-{uuid.uuid4().hex[:8]}"
        action = (
            f"SELL/UNWIND token={token_id[:12]}… price={price:.4f} size={size} "
            f"maker={maker} order_id={oid}"
        )
        self.actions.append(action)
        logger.info("[dry_run] %s", action)
        return LegResult(
            side=LegSide.YES,
            token_id=token_id,
            price=price,
            size=size,
            filled=True,
            order_id=oid,
            fill_size=size,
            dry_run=True,
        )


class LiveClobBackend:
    """Optional live path via py-clob-client when installed + key set.

    Taker fee risk: marketable (taker) orders may pay fees that wipe
    edges near min_edge. Prefer GTC maker posts when prefer_maker=True.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    def buy(
        self, token_id: str, price: float, size: float, *, maker: bool
    ) -> LegResult:
        return self._place("BUY", token_id, price, size, maker)

    def sell(
        self, token_id: str, price: float, size: float, *, maker: bool
    ) -> LegResult:
        return self._place("SELL", token_id, price, size, maker)

    def _place(
        self, side: str, token_id: str, price: float, size: float, maker: bool
    ) -> LegResult:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        try:
            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY if side == "BUY" else SELL,
            )
            signed = self.client.create_order(args)
            order_type = OrderType.GTC if maker else OrderType.FOK
            resp = self.client.post_order(signed, order_type)
            oid = None
            if isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("id")
            filled = True  # optimistic; production should poll fills
            return LegResult(
                side=LegSide.YES,
                token_id=token_id,
                price=price,
                size=size,
                filled=filled,
                order_id=str(oid) if oid else None,
                fill_size=size if filled else 0.0,
                dry_run=False,
            )
        except Exception as exc:
            logger.exception("live order failed")
            return LegResult(
                side=LegSide.YES,
                token_id=token_id,
                price=price,
                size=size,
                filled=False,
                dry_run=False,
                error=str(exc),
            )


@dataclass
class Executor:
    risk: RiskManager
    backend: OrderBackend
    require_both_legs: bool = True
    prefer_maker: bool = True

    def execute_pair(
        self,
        opportunity: Opportunity,
        yes_token_id: str,
        no_token_id: str,
        size: float,
        *,
        yes_unwind_bid: Optional[float] = None,
        no_unwind_bid: Optional[float] = None,
    ) -> ExecutionReport:
        """Buy YES and NO. If only one fills → immediately unwind naked leg."""
        size = self.risk.clamp_size(size)
        ok, reason = self.risk.allow_trade(size)
        planned: list[str] = []

        if not ok:
            logger.warning("trade blocked: %s", reason)
            empty = LegResult(
                side=LegSide.YES,
                token_id=yes_token_id,
                price=opportunity.yes_ask,
                size=size,
                filled=False,
                error=reason,
                dry_run=self.risk.dry_run,
            )
            empty_no = LegResult(
                side=LegSide.NO,
                token_id=no_token_id,
                price=opportunity.no_ask,
                size=size,
                filled=False,
                error=reason,
                dry_run=self.risk.dry_run,
            )
            return ExecutionReport(
                opportunity=opportunity,
                size=size,
                yes=empty,
                no=empty_no,
                both_filled=False,
                planned_actions=[f"ABORT: {reason}"],
                dry_run=self.risk.dry_run,
                aborted=True,
                abort_reason=reason,
            )

        maker = self.prefer_maker
        planned.append(
            f"BUY_YES@{opportunity.yes_ask:.4f} + BUY_NO@{opportunity.no_ask:.4f} "
            f"size={size} edge={opportunity.edge:.4f} maker={maker}"
        )
        # Taker fee note for operators
        if not maker:
            planned.append(
                "WARN: taker/FOK path — fees may erase edge; prefer_maker recommended"
            )

        yes = self.backend.buy(
            yes_token_id, opportunity.yes_ask, size, maker=maker
        )
        yes.side = LegSide.YES
        no = self.backend.buy(
            no_token_id, opportunity.no_ask, size, maker=maker
        )
        no.side = LegSide.NO

        if hasattr(self.backend, "actions"):
            planned.extend(getattr(self.backend, "actions"))

        both = bool(yes.filled and no.filled)
        report = ExecutionReport(
            opportunity=opportunity,
            size=size,
            yes=yes,
            no=no,
            both_filled=both,
            planned_actions=planned,
            dry_run=self.risk.dry_run,
        )

        if both:
            logger.info(
                "both legs filled size=%s pair_cost=%.4f edge=%.4f",
                size,
                opportunity.pair_cost,
                opportunity.edge,
            )
            return report

        if not self.require_both_legs:
            return report

        # BOTH LEGS OR NEITHER — unwind naked
        unwind: list[LegResult] = []
        if yes.filled and not no.filled:
            bid = yes_unwind_bid if yes_unwind_bid is not None else max(
                0.01, opportunity.yes_ask - 0.01
            )
            u = self.backend.sell(yes_token_id, bid, yes.fill_size or size, maker=False)
            u.side = LegSide.YES
            unwind.append(u)
            planned.append(f"UNWIND naked YES fill_size={yes.fill_size}")
            logger.error("one-sided YES fill — unwinding")
        elif no.filled and not yes.filled:
            bid = no_unwind_bid if no_unwind_bid is not None else max(
                0.01, opportunity.no_ask - 0.01
            )
            u = self.backend.sell(no_token_id, bid, no.fill_size or size, maker=False)
            u.side = LegSide.NO
            unwind.append(u)
            planned.append(f"UNWIND naked NO fill_size={no.fill_size}")
            logger.error("one-sided NO fill — unwinding")

        report.unwound = bool(unwind)
        report.unwind_legs = unwind
        report.planned_actions = planned
        return report
