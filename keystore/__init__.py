"""hermes-keystore — encrypted secret store for Hermes Agent.

Provides an encrypted SQLite-backed secret store with per-secret
AEAD encryption (XChaCha20-Poly1305), a master key derived from
a user passphrase via Argon2id, cross-platform credential caching,
and secret categorisation (injectable / gated / sealed / user_only).

Architecture:
    keystore/store.py            — core encrypted store
    keystore/credential_store.py — cross-platform passphrase caching
    keystore/client.py           — high-level API (unlock, inject, get)
    keystore/categories.py       — secret category definitions
    keystore/migrations.py       — DB schema migrations
    keystore/cli.py              — `hermes keystore` subcommands
"""
