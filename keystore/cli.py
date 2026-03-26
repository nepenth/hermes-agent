"""CLI subcommands for ``hermes keystore``.

Provides:
    hermes keystore init              — Create a new keystore
    hermes keystore list              — List stored secrets (no values)
    hermes keystore set <name>        — Add or update a secret
    hermes keystore show <name>       — Decrypt and display a secret
    hermes keystore delete <name>     — Remove a secret
    hermes keystore set-category      — Change a secret's access category
    hermes keystore migrate           — Import from .env
    hermes keystore remember          — Cache passphrase in OS credential store
    hermes keystore forget            — Remove cached passphrase
    hermes keystore change-passphrase — Re-encrypt with a new passphrase
    hermes keystore audit             — Show access log
    hermes keystore status            — Show keystore status
"""

import argparse
import getpass
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.table import Table
    _RICH = True
except ImportError:
    _RICH = False


def _cprint(msg: str, style: str = "") -> None:
    """Print with optional Rich styling, falling back to plain."""
    if _RICH:
        Console().print(msg, style=style)
    else:
        print(msg)


def _get_client():
    """Import and return the keystore client (lazy to avoid import errors
    when keystore deps aren't installed)."""
    try:
        from keystore.client import get_keystore
        return get_keystore()
    except ImportError as e:
        _cprint(
            f"\n  ✗ Keystore dependencies not installed: {e}\n"
            f"    Install with: pip install 'hermes-agent[keystore]'\n",
            style="bold red",
        )
        sys.exit(1)


def _require_unlocked(ks, interactive: bool = True) -> None:
    """Ensure the keystore is unlocked or exit."""
    from keystore.store import PassphraseMismatch, KeystoreLocked
    try:
        if not ks.ensure_unlocked(interactive=interactive):
            _cprint("\n  Keystore not initialized. Run: hermes keystore init\n", style="yellow")
            sys.exit(1)
    except PassphraseMismatch:
        _cprint("\n  ✗ Incorrect passphrase\n", style="bold red")
        sys.exit(1)
    except KeystoreLocked as e:
        _cprint(f"\n  ✗ {e}\n", style="bold red")
        sys.exit(1)


# =========================================================================
# Subcommand handlers
# =========================================================================

def cmd_keystore_init(args: argparse.Namespace) -> None:
    """Create a new encrypted keystore."""
    from keystore.store import KeystoreError
    ks = _get_client()

    if ks.is_initialized:
        _cprint("\n  Keystore already initialized.", style="yellow")
        count = ks.secret_count()
        _cprint(f"  {count} secrets stored.\n")
        return

    _cprint("\n  🔐 Secure Keystore Setup\n")
    _cprint("  Your API keys and secrets will be encrypted with a master passphrase.")
    _cprint("  Choose something memorable — you'll need it each time you start Hermes.\n")

    passphrase = getpass.getpass("  Passphrase: ")
    if not passphrase:
        _cprint("\n  ✗ Passphrase cannot be empty\n", style="bold red")
        sys.exit(1)
    confirm = getpass.getpass("  Confirm:    ")
    if passphrase != confirm:
        _cprint("\n  ✗ Passphrases don't match\n", style="bold red")
        sys.exit(1)

    try:
        ks.initialize(passphrase)
    except KeystoreError as e:
        _cprint(f"\n  ✗ {e}\n", style="bold red")
        sys.exit(1)

    from keystore.client import _default_db_path
    _cprint(f"\n  ✓ Keystore created at {_default_db_path()}", style="green")
    _cprint("")
    _cprint("  💡 Tip: Run 'hermes keystore remember' to cache your passphrase")
    _cprint("     so you don't have to type it every time.\n")


def cmd_keystore_list(args: argparse.Namespace) -> None:
    """List all stored secrets (names and categories, no values)."""
    ks = _get_client()
    _require_unlocked(ks)

    secrets = ks.list_secrets()
    if not secrets:
        _cprint("\n  No secrets stored. Use 'hermes keystore set <name>' to add one.\n")
        return

    if _RICH:
        console = Console()
        table = Table(title="Keystore Secrets", show_lines=False)
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Category", style="magenta")
        table.add_column("Description")
        table.add_column("Last Accessed", style="dim")
        table.add_column("Accesses", justify="right", style="dim")

        _cat_style = {
            "injectable": "green",
            "gated": "yellow",
            "sealed": "red",
            "user_only": "blue",
        }
        for s in secrets:
            cat_style = _cat_style.get(s.category, "white")
            last = s.last_accessed_at[:10] if s.last_accessed_at else "never"
            table.add_row(
                s.name,
                f"[{cat_style}]{s.category}[/{cat_style}]",
                s.description or "",
                last,
                str(s.access_count),
            )
        console.print()
        console.print(table)
        console.print()
    else:
        print(f"\n  {'Name':<35} {'Category':<12} {'Description'}")
        print(f"  {'─'*35} {'─'*12} {'─'*30}")
        for s in secrets:
            print(f"  {s.name:<35} {s.category:<12} {s.description or ''}")
        print()


