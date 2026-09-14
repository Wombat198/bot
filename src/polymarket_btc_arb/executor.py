"""Both-legs-or-neither execution with unwind on one-sided fills.

Prefer maker/resting orders when configured. Document taker fee risk:
crossing the spread (taker) can erase thin edges via fees.

Live path polls order status / trades until filled, cancelled, or timeout —
never assumes optimistic fill=True.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

from .arbitrage import Opportunity
from .risk import RiskManager

logger = logging.getLogger(__name__)

# Terminal-ish statuses from CLOB open-order / post_order responses
_FILLED_STATUSES = {
    "matched",
    "filled",
    "order_status_matched",
    "trade_status_matched",
    "trade_status_mined",
    "trade_status_confirmed",
}
_CANCEL_STATUSES = {
    "canceled",
    "cancelled",
    "order_status_canceled",
    "order_status_canceled_market_resolved",
    "order_status_invalid",
    "rejected",
    "expired",
}


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
    status: Optional[str] = None


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


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return dict(vars(obj))
    return {}


def parse_post_order_fill(resp: Any, requested_size: float) -> tuple[bool, float, Optional[str], Optional[str]]:
    """Parse immediate post_order response into (filled, fill_size, order_id, status).

    Does **not** invent a full fill — only trusts explicit matched amounts / status.
    """
    d = _as_dict(resp)
    oid = d.get("orderID") or d.get("order_id") or d.get("id")
    status = str(d.get("status") or "").lower()
    fill_size = 0.0

    # takingAmount on BUY is shares received when buying outcome tokens
    for key in ("takingAmount", "taking_amount", "size_matched", "sizeMatched"):
        if d.get(key) is not None:
            try:
                fill_size = max(fill_size, float(d[key]))
            except (TypeError, ValueError):
                pass

    success_flag = d.get("success")
    if status in _FILLED_STATUSES or (
        success_flag is True and fill_size > 0 and status in ("", "live", "matched")
    ):
        if fill_size <= 0:
            # matched but no size field — treat as full only when status is matched
            if status in _FILLED_STATUSES:
                fill_size = float(requested_size)
        filled = fill_size > 0
        return filled, fill_size if filled else 0.0, str(oid) if oid else None, status or None

    if status in _CANCEL_STATUSES:
        return False, fill_size, str(oid) if oid else None, status

    # Live/open — not yet confirmed
    return False, fill_size, str(oid) if oid else None, status or None


def confirm_order_fill(
    client: Any,
    order_id: Optional[str],
    requested_size: float,
    *,
    timeout_sec: float = 3.0,
    poll_interval: float = 0.25,
    initial_resp: Any = None,
) -> tuple[bool, float, Optional[str]]:
    """Poll get_order / get_trades until filled, cancelled, or timeout.

    Returns (filled, fill_size, final_status).
    """
    filled, fill_size, oid, status = parse_post_order_fill(
        initial_resp, requested_size
    )
    if order_id:
        oid = order_id
    if filled and fill_size > 0:
        return True, fill_size, status

    if not oid or client is None:
        return False, fill_size, status

    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    last_status = status

    while time.monotonic() < deadline:
        # 1) get_order (active orders)
        try:
            if hasattr(client, "get_order"):
                order = client.get_order(oid)
                od = _as_dict(order)
                if od:
                    st = str(od.get("status") or "").lower()
                    last_status = st or last_status
                    matched = od.get("size_matched") or od.get("sizeMatched") or 0
                    try:
                        matched_f = float(matched)
                        # Some APIs return 6-decimal fixed ints
                        if matched_f > requested_size * 10:
                            matched_f = matched_f / 1_000_000.0
                        fill_size = max(fill_size, matched_f)
                    except (TypeError, ValueError):
                        pass
                    if st in _FILLED_STATUSES or (
                        fill_size > 0
                        and fill_size + 1e-9 >= float(requested_size) * 0.999
                    ):
                        return True, fill_size, st
                    if st in _CANCEL_STATUSES:
                        return fill_size > 0, fill_size, st
        except Exception as exc:
            logger.debug("get_order poll: %s", exc)

        # 2) get_trades fallback (filled orders often leave get_order)
        try:
            if hasattr(client, "get_trades"):
                trades = client.get_trades()
                if isinstance(trades, dict):
                    trades = trades.get("data") or trades.get("trades") or []
                for t in trades or []:
                    td = _as_dict(t)
                    tid = (
                        td.get("taker_order_id")
                        or td.get("takerOrderId")
                        or td.get("order_id")
                        or td.get("id")
                    )
                    if str(tid) != str(oid):
                        # also check maker_orders list
                        makers = td.get("maker_orders") or td.get("makerOrders") or []
                        hit = any(
                            str(_as_dict(m).get("order_id") or _as_dict(m).get("id"))
                            == str(oid)
                            for m in makers
                        )
                        if not hit:
                            continue
                    sz = td.get("size") or td.get("matched_amount") or 0
                    try:
                        sf = float(sz)
                        if sf > requested_size * 10:
                            sf = sf / 1_000_000.0
                        fill_size = max(fill_size, sf)
                    except (TypeError, ValueError):
                        pass
                    st = str(td.get("status") or "matched").lower()
                    last_status = st
                    if fill_size > 0:
                        return True, fill_size, st
        except Exception as exc:
            logger.debug("get_trades poll: %s", exc)

        time.sleep(poll_interval)

    # Timeout: cancel resting remainder if possible
    if oid and hasattr(client, "cancel"):
        try:
            client.cancel(oid)
            last_status = last_status or "timeout_cancelled"
        except Exception:
            try:
                if hasattr(client, "cancel_order"):
                    client.cancel_order(oid)
                    last_status = last_status or "timeout_cancelled"
            except Exception as exc:
                logger.debug("cancel on timeout failed: %s", exc)

    return fill_size > 0, fill_size, last_status or "timeout"


class DryRunBackend:
    """Simulates fills for paper trading against live quotes.

    Goes through the same LegResult report shape as live.
    """

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
            status="matched" if self.fill else "canceled",
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
            status="matched",
        )


class LiveClobBackend:
    """Live path via py-clob-client with real fill confirmation.

    Taker fee risk: marketable (taker) orders may pay fees that wipe
    edges near min_edge. Prefer GTC maker posts when prefer_maker=True.
    """

    def __init__(
        self,
        client: Any,
        *,
        fill_timeout_sec: float = 3.0,
        poll_interval_sec: float = 0.25,
    ) -> None:
        self.client = client
        self.fill_timeout_sec = float(fill_timeout_sec)
        self.poll_interval_sec = float(poll_interval_sec)

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
            filled_now, fill_now, oid, status = parse_post_order_fill(resp, size)
            if not filled_now:
                filled_now, fill_now, status = confirm_order_fill(
                    self.client,
                    oid,
                    size,
                    timeout_sec=self.fill_timeout_sec,
                    poll_interval=self.poll_interval_sec,
                    initial_resp=resp,
                )
            return LegResult(
                side=LegSide.YES,
                token_id=token_id,
                price=price,
                size=size,
                filled=bool(filled_now and fill_now > 0),
                order_id=str(oid) if oid else None,
                fill_size=float(fill_now) if filled_now else 0.0,
                dry_run=False,
                status=status,
            )
        except Exception as exc:
            logger.exception("live order failed")
            return LegResult(
                side=LegSide.YES,
                token_id=token_id,
                price=price,
                size=size,
                filled=False,
                fill_size=0.0,
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
        """Buy YES and NO. If only one fills → immediately unwind naked leg.

        Unwind size uses actual ``fill_size``, never the optimistic request size
        when fill_size is known.
        """
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
            f"size={size} gross_edge={opportunity.edge:.4f} "
            f"net_edge={getattr(opportunity, 'net_edge', opportunity.edge):.4f} "
            f"maker={maker}"
        )
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
        # Paired size for merge = min of actual fills when both filled
        paired_size = size
        if both:
            paired_size = min(
                yes.fill_size or size,
                no.fill_size or size,
            )

        report = ExecutionReport(
            opportunity=opportunity,
            size=paired_size if both else size,
            yes=yes,
            no=no,
            both_filled=both,
            planned_actions=planned,
            dry_run=self.risk.dry_run,
        )

        if both:
            logger.info(
                "both legs filled size=%s pair_cost=%.4f gross_edge=%.4f net_edge=%.4f",
                paired_size,
                opportunity.pair_cost,
                opportunity.edge,
                getattr(opportunity, "net_edge", opportunity.edge),
            )
            # If fills unequal, unwind the excess on the larger leg
            y_fill = yes.fill_size or 0.0
            n_fill = no.fill_size or 0.0
            if abs(y_fill - n_fill) > 1e-9:
                excess_side = LegSide.YES if y_fill > n_fill else LegSide.NO
                excess = abs(y_fill - n_fill)
                if excess_side == LegSide.YES and excess > 0:
                    bid = yes_unwind_bid if yes_unwind_bid is not None else max(
                        0.01, opportunity.yes_ask - 0.01
                    )
                    u = self.backend.sell(
                        yes_token_id, bid, excess, maker=False
                    )
                    u.side = LegSide.YES
                    report.unwind_legs.append(u)
                    report.unwound = True
                    planned.append(f"UNWIND excess YES size={excess}")
                elif excess > 0:
                    bid = no_unwind_bid if no_unwind_bid is not None else max(
                        0.01, opportunity.no_ask - 0.01
                    )
                    u = self.backend.sell(
                        no_token_id, bid, excess, maker=False
                    )
                    u.side = LegSide.NO
                    report.unwind_legs.append(u)
                    report.unwound = True
                    planned.append(f"UNWIND excess NO size={excess}")
                report.planned_actions = planned
            return report

        if not self.require_both_legs:
            return report

        # BOTH LEGS OR NEITHER — unwind naked using actual fill_size only
        unwind: list[LegResult] = []
        if yes.filled and not no.filled:
            unwind_sz = yes.fill_size if yes.fill_size > 0 else 0.0
            if unwind_sz > 0:
                bid = yes_unwind_bid if yes_unwind_bid is not None else max(
                    0.01, opportunity.yes_ask - 0.01
                )
                u = self.backend.sell(
                    yes_token_id, bid, unwind_sz, maker=False
                )
                u.side = LegSide.YES
                unwind.append(u)
                planned.append(f"UNWIND naked YES fill_size={unwind_sz}")
                logger.error("one-sided YES fill — unwinding size=%s", unwind_sz)
        elif no.filled and not yes.filled:
            unwind_sz = no.fill_size if no.fill_size > 0 else 0.0
            if unwind_sz > 0:
                bid = no_unwind_bid if no_unwind_bid is not None else max(
                    0.01, opportunity.no_ask - 0.01
                )
                u = self.backend.sell(
                    no_token_id, bid, unwind_sz, maker=False
                )
                u.side = LegSide.NO
                unwind.append(u)
                planned.append(f"UNWIND naked NO fill_size={unwind_sz}")
                logger.error("one-sided NO fill — unwinding size=%s", unwind_sz)

        report.unwound = bool(unwind)
        report.unwind_legs = unwind
        report.planned_actions = planned
        return report
