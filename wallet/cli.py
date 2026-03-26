"""CLI subcommands for ``hermes wallet``.

Provides:
    hermes wallet create       — Create a new wallet (fresh keypair)
    hermes wallet create-agent — Create an agent wallet (auto-approve within policy)
    hermes wallet import       — Import wallet from exported private key (migration)
    hermes wallet export       — Export private key for migration to another machine
    hermes wallet list         — List wallets
    hermes wallet balance      — Check balance
    hermes wallet send         — Send tokens (interactive approval)
    hermes wallet fund         — Show deposit address
    hermes wallet history      — Transaction history
    hermes wallet freeze       — Kill switch
    hermes wallet unfreeze     — Resume operations
    hermes wallet status       — Show wallet status
"""

import argparse
import getpass
import json
import sys
from decimal import Decimal, InvalidOperation

try:
    from rich.console import Console
    from rich.table import Table
    _RICH = True
except ImportError:
    _RICH = False


def _cprint(msg: str, style: str = "") -> None:
    if _RICH:
        Console().print(msg, style=style)
    else:
        print(msg)


def _get_wallet_manager():
    """Initialize and return the shared wallet manager + policy engine."""
    try:
        from wallet.runtime import get_runtime
        mgr, policy = get_runtime()
        if mgr is None:
            from keystore.store import KeystoreLocked
            raise KeystoreLocked("Keystore is locked")
        return mgr, policy
    except ImportError as e:
        _cprint(f"\n  ✗ Wallet dependencies not installed: {e}", style="bold red")
        _cprint("    Install with: pip install 'hermes-agent[wallet]'\n")
        sys.exit(1)


# =========================================================================
# Subcommand handlers
# =========================================================================

def cmd_wallet_create(args: argparse.Namespace) -> None:
    """Create a new wallet."""
    mgr, _ = _get_wallet_manager()
    chain = args.chain
    label = args.label or ""

    if chain not in mgr.supported_chains:
        _cprint(f"\n  ✗ Unsupported chain: {chain}", style="bold red")
        _cprint(f"    Available: {', '.join(mgr.supported_chains)}\n")
        return

    wallet = mgr.create_wallet(chain=chain, label=label, wallet_type="user")

    _cprint(f"\n  ✓ Wallet created!", style="bold green")
    _cprint(f"    Label:   {wallet.label}")
    _cprint(f"    Chain:   {wallet.chain}")
    _cprint(f"    Address: {wallet.address}")
    _cprint(f"    ID:      {wallet.wallet_id}")
    _cprint(f"\n  💡 Fund this wallet by sending tokens to the address above.")
    _cprint(f"     This is a fresh wallet — send it some tokens to get started.")
    _cprint(f"     All transactions from this wallet require your approval.\n")


def cmd_wallet_create_agent(args: argparse.Namespace) -> None:
    """Create an agent wallet with auto-approve policies."""
    mgr, _ = _get_wallet_manager()
    chain = args.chain
    label = args.label or "Agent Wallet"

    if chain not in mgr.supported_chains:
        _cprint(f"\n  ✗ Unsupported chain: {chain}", style="bold red")
        return

    wallet = mgr.create_wallet(chain=chain, label=label, wallet_type="agent")

    _cprint(f"\n  ✓ Agent wallet created!", style="bold green")
    _cprint(f"    Label:   {wallet.label}")
    _cprint(f"    Chain:   {wallet.chain}")
    _cprint(f"    Address: {wallet.address}")
    _cprint(f"    ID:      {wallet.wallet_id}")
    _cprint(f"\n  ⚠️  Agent wallets auto-approve transactions within policy limits.")
    _cprint(f"     Default: max 1.0 {mgr.get_provider(chain).config.symbol}/tx, 5.0/day\n")