def cmd_keystore_set(args: argparse.Namespace) -> None:
    """Add or update a secret."""
    ks = _get_client()
    _require_unlocked(ks)

    name = args.name.upper()
    value = getpass.getpass(f"  Value for {name} (hidden): ")
    if not value:
        _cprint("\n  ✗ Value cannot be empty\n", style="bold red")
        sys.exit(1)

    category = args.category
    description = args.description or ""

    ks.set_secret(name, value, category=category, description=description)
    _cprint(f"\n  ✓ Secret '{name}' stored (category: {category or 'auto'})\n", style="green")


def cmd_keystore_show(args: argparse.Namespace) -> None:
    """Decrypt and display a secret (requires passphrase re-entry)."""
    ks = _get_client()
    _require_unlocked(ks)

    name = args.name.upper()

    # Re-verify identity for sealed/user_only secrets
    value = ks.get_secret(name, requester="cli")
    if value is None:
        _cprint(f"\n  ✗ Secret '{name}' not found or access denied\n", style="bold red")
        sys.exit(1)

    _cprint(f"\n  {name} = {value}\n")


def cmd_keystore_delete(args: argparse.Namespace) -> None:
    """Remove a secret."""
    ks = _get_client()
    _require_unlocked(ks)

    name = args.name.upper()
    if ks.delete_secret(name):
        _cprint(f"\n  ✓ Secret '{name}' deleted\n", style="green")
    else:
        _cprint(f"\n  ✗ Secret '{name}' not found\n", style="bold red")


def cmd_keystore_set_category(args: argparse.Namespace) -> None:
    """Change a secret's access category."""
    from keystore.store import KeystoreError
    ks = _get_client()
    _require_unlocked(ks)

    name = args.name.upper()
    category = args.category
    try:
        if ks.set_category(name, category):
            _cprint(f"\n  ✓ {name} → {category}\n", style="green")
        else:
            _cprint(f"\n  ✗ Secret '{name}' not found\n", style="bold red")
    except KeystoreError as e:
        _cprint(f"\n  ✗ {e}\n", style="bold red")


