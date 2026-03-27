"""Shared wallet runtime.

Provides a single configured WalletManager + PolicyEngine per process so all
entry points (CLI, tools, approvals, gateway) share the same provider setup,
policy configuration, and persisted state.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_runtime: Optional[tuple] = None


def _hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))


def _wallet_state_dir() -> Path:
    p = _hermes_home() / "wallet"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_wallet_config() -> dict:
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        return cfg.get("wallet", {}) or {}
    except Exception:
        return {}


def _policy_overrides_from_config(wallet_cfg: dict) -> dict:
    # Documented config is not yet fully per-wallet. Support a minimal global map now.
    overrides = wallet_cfg.get("policies", {}) or {}
    agent_cfg = wallet_cfg.get("agent_wallet", {}) or {}
    # Map a few friendly config keys into policy override shape.
    mapped = dict(overrides)
    if agent_cfg.get("max_per_tx_native") is not None:
        mapped.setdefault("spending_limit", {})["max_native"] = str(agent_cfg["max_per_tx_native"])
    if agent_cfg.get("daily_limit_native") is not None:
        mapped.setdefault("daily_limit", {})["max_native"] = str(agent_cfg["daily_limit_native"])
    if agent_cfg.get("auto_approve_below_native") is not None:
        mapped.setdefault("require_approval", {})["above_native"] = str(agent_cfg["auto_approve_below_native"])
    return mapped


def get_runtime():
    global _runtime
    if _runtime is not None:
        return _runtime

    from keystore.client import get_keystore
    from wallet.manager import WalletManager
    from wallet.policy import PolicyEngine

    ks = get_keystore()
    if not ks.is_unlocked:
        try:
            if not ks.ensure_unlocked(interactive=False):
                return None, None
        except Exception:
            return None, None

    mgr = WalletManager(ks, state_dir=_wallet_state_dir())
    wallet_cfg = _load_wallet_config()
    policy = PolicyEngine(
        policies=_policy_overrides_from_config(wallet_cfg),
        state_path=_wallet_state_dir() / "policy_state.json",
    )

    rpc_overrides = wallet_cfg.get("rpc_endpoints", {}) or {}

    try:
        from wallet.chains.evm import EVMProvider, EVM_CHAINS
        for chain_id, config in EVM_CHAINS.items():
            mgr.register_provider(chain_id, EVMProvider(config, rpc_url_override=rpc_overrides.get(chain_id, "")))
    except ImportError:
        pass

    try:
        from wallet.chains.solana import SolanaProvider, SOLANA_CHAINS
        for chain_id, config in SOLANA_CHAINS.items():
            mgr.register_provider(chain_id, SolanaProvider(config, rpc_url_override=rpc_overrides.get(chain_id, "")))
    except ImportError:
        pass

    _runtime = (mgr, policy)
    return _runtime


def reset_runtime() -> None:
    global _runtime
    _runtime = None
