"""Merge Yes+No full sets back to USDC (CTF mergePositions).

After both legs fill, merge immediately — do not wait for resolution.
Dry-run stubs the call; live path uses web3 CTF or SDK when available.

Required approvals (document for operators — see README):
  1. USDC (or pUSD) ERC-20 ``approve`` for CTF / collateral adapter as needed
     for split flows (merge returns collateral to you).
  2. Conditional Tokens ERC-1155 ``setApprovalForAll(operator, true)`` for:
       - CTF Exchange (trading) — usually done by Polymarket UI / py-clob-client
       - CTF contract / CtfCollateralAdapter when merging via adapter
  Polygon addresses (common):
    CTF:              0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
    USDC.e (legacy):  0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
    pUSD (V2):        0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
    CtfCollateralAdapter: 0xAdA100Db00Ca00073811820692005400218FcE1f

Official CTF call (binary CLOB tokens)::
    mergePositions(collateral, bytes32(0), conditionId, [1, 2], amount)
    amount is 6-decimal base units (1 share = 1_000_000).

Refs:
  https://github.com/Polymarket/conditional-token-examples-py/blob/main/ctf_examples/merge.py
  https://docs.polymarket.com/trading/positions/manage
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF_COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"

MERGE_POSITIONS_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "mergePositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


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


class LiveMerger:
    """Real merge via web3 CTF ``mergePositions`` or injected SDK client.

    Fails clearly when wallet / web3 / approvals are unavailable —
    never fakes success.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        private_key: Optional[str] = None,
        rpc_url: Optional[str] = None,
        collateral: str = USDC_E_ADDRESS,
        ctf_address: str = CTF_ADDRESS,
        use_adapter: bool = False,
        chain_id: int = 137,
    ) -> None:
        self.client = client
        self.private_key = private_key or os.environ.get("POLYMARKET_PRIVATE_KEY")
        self.rpc_url = (
            rpc_url
            or os.environ.get("POLYMARKET_RPC_URL")
            or os.environ.get("RPC_URL")
        )
        self.collateral = collateral
        self.ctf_address = CTF_COLLATERAL_ADAPTER if use_adapter else ctf_address
        self.use_adapter = use_adapter
        self.chain_id = chain_id

    def merge_positions(self, condition_id: str, size: float) -> MergeResult:
        # 1) Prefer duck-typed SDK on injected client
        if self.client is not None:
            sdk = self._try_sdk_merge(condition_id, size)
            if sdk is not None:
                return sdk

        # 2) Direct web3 CTF call
        if self.private_key and self.rpc_url:
            return self._web3_merge(condition_id, size)

        return MergeResult(
            condition_id=condition_id,
            size=size,
            success=False,
            dry_run=False,
            detail=(
                "live merge unavailable: need web3+RPC_URL+POLYMARKET_PRIVATE_KEY "
                "for CTF mergePositions, or an SDK client with merge_positions. "
                "Also ensure CTF ERC-1155 setApprovalForAll and collateral approvals "
                "are set (see README)."
            ),
        )

    def _try_sdk_merge(self, condition_id: str, size: float) -> Optional[MergeResult]:
        try:
            if hasattr(self.client, "merge_positions"):
                handle = self.client.merge_positions(
                    condition_id=condition_id, amount=size
                )
            elif hasattr(self.client, "merge_multiple_positions"):
                handle = self.client.merge_multiple_positions(
                    positions=[{"condition_id": condition_id, "amount": size}]
                )
            else:
                return None
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
                detail="live merge submitted via SDK client",
            )
        except Exception as exc:
            logger.exception("SDK merge failed")
            return MergeResult(
                condition_id=condition_id,
                size=size,
                success=False,
                dry_run=False,
                detail=f"SDK merge failed: {exc}",
            )

    def _web3_merge(self, condition_id: str, size: float) -> MergeResult:
        try:
            from web3 import Web3
        except ImportError:
            return MergeResult(
                condition_id=condition_id,
                size=size,
                success=False,
                dry_run=False,
                detail="web3 not installed — pip install web3 (optional live extra)",
            )

        try:
            w3 = Web3(Web3.HTTPProvider(self.rpc_url))
            if not w3.is_connected():
                return MergeResult(
                    condition_id=condition_id,
                    size=size,
                    success=False,
                    dry_run=False,
                    detail=f"RPC not connected: {self.rpc_url}",
                )
            acct = w3.eth.account.from_key(self.private_key)
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(self.ctf_address),
                abi=MERGE_POSITIONS_ABI,
            )
            amount = int(round(float(size) * 1_000_000))
            if amount <= 0:
                return MergeResult(
                    condition_id=condition_id,
                    size=size,
                    success=False,
                    dry_run=False,
                    detail="merge amount <= 0",
                )
            parent = b"\x00" * 32
            cond = condition_id
            if isinstance(cond, str):
                cond_hex = cond[2:] if cond.startswith("0x") else cond
                cond_bytes = bytes.fromhex(cond_hex)
            else:
                cond_bytes = cond

            collateral = Web3.to_checksum_address(
                PUSD_ADDRESS if self.use_adapter else self.collateral
            )
            fn = ctf.functions.mergePositions(
                collateral,
                parent,
                cond_bytes,
                [1, 2],
                amount,
            )
            tx = fn.build_transaction(
                {
                    "from": acct.address,
                    "nonce": w3.eth.get_transaction_count(acct.address),
                    "chainId": self.chain_id,
                    "gas": 500_000,
                }
            )
            # Let node fill gas price if strategy unavailable
            if "gasPrice" not in tx and "maxFeePerGas" not in tx:
                tx["gasPrice"] = w3.eth.gas_price
            signed = acct.sign_transaction(tx)
            raw = getattr(signed, "rawTransaction", None) or getattr(
                signed, "raw_transaction", None
            )
            tx_hash = w3.eth.send_raw_transaction(raw)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            ok = receipt.status == 1
            return MergeResult(
                condition_id=condition_id,
                size=size,
                success=ok,
                dry_run=False,
                tx_hash=tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash),
                detail=(
                    "CTF mergePositions mined"
                    if ok
                    else "CTF mergePositions reverted — check approvals / balances"
                ),
            )
        except Exception as exc:
            logger.exception("web3 merge failed")
            return MergeResult(
                condition_id=condition_id,
                size=size,
                success=False,
                dry_run=False,
                detail=f"web3 merge failed: {exc}",
            )


# Back-compat alias
class LiveMergeStub(LiveMerger):
    """Deprecated name — use LiveMerger. Same fail-closed behavior."""


def merge_positions(
    condition_id: str,
    size: float,
    *,
    dry_run: bool = True,
    backend: Optional[MergeBackend] = None,
) -> MergeResult:
    """Public helper used by main loop."""
    if backend is None:
        backend = DryRunMerger() if dry_run else LiveMerger()
    return backend.merge_positions(condition_id, size)
