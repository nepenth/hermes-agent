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

    Called by the CLI callback or gateway /approve handler after the user
    approves.  Returns a JSON string with the result.
    """
    try:
        from keystore.client import get_keystore
        from wallet.manager import WalletManager
        from wallet.policy import PolicyEngine

        ks = get_keystore()
        if not ks.is_unlocked:
            return json.dumps({"error": "Keystore is locked"})

        mgr = WalletManager(ks)

        # Register providers
        try:
            from wallet.chains.evm import EVMProvider, EVM_CHAINS
            for chain_id, config in EVM_CHAINS.items():
                mgr.register_provider(chain_id, EVMProvider(config))
        except ImportError:
            pass
        try:
            from wallet.chains.solana import SolanaProvider, SOLANA_CHAINS
            for chain_id, config in SOLANA_CHAINS.items():
                mgr.register_provider(chain_id, SolanaProvider(config))
        except ImportError:
            pass

        wallet_id = pending["wallet_id"]
        to_address = pending["to_address"]
        amount = Decimal(pending["amount"])

        result = mgr.send(wallet_id, to_address, amount, decided_by="owner_approved")

        if result.status == "failed":
            return json.dumps({"status": "failed", "error": result.error})

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
