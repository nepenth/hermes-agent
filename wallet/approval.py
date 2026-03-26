"""Wallet transaction approval — pending state and execution.

Mirrors the dangerous-command approval pattern in tools/approval.py
but for wallet transactions.  When wallet_send hits a ``require_approval``
policy verdict, the transaction details are stashed here.  The CLI or
gateway then prompts the user and calls ``execute_approved()`` to
actually send it.

Thread-safe: all state is guarded by a lock.
"""

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pending: dict[str, dict] = {}  # session_key → tx details


@dataclass
class PendingWalletTx:
    """A wallet transaction awaiting owner approval."""
    wallet_id: str
    chain: str
    from_address: str
    to_address: str
    amount: str           # Decimal as string
    symbol: str
    wallet_label: str
    wallet_type: str
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return f"Send {self.amount} {self.symbol} → {self.to_address} on {self.chain}"


def submit_pending(session_key: str, tx: PendingWalletTx) -> None:
    """Stash a transaction for user approval."""
    tx.timestamp = time.time()
    with _lock:
        _pending[session_key] = tx.to_dict()
    logger.info("Wallet tx pending approval [%s]: %s", session_key, tx.summary())


def pop_pending(session_key: str) -> Optional[dict]:
    """Retrieve and remove a pending wallet transaction."""
    with _lock:
        return _pending.pop(session_key, None)


def has_pending(session_key: str) -> bool:
    """Check if a session has a pending wallet transaction."""
    with _lock:
        return session_key in _pending


def execute_approved(session_key: str, pending: dict) -> str:
    """Execute an approved wallet transaction.

    Uses the shared wallet runtime so approvals go through the same provider
    configuration and persisted policy state as normal tool execution.
    """
    try:
        from wallet.runtime import get_runtime
        from wallet.policy import TxRequest, PolicyVerdict

        mgr, policy = get_runtime()
        if mgr is None:
            return json.dumps({"error": "Keystore is locked"})

        wallet_id = pending["wallet_id"]
        to_address = pending["to_address"]
        amount = Decimal(pending["amount"])

        tx_req = TxRequest(
            wallet_id=wallet_id,
            wallet_type=pending.get("wallet_type", "user"),
            chain=pending["chain"],
            to_address=to_address,
            amount=amount,
            symbol=pending["symbol"],
        )

        # Re-evaluate policies at execution time so freeze/cumulative limits
        # still apply. Approval only overrides the require_approval verdict.
        eval_result = policy.evaluate(tx_req)
        if eval_result.verdict == PolicyVerdict.BLOCK:
            return json.dumps({
                "status": "blocked",
                "error": eval_result.reason,
                "policy": eval_result.failed,
            })

        result = mgr.send(
            wallet_id,
            to_address,
            amount,
            decided_by="owner_approved",
            policy_result=json.dumps({
                "verdict": eval_result.verdict.value,
                "checked": eval_result.checked,
                "failed": eval_result.failed,
                "approved_via": "owner",
            }),
        )

        if result.status == "failed":
            return json.dumps({"status": "failed", "error": result.error})

        policy.record_transaction(tx_req)

        return json.dumps({
            "status": "submitted",
            "tx_hash": result.tx_hash,
            "explorer_url": result.explorer_url,
            "chain": result.chain,
            "amount": pending["amount"],
            "symbol": pending["symbol"],
            "to": to_address,
        })
    except Exception as e:
        logger.error("Failed to execute approved wallet tx: %s", e)
        return json.dumps({"error": f"Transaction execution failed: {e}"})
