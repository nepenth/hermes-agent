# Wallet & Keystore

## Overview

Hermes Agent includes an optional crypto wallet with an encrypted keystore. The agent can hold funds, check balances, and send native tokens on Solana and EVM chains — with policy-controlled spending limits and owner approval for transactions.

## Install

```bash
pip install 'hermes-agent[wallet]'          # EVM chains
pip install 'hermes-agent[wallet-solana]'    # + Solana
```

## Quick Start

```bash
hermes keystore init                         # Set master passphrase
hermes wallet create --chain solana          # Create wallet
hermes wallet fund                           # Show deposit address
hermes wallet balance                        # Check balance
```

Enable the `wallet` toolset in `config.yaml` or via `hermes chat -t hermes-cli,wallet`.

## Wallet CLI

| Command | Description |
|---------|-------------|
| `hermes wallet create --chain <chain>` | Create a fresh user wallet |
| `hermes wallet create-agent --chain <chain>` | Create agent wallet (auto-approve within limits) |
| `hermes wallet import --chain <chain>` | Import from exported private key |
| `hermes wallet export` | Export private key for migration |
| `hermes wallet list` | List wallets + balances |
| `hermes wallet balance` | Check balance |
| `hermes wallet send <to> <amount>` | Send tokens (interactive confirmation) |
| `hermes wallet fund` | Show deposit address |
| `hermes wallet history` | Transaction history |
| `hermes wallet freeze` | Kill switch — block everything |
| `hermes wallet unfreeze` | Resume after freeze |
| `hermes wallet status` | Wallet overview |

## Keystore CLI

| Command | Description |
|---------|-------------|
| `hermes keystore init` | Create encrypted keystore |
| `hermes keystore list` | List secrets (names only) |
| `hermes keystore set <name>` | Add/update a secret |
| `hermes keystore show <name>` | Decrypt and display |
| `hermes keystore delete <name>` | Remove a secret |
| `hermes keystore migrate` | Import from `.env` |
| `hermes keystore remember` | Cache passphrase in OS credential store |
| `hermes keystore forget` | Remove cached passphrase |
| `hermes keystore change-passphrase` | Re-encrypt everything |
| `hermes keystore audit` | Access log |

## Agent Tools

| Tool | Description |
|------|-------------|
| `wallet_list` | List wallets + balances |
| `wallet_balance` | Check specific balance |
| `wallet_address` | Get deposit address |
| `wallet_send` | Send tokens (policy-gated) |
| `wallet_estimate_gas` | Fee estimation |
| `wallet_history` | Transaction log |
| `wallet_networks` | Supported chains |

## Security

- **Encryption:** Argon2id KDF + XChaCha20-Poly1305 per-secret AEAD
- **Agent never sees keys:** Private keys are `sealed` — the agent uses tools, not keys
- **Policies:** Spending limits, rate limits, daily caps, approval thresholds, recipient lists
- **User wallets:** Every transaction requires owner approval
- **Agent wallets:** Auto-approve within limits, escalate above threshold
- **Kill switch:** `hermes wallet freeze` — instant, no exceptions

## Supported Chains

**Mainnet:** Ethereum, Base, Polygon, Arbitrum, Optimism, Solana  
**Testnet:** Ethereum Sepolia, Base Sepolia, Solana Devnet

Custom RPC endpoints via `wallet.rpc_endpoints` in `config.yaml`.