def cmd_wallet_import(args: argparse.Namespace) -> None:
    """Import a wallet from an exported private key (for migration)."""
    mgr, _ = _get_wallet_manager()
    chain = args.chain
    label = args.label or ""
    wallet_type = args.type or "user"

    _cprint("\n  📦 Import Wallet")
    _cprint("  This is for migrating a wallet from another Hermes install.")
    _cprint("  Use 'hermes wallet export' on the source machine first.\n")

    private_key = getpass.getpass("  Private key (hidden): ")
    if not private_key:
        _cprint("\n  ✗ Cancelled\n", style="yellow")
        return

    try:
        wallet = mgr.import_wallet(
            chain=chain, private_key=private_key.strip(),
            label=label, wallet_type=wallet_type,
        )
        _cprint(f"\n  ✓ Wallet imported!", style="bold green")
        _cprint(f"    Label:   {wallet.label}")
        _cprint(f"    Chain:   {wallet.chain}")
        _cprint(f"    Address: {wallet.address}")
        _cprint(f"    Type:    {wallet.wallet_type}")
        _cprint(f"    ID:      {wallet.wallet_id}\n")
    except Exception as e:
        _cprint(f"\n  ✗ Import failed: {e}\n", style="bold red")


def cmd_wallet_export(args: argparse.Namespace) -> None:
    """Export a wallet's private key for migration to another machine."""
    mgr, _ = _get_wallet_manager()

    wallet_id = args.wallet_id
    chain = args.chain

    try:
        wallet = mgr.resolve_wallet(wallet_id=wallet_id, chain=chain)
    except Exception as e:
        _cprint(f"\n  ✗ {e}\n", style="bold red")
        return

    _cprint(f"\n  ⚠️  Export Private Key: {wallet.label}")
    _cprint(f"     Chain:   {wallet.chain}")
    _cprint(f"     Address: {wallet.address}")
    _cprint(f"\n     This will display the private key in your terminal.")
    _cprint(f"     Anyone with this key has FULL control of this wallet.")
    _cprint(f"     Make sure nobody is watching your screen.\n")

    # Require passphrase re-entry as confirmation
    passphrase = getpass.getpass("  Re-enter keystore passphrase to confirm: ")
    if not passphrase:
        _cprint("\n  ✗ Cancelled\n", style="yellow")
        return

    # Verify passphrase
    try:
        from keystore.client import get_keystore
        ks = get_keystore()
        # Quick verify by attempting to re-derive (the store validates on unlock)
        from keystore.store import EncryptedStore
        test_store = EncryptedStore(ks._store._db_path)
        test_store.unlock(passphrase)
        test_store.lock()
    except Exception:
        _cprint("\n  ✗ Incorrect passphrase\n", style="bold red")
        return

    try:
        private_key = mgr.export_private_key(wallet.wallet_id)
        _cprint(f"\n  Private key for {wallet.label}:")
        _cprint(f"  {private_key}\n", style="bold")
        _cprint(f"  To import on another machine:")
        _cprint(f"    hermes wallet import --chain {wallet.chain} --type {wallet.wallet_type}")
        _cprint(f"\n  ⚠️  This key will not be shown again. Copy it now.\n")
    except Exception as e:
        _cprint(f"\n  ✗ Export failed: {e}\n", style="bold red")


def cmd_wallet_list(args: argparse.Namespace) -> None:
    """List all wallets."""
    mgr, _ = _get_wallet_manager()
    wallets = mgr.list_wallets()

    if not wallets:
        _cprint("\n  No wallets found. Create one with: hermes wallet create --chain <chain>\n")
        return

    if _RICH:
        console = Console()
        table = Table(title="Wallets", show_lines=False)
        table.add_column("ID", style="dim")
        table.add_column("Label", style="cyan")
        table.add_column("Chain", style="magenta")
        table.add_column("Type")
        table.add_column("Address", style="green")
        table.add_column("Balance", justify="right")

        for w in wallets:
            try:
                bal = mgr.get_balance(w.wallet_id)
                balance_str = f"{bal.balance:.6f} {bal.symbol}"
            except Exception:
                balance_str = "?"

            type_style = "yellow" if w.wallet_type == "agent" else "blue"
            table.add_row(
                w.wallet_id,
                w.label,
                w.chain,
                f"[{type_style}]{w.wallet_type}[/{type_style}]",
                w.address,
                balance_str,
            )
        console.print()
        console.print(table)
        console.print()
    else:
        for w in wallets:
            _cprint(f"  {w.wallet_id}  {w.label}  ({w.chain}, {w.wallet_type})  {w.address}")
        _cprint("")


