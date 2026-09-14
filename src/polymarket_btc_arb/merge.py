"""Merge Yes+No full sets back to USDC (CTF mergePositions).

After both legs fill, merge immediately — do not wait for resolution.
Dry-run stubs the call; live path uses optional SDK / documented interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass
class MergeResult:
    condition_id: str
    size: float
    success: bool
    dry_run: bool
    tx_hash: Optional[str] = None
    detail: str = ""


class MergeBackend(Protocol):
    def merge_positions(self, condition_id: str, size: float) -> MergeResult: ...


class DryRunMerger:
    def merge_positions(self, condition_id: str, size: float) -> MergeResult:
        msg = (
            f"[dry_run] merge condition_id={condition_id} size={size} "
            "→ would call CTF mergePositions / SDK merge"
        )
        logger.info(msg)
        return MergeResult(
            condition_id=condition_id,
            size=size,
            success=True,
            dry_run=True,
            detail=msg,
        )


class LiveMergeStub:
    """Interface for live merge via py-sdk / CTF / poly-web3.

    Real merge requires wallet + CTF approvals. Wire your preferred SDK here.
    Official flow (binary): mergePositions(collateral, 0x0, conditionId, [1,2], amount).
    Newer SDKs: client.merge_positions / merge_multiple_positions.
    """

    def __init__(self, client: Any = None) -> None:
        self.client = client

    def merge_positions(self, condition_id: str, size: float) -> MergeResult:
        if self.client is None:
            return MergeResult(
                condition_id=condition_id,
                size=size,
                success=False,
                dry_run=False,
                detail=(
                    "live merge client not configured — install polymarket py-sdk "
                    "or poly-web3 and inject client"
                ),
            )
        try:
            # Best-effort duck-typing across SDK versions
            if hasattr(self.client, "merge_positions"):
                handle = self.client.merge_positions(
                    condition_id=condition_id, amount=size
                )
            elif hasattr(self.client, "merge_multiple_positions"):
                handle = self.client.merge_multiple_positions(
                    positions=[{"condition_id": condition_id, "amount": size}]
                )
            else:
                raise RuntimeError("client has no merge method")
            tx = None
            if hasattr(handle, "wait"):
                outcome = handle.wait()
                tx = getattr(outcome, "transaction_hash", None) or str(outcome)
            elif isinstance(handle, dict):
                tx = handle.get("transactionHash") or handle.get("hash")
            return MergeResult(
                condition_id=condition_id,
                size=size,
                success=True,
                dry_run=False,
                tx_hash=str(tx) if tx else None,
                detail="live merge submitted",
            )
        except Exception as exc:
            logger.exception("merge failed")
            return MergeResult(
                condition_id=condition_id,
                size=size,
                success=False,
                dry_run=False,
                detail=str(exc),
            )


def merge_positions(
    condition_id: str,
    size: float,
    *,
    dry_run: bool = True,
    backend: Optional[MergeBackend] = None,
) -> MergeResult:
    """Public helper used by main loop."""
    if backend is None:
        backend = DryRunMerger() if dry_run else LiveMergeStub()
    return backend.merge_positions(condition_id, size)