def cmd_keystore_migrate(args: argparse.Namespace) -> None:
    """Migrate secrets from .env to the keystore."""
    ks = _get_client()

    # Initialize if needed
    if not ks.is_initialized:
        _cprint("\n  🔐 Keystore not initialized — setting up now.\n")
        passphrase = getpass.getpass("  Choose a passphrase: ")
        if not passphrase:
            _cprint("\n  ✗ Passphrase cannot be empty\n", style="bold red")
            sys.exit(1)
        confirm = getpass.getpass("  Confirm:              ")
        if passphrase != confirm:
            _cprint("\n  ✗ Passphrases don't match\n", style="bold red")
            sys.exit(1)
        ks.initialize(passphrase)
        _cprint("  ✓ Keystore created\n", style="green")
    else:
        _require_unlocked(ks)

    from keystore.client import _env_file_path
    env_path = _env_file_path()
    if not env_path.exists():
        _cprint(f"\n  No .env file found at {env_path}\n", style="yellow")
        return

    migrated = ks.migrate_from_env(env_path)
    if not migrated:
        _cprint("\n  No secrets found in .env to migrate.\n", style="yellow")
        return

    _cprint(f"\n  📦 Migrated {len(migrated)} secrets:\n")
    for name, category in sorted(migrated.items()):
        _cprint(f"    {name:<35} → {category}")

    # Backup and replace .env
    if not args.keep_env:
        backup_path = env_path.with_suffix(
            f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        shutil.copy2(env_path, backup_path)
        _cprint(f"\n  ✓ Original .env backed up to {backup_path.name}", style="green")

        # Write stub
        with open(env_path, "w") as f:
            f.write(
                "# Secrets are now managed by the Hermes encrypted keystore.\n"
                "# Run 'hermes keystore list' to see stored secrets.\n"
                "# Run 'hermes keystore set <NAME>' to add/update a secret.\n"
                "#\n"
                "# You can still set env vars here for non-secret config,\n"
                "# or export secrets in your shell for CI/Docker environments.\n"
                "# Shell exports always take priority over the keystore.\n"
            )
        _cprint("  ✓ .env replaced with stub (keystore handles secrets now)", style="green")

    _cprint(f"\n  ✓ Migration complete\n", style="bold green")
    _cprint("  Review categories with: hermes keystore list")
    _cprint("  Change a category:      hermes keystore set-category <NAME> <CATEGORY>\n")


def cmd_keystore_remember(args: argparse.Namespace) -> None:
    """Cache the passphrase in the OS credential store."""
    from keystore import credential_store

    ks = _get_client()

    backend = credential_store.backend_name()
    if backend:
        _cprint(f"\n  Detected: {backend}\n")
    else:
        _cprint("\n  ⚠️  No credential store backend available.\n", style="yellow")
        _cprint("  Options:")
        _cprint("    • Set HERMES_KEYSTORE_PASSPHRASE env var for headless/Docker")
        _cprint("    • Install keyring: pip install keyring")
        if sys.platform == "linux":
            _cprint("    • Install keyctl: apt install keyutils")
        _cprint("    • Type your passphrase each time (most secure)\n")
        return

    passphrase = getpass.getpass("  Keystore passphrase: ")
    if not passphrase:
        _cprint("\n  ✗ Cancelled\n", style="yellow")
        return

    success, msg = ks.remember_passphrase(passphrase)
    if success:
        _cprint(f"\n  ✓ Passphrase saved to {msg}", style="green")
        _cprint("  To remove: hermes keystore forget\n")

        # Backend-specific notes
        if "Kernel Keyring" in msg:
            _cprint(
                "  ⚠️  Note: kernel keyring may expire after inactivity.\n"
                "     For always-on gateway deployments, consider\n"
                "     HERMES_KEYSTORE_PASSPHRASE env var instead.\n",
                style="dim",
            )
        elif "Encrypted File" in msg:
            _cprint(
                "  ⚠️  This uses machine-derived encryption.\n"
                "     Less secure than a system keychain, but works everywhere.\n",
                style="dim",
            )
    else:
        _cprint(f"\n  ✗ {msg}\n", style="bold red")


def cmd_keystore_forget(args: argparse.Namespace) -> None:
    """Remove the cached passphrase."""
    ks = _get_client()
    success, msg = ks.forget_passphrase()
    if success:
        _cprint(f"\n  ✓ Passphrase removed from {msg}\n", style="green")
    else:
        _cprint(f"\n  ✗ {msg}\n", style="yellow")


def cmd_keystore_change_passphrase(args: argparse.Namespace) -> None:
    """Change the master passphrase."""
    from keystore.store import PassphraseMismatch
    ks = _get_client()

    if not ks.is_initialized:
        _cprint("\n  Keystore not initialized. Run: hermes keystore init\n", style="yellow")
        return

    old = getpass.getpass("  Current passphrase: ")
    new = getpass.getpass("  New passphrase:     ")
    if not new:
        _cprint("\n  ✗ Passphrase cannot be empty\n", style="bold red")
        return
    confirm = getpass.getpass("  Confirm new:        ")
    if new != confirm:
        _cprint("\n  ✗ Passphrases don't match\n", style="bold red")
        return

    try:
        ks.change_passphrase(old, new)
        _cprint("\n  ✓ Passphrase changed successfully\n", style="green")
        _cprint("  💡 If you used 'hermes keystore remember', run it again to update.\n")
    except PassphraseMismatch:
        _cprint("\n  ✗ Current passphrase is incorrect\n", style="bold red")


def cmd_keystore_audit(args: argparse.Namespace) -> None:
    """Show the access log."""
    ks = _get_client()
    _require_unlocked(ks)

    entries = ks.get_access_log(limit=args.limit)
    if not entries:
        _cprint("\n  No access log entries.\n")
        return

    if _RICH:
        console = Console()
        table = Table(title="Keystore Access Log", show_lines=False)
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("Secret", style="cyan")
        table.add_column("Action")
        table.add_column("Requester", style="magenta")

        _action_style = {
            "read": "green",
            "write": "blue",
            "inject": "green",
            "denied": "bold red",
            "delete": "yellow",
        }
        for e in entries:
            ts = e["timestamp"][:19].replace("T", " ")
            action = e["action"]
            style = _action_style.get(action, "white")
            table.add_row(ts, e["secret_name"], f"[{style}]{action}[/{style}]", e["requester"] or "")
        console.print()
        console.print(table)
        console.print()
    else:
        print(f"\n  {'Time':<20} {'Secret':<35} {'Action':<8} {'Requester'}")
        print(f"  {'─'*20} {'─'*35} {'─'*8} {'─'*12}")
        for e in entries:
            ts = e["timestamp"][:19].replace("T", " ")
            print(f"  {ts:<20} {e['secret_name']:<35} {e['action']:<8} {e['requester'] or ''}")
        print()


def cmd_keystore_status(args: argparse.Namespace) -> None:
    """Show keystore status."""
    from keystore import credential_store
    ks = _get_client()

    _cprint("\n  🔐 Keystore Status\n")

    if not ks.is_initialized:
        _cprint("  Status:      Not initialized", style="yellow")
        _cprint("  Run:         hermes keystore init\n")
        return

    count = ks.secret_count()
    _cprint(f"  Status:      {'Unlocked' if ks.is_unlocked else 'Locked'}")
    _cprint(f"  Secrets:     {count}")

    from keystore.client import _default_db_path
    db_path = _default_db_path()
    if db_path.exists():
        size_kb = db_path.stat().st_size / 1024
        _cprint(f"  DB path:     {db_path}")
        _cprint(f"  DB size:     {size_kb:.1f} KB")

    backend = credential_store.backend_name()
    cached = credential_store.retrieve_passphrase() is not None if backend else False
    _cprint(f"  Cred store:  {backend or 'None available'}")
    if backend:
        _cprint(f"  Passphrase:  {'Cached' if cached else 'Not cached'}")

    _cprint("")


# =========================================================================
# Argparse registration (called from hermes_cli/main.py)
# =========================================================================

def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``hermes keystore`` subcommand tree."""
    keystore_parser = subparsers.add_parser(
        "keystore",
        help="Manage the encrypted secret store",
        description="Encrypted keystore for API keys, tokens, and wallet secrets.",
    )
    keystore_parser.set_defaults(func=cmd_keystore_status)

    ks_sub = keystore_parser.add_subparsers(dest="keystore_command")

    # init
    ks_sub.add_parser("init", help="Create a new keystore").set_defaults(func=cmd_keystore_init)

    # list
    ks_sub.add_parser("list", aliases=["ls"], help="List stored secrets").set_defaults(func=cmd_keystore_list)

    # set
    set_p = ks_sub.add_parser("set", aliases=["add"], help="Add or update a secret")
    set_p.add_argument("name", help="Secret name (e.g. OPENROUTER_API_KEY)")
    set_p.add_argument("--category", "-c", default=None,
                       choices=["injectable", "gated", "sealed", "user_only"],
                       help="Access category (default: auto-detected)")
    set_p.add_argument("--description", "-d", default="", help="Human-readable description")
    set_p.set_defaults(func=cmd_keystore_set)

    # show
    show_p = ks_sub.add_parser("show", aliases=["get"], help="Decrypt and display a secret")
    show_p.add_argument("name", help="Secret name")
    show_p.set_defaults(func=cmd_keystore_show)

    # delete
    del_p = ks_sub.add_parser("delete", aliases=["rm", "remove"], help="Remove a secret")
    del_p.add_argument("name", help="Secret name")
    del_p.set_defaults(func=cmd_keystore_delete)

    # set-category
    cat_p = ks_sub.add_parser("set-category", help="Change a secret's access category")
    cat_p.add_argument("name", help="Secret name")
    cat_p.add_argument("category", choices=["injectable", "gated", "sealed", "user_only"])
    cat_p.set_defaults(func=cmd_keystore_set_category)

    # migrate
    mig_p = ks_sub.add_parser("migrate", help="Import secrets from .env")
    mig_p.add_argument("--keep-env", action="store_true",
                       help="Don't replace .env with a stub after migration")
    mig_p.set_defaults(func=cmd_keystore_migrate)

    # remember / forget
    ks_sub.add_parser("remember", help="Cache passphrase in OS credential store").set_defaults(func=cmd_keystore_remember)
    ks_sub.add_parser("forget", help="Remove cached passphrase").set_defaults(func=cmd_keystore_forget)

    # change-passphrase
    ks_sub.add_parser("change-passphrase", help="Change master passphrase").set_defaults(func=cmd_keystore_change_passphrase)

    # audit
    audit_p = ks_sub.add_parser("audit", aliases=["log"], help="Show access log")
    audit_p.add_argument("--limit", "-n", type=int, default=50, help="Number of entries (default: 50)")
    audit_p.set_defaults(func=cmd_keystore_audit)

    # status
    ks_sub.add_parser("status", help="Show keystore status").set_defaults(func=cmd_keystore_status)