def cmd_wallet_balance(args: argparse.Namespace) -> None:
    """Check wallet balance."""
    mgr, _ = _get_wallet_manager()
    try:
        wallet = mgr.resolve_wallet(wallet_id=args.wallet_id, chain=args.chain)
        bal = mgr.get_balance(wallet.wallet_id)
        _cprint(f"\n  {wallet.label} ({wallet.chain})")
        _cprint(f"  Address: {wallet.address}")
        _cprint(f"  Balance: {bal.balance:.9f} {bal.symbol}\n", style="bold green")
    except Exception as e:
        _cprint(f"\n  ✗ {e}\n", style="bold red")


def cmd_wallet_send(args: argparse.Namespace) -> None:
    """Send tokens (with interactive approval)."""
    mgr, policy = _get_wallet_manager()

    to_address = args.to
    try:
        amount = Decimal(args.amount)
    except InvalidOperation:
        _cprint(f"\n  ✗ Invalid amount: {args.amount}\n", style="bold red")
        return

    try:
        wallet = mgr.resolve_wallet(wallet_id=args.wallet_id, chain=args.chain)
    except Exception as e:
        _cprint(f"\n  ✗ {e}\n", style="bold red")
        return

    provider = mgr.get_provider(wallet.chain)
    symbol = provider.config.symbol

    # Show confirmation
    _cprint(f"\n  📤 Send Transaction")
    _cprint(f"     From:   {wallet.label} ({wallet.address})")
    _cprint(f"     To:     {to_address}")
    _cprint(f"     Amount: {amount} {symbol}")
    _cprint(f"     Chain:  {provider.config.display_name}")

    # Estimate fee
    try:
        fee = mgr.estimate_fee(wallet.wallet_id, to_address, amount)
        _cprint(f"     Fee:    ~{fee.estimated_fee:.6f} {fee.symbol}")
    except Exception:
        _cprint(f"     Fee:    (estimate unavailable)")

    _cprint("")

    # Confirm
    try:
        confirm = input("  Confirm? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = "n"

    if confirm not in ("y", "yes"):
        _cprint("\n  ✗ Cancelled\n", style="yellow")
        return

    # Evaluate policy first
    try:
        from wallet.policy import TxRequest, PolicyVerdict
        tx_req = TxRequest(
            wallet_id=wallet.wallet_id,
            wallet_type=wallet.wallet_type,
            chain=wallet.chain,
            to_address=to_address,
            amount=amount,
            symbol=symbol,
        )
        eval_result = policy.evaluate(tx_req)
        if eval_result.verdict == PolicyVerdict.BLOCK:
            _cprint(f"\n  ✗ Blocked by policy: {eval_result.reason}\n", style="bold red")
            return

        # CLI owner explicitly approved by confirming above, so approval-gated
        # txs may proceed here. We still preserve the policy result in history.
        result = mgr.send(
            wallet.wallet_id,
            to_address,
            amount,
            decided_by="owner_cli",
            policy_result=json.dumps({
                "verdict": eval_result.verdict.value,
                "checked": eval_result.checked,
                "failed": eval_result.failed,
                "approved_via": "owner_cli",
            }),
        )
        if result.status == "failed":
            _cprint(f"\n  ✗ Transaction failed: {result.error}\n", style="bold red")
            return

        policy.record_transaction(tx_req)
        _cprint(f"\n  ✓ Transaction submitted!", style="bold green")
        _cprint(f"    TX hash: {result.tx_hash}")
        if result.explorer_url:
            _cprint(f"    Explorer: {result.explorer_url}")
        _cprint("")
    except Exception as e:
        _cprint(f"\n  ✗ Error: {e}\n", style="bold red")


def cmd_wallet_fund(args: argparse.Namespace) -> None:
    """Show deposit address for a wallet."""
    mgr, _ = _get_wallet_manager()
    try:
        wallet = mgr.resolve_wallet(wallet_id=args.wallet_id, chain=args.chain)
        provider = mgr.get_provider(wallet.chain)
        _cprint(f"\n  💰 Fund Wallet: {wallet.label}")
        _cprint(f"     Chain: {provider.config.display_name}")
        _cprint(f"\n     Send {provider.config.symbol} to:")
        _cprint(f"     {wallet.address}", style="bold green")
        if provider.config.is_testnet:
            _cprint(f"\n     ⚠️  This is a testnet wallet — use testnet faucets")
        _cprint("")
    except Exception as e:
        _cprint(f"\n  ✗ {e}\n", style="bold red")


def cmd_wallet_history(args: argparse.Namespace) -> None:
    """Show transaction history."""
    mgr, _ = _get_wallet_manager()
    records = mgr.get_tx_history(wallet_id=args.wallet_id, limit=args.limit)

    if not records:
        _cprint("\n  No transactions yet.\n")
        return

    if _RICH:
        console = Console()
        table = Table(title="Transaction History", show_lines=False)
        table.add_column("Time", style="dim")
        table.add_column("Chain")
        table.add_column("To", style="cyan")
        table.add_column("Amount", justify="right")
        table.add_column("Status")
        table.add_column("TX Hash", style="dim")

        _status_style = {"submitted": "yellow", "confirmed": "green", "failed": "red", "rejected": "red"}
        for r in records:
            ts = r.requested_at[:19].replace("T", " ") if r.requested_at else ""
            status_s = _status_style.get(r.status, "white")
            to_short = r.to_address[:10] + "..." + r.to_address[-6:] if len(r.to_address) > 20 else r.to_address
            hash_short = r.tx_hash[:12] + "..." if r.tx_hash and len(r.tx_hash) > 15 else r.tx_hash or ""
            table.add_row(ts, r.chain, to_short, f"{r.amount} {r.symbol}",
                         f"[{status_s}]{r.status}[/{status_s}]", hash_short)
        console.print()
        console.print(table)
        console.print()
    else:
        for r in records:
            _cprint(f"  {r.requested_at[:19]}  {r.chain}  {r.amount} {r.symbol}  → {r.to_address[:16]}...  {r.status}")
        _cprint("")


def cmd_wallet_freeze(args: argparse.Namespace) -> None:
    """Activate kill switch — block all transactions."""
    _, policy = _get_wallet_manager()
    policy.freeze()
    _cprint("\n  🔒 Wallet FROZEN — all transactions are blocked.", style="bold red")
    _cprint("     Run 'hermes wallet unfreeze' to resume.\n")


def cmd_wallet_unfreeze(args: argparse.Namespace) -> None:
    """Deactivate kill switch."""
    _, policy = _get_wallet_manager()
    policy.unfreeze()
    _cprint("\n  🔓 Wallet unfrozen — transactions are allowed.\n", style="green")


def cmd_wallet_status(args: argparse.Namespace) -> None:
    """Show wallet status overview."""
    mgr, policy = _get_wallet_manager()
    wallets = mgr.list_wallets()

    _cprint(f"\n  💰 Wallet Status")
    _cprint(f"     Wallets:    {len(wallets)}")
    _cprint(f"     Chains:     {', '.join(mgr.supported_chains) or 'none'}")
    _cprint(f"     Frozen:     {'YES ⚠️' if policy.is_frozen else 'No'}")
    _cprint(f"     TX history: {len(mgr.get_tx_history())}")
    _cprint("")


# =========================================================================
# Argparse registration
# =========================================================================

def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``hermes wallet`` subcommand tree."""
    wallet_parser = subparsers.add_parser(
        "wallet",
        help="Manage crypto wallets",
        description="Create, fund, and manage crypto wallets with policy-controlled transactions.",
    )
    wallet_parser.set_defaults(func=cmd_wallet_status)

    w_sub = wallet_parser.add_subparsers(dest="wallet_command")

    # create
    create_p = w_sub.add_parser("create", help="Create a new wallet")
    create_p.add_argument("--chain", "-c", required=True, help="Chain (ethereum, base, solana, etc.)")
    create_p.add_argument("--label", "-l", default="", help="Wallet label")
    create_p.set_defaults(func=cmd_wallet_create)

    # create-agent
    agent_p = w_sub.add_parser("create-agent", help="Create an agent wallet (auto-approve within limits)")
    agent_p.add_argument("--chain", "-c", required=True, help="Chain")
    agent_p.add_argument("--label", "-l", default="", help="Wallet label")
    agent_p.set_defaults(func=cmd_wallet_create_agent)

    # import
    import_p = w_sub.add_parser("import", help="Import wallet from exported private key (migration)")
    import_p.add_argument("--chain", "-c", required=True, help="Chain")
    import_p.add_argument("--label", "-l", default="", help="Wallet label")
    import_p.add_argument("--type", "-t", default="user", choices=["user", "agent"],
                          help="Wallet type (default: user)")
    import_p.set_defaults(func=cmd_wallet_import)

    # export
    export_p = w_sub.add_parser("export", help="Export private key for migration to another machine")
    export_p.add_argument("--wallet-id", "-w", default=None, help="Wallet ID")
    export_p.add_argument("--chain", "-c", default=None, help="Chain")
    export_p.set_defaults(func=cmd_wallet_export)

    # list
    w_sub.add_parser("list", aliases=["ls"], help="List all wallets").set_defaults(func=cmd_wallet_list)

    # balance
    bal_p = w_sub.add_parser("balance", aliases=["bal"], help="Check wallet balance")
    bal_p.add_argument("--wallet-id", "-w", default=None, help="Wallet ID")
    bal_p.add_argument("--chain", "-c", default=None, help="Chain")
    bal_p.set_defaults(func=cmd_wallet_balance)

    # send
    send_p = w_sub.add_parser("send", help="Send tokens")
    send_p.add_argument("to", help="Recipient address")
    send_p.add_argument("amount", help="Amount in native token units")
    send_p.add_argument("--wallet-id", "-w", default=None, help="Wallet ID")
    send_p.add_argument("--chain", "-c", default=None, help="Chain")
    send_p.set_defaults(func=cmd_wallet_send)

    # fund
    fund_p = w_sub.add_parser("fund", help="Show deposit address")
    fund_p.add_argument("--wallet-id", "-w", default=None, help="Wallet ID")
    fund_p.add_argument("--chain", "-c", default=None, help="Chain")
    fund_p.set_defaults(func=cmd_wallet_fund)

    # history
    hist_p = w_sub.add_parser("history", help="Transaction history")
    hist_p.add_argument("--wallet-id", "-w", default=None, help="Wallet ID")
    hist_p.add_argument("--limit", "-n", type=int, default=20, help="Max entries")
    hist_p.set_defaults(func=cmd_wallet_history)

    # freeze / unfreeze
    w_sub.add_parser("freeze", help="Kill switch — block all transactions").set_defaults(func=cmd_wallet_freeze)
    w_sub.add_parser("unfreeze", help="Resume transactions after freeze").set_defaults(func=cmd_wallet_unfreeze)

    # status
    w_sub.add_parser("status", help="Show wallet status").set_defaults(func=cmd_wallet_status)
