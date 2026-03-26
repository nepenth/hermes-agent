"""Secret categories — access control classifications.

Every secret in the keystore belongs to one of four categories that
determine how and whether the agent process can access it.
"""

from enum import Enum
from typing import Dict


class SecretCategory(str, Enum):
    """Access control classification for keystore secrets."""

    INJECTABLE = "injectable"
    """Auto-injected into os.environ at agent startup.

    The agent code reads these via os.getenv() as before.
    No plaintext file on disk — the daemon populates env vars
    in the child process.

    Examples: OPENROUTER_API_KEY, FAL_KEY, PARALLEL_API_KEY
    """

    GATED = "gated"
    """Available on request through the daemon, with logging.

    The agent can ask for these via the keystore client, but every
    access is logged.  Optionally requires user approval per-access.

    Examples: GITHUB_TOKEN, SSH private keys
    """

    SEALED = "sealed"
    """Never exposed to the agent process in any form.

    The daemon uses these internally (e.g., wallet private keys)
    and the agent interacts through session tokens or tool results.

    Examples: wallet private keys, master passwords
    """

    USER_ONLY = "user_only"
    """Accessible only via the CLI, never by the agent or gateway.

    These are secrets the user manages directly and the agent
    should never see, even through gated access.

    Examples: SUDO_PASSWORD, backup encryption keys
    """


# Default category assignments for known env var names.
# Anything not listed defaults to INJECTABLE for backward compatibility.
DEFAULT_CATEGORIES: Dict[str, SecretCategory] = {
    # Provider API keys — injectable (agent needs them for LLM calls)
    "OPENROUTER_API_KEY": SecretCategory.INJECTABLE,
    "ANTHROPIC_API_KEY": SecretCategory.INJECTABLE,
    "OPENAI_API_KEY": SecretCategory.INJECTABLE,
    "GLM_API_KEY": SecretCategory.INJECTABLE,
    "ZAI_API_KEY": SecretCategory.INJECTABLE,
    "Z_AI_API_KEY": SecretCategory.INJECTABLE,
    "KIMI_API_KEY": SecretCategory.INJECTABLE,
    "MINIMAX_API_KEY": SecretCategory.INJECTABLE,
    "MINIMAX_CN_API_KEY": SecretCategory.INJECTABLE,
    "OPENCODE_ZEN_API_KEY": SecretCategory.INJECTABLE,
    "OPENCODE_GO_API_KEY": SecretCategory.INJECTABLE,
    "DASHSCOPE_API_KEY": SecretCategory.INJECTABLE,
    "COPILOT_API_KEY": SecretCategory.INJECTABLE,

    # Tool API keys — injectable
    "PARALLEL_API_KEY": SecretCategory.INJECTABLE,
    "FIRECRAWL_API_KEY": SecretCategory.INJECTABLE,
    "FAL_KEY": SecretCategory.INJECTABLE,
    "BROWSERBASE_API_KEY": SecretCategory.INJECTABLE,
    "HONCHO_API_KEY": SecretCategory.INJECTABLE,

    # Messaging platform tokens — injectable (gateway needs them)
    "TELEGRAM_BOT_TOKEN": SecretCategory.INJECTABLE,
    "DISCORD_BOT_TOKEN": SecretCategory.INJECTABLE,
    "SLACK_BOT_TOKEN": SecretCategory.INJECTABLE,
    "SLACK_APP_TOKEN": SecretCategory.INJECTABLE,
    "WHATSAPP_API_TOKEN": SecretCategory.INJECTABLE,
    "SIGNAL_HTTP_URL": SecretCategory.INJECTABLE,
    "MATTERMOST_TOKEN": SecretCategory.INJECTABLE,
    "MATRIX_PASSWORD": SecretCategory.INJECTABLE,
    "DINGTALK_CLIENT_ID": SecretCategory.INJECTABLE,
    "DINGTALK_CLIENT_SECRET": SecretCategory.INJECTABLE,
    "TWILIO_ACCOUNT_SID": SecretCategory.INJECTABLE,
    "TWILIO_AUTH_TOKEN": SecretCategory.INJECTABLE,

    # Gated — logged access, optional approval
    "GITHUB_TOKEN": SecretCategory.GATED,

    # User-only — never exposed to agent
    "SUDO_PASSWORD": SecretCategory.USER_ONLY,

    # Sealed — wallet keys use a different naming convention
    # (wallet:chain:address) and are always sealed.
}


def default_category(secret_name: str) -> SecretCategory:
    """Return the default category for a secret name.

    Wallet keys (prefixed with ``wallet:``) are always SEALED.
    Known env vars use the mapping above.
    Everything else defaults to INJECTABLE for backward compatibility.
    """
    if secret_name.startswith("wallet:"):
        return SecretCategory.SEALED
    return DEFAULT_CATEGORIES.get(secret_name, SecretCategory.INJECTABLE)
