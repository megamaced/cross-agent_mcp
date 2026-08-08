"""Configuration for the cross-agent MCP bridge.

Every knob is overridable through an environment variable so that the bridge can be
tuned from the MCP client configuration (Claude Code `.mcp.json`, Codex `config.toml`)
without touching the code.
"""

import os
from typing import Optional


def get_env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def get_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_env_optional(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value else None


# bridge state directories
HOME_DIR: str = os.path.expanduser(get_env_str('CROSS_AGENT_HOME', '~/.cross-agent')) + '/'
REGISTRY_PATH: str = HOME_DIR + 'registry.json'
LOCK_DIR: str = HOME_DIR + 'locks/'
LOG_DIR: str = HOME_DIR + 'logs/'
LOG_PATH: str = LOG_DIR + 'bridge.log'

# agent session stores
CLAUDE_HOME_DIR: str = os.path.expanduser(get_env_str('CLAUDE_CONFIG_DIR', '~/.claude')) + '/'
CLAUDE_PROJECTS_DIR: str = CLAUDE_HOME_DIR + 'projects/'
CODEX_HOME_DIR: str = os.path.expanduser(get_env_str('CODEX_HOME', '~/.codex')) + '/'
CODEX_SESSIONS_DIR: str = CODEX_HOME_DIR + 'sessions/'
CODEX_SESSION_INDEX_PATH: str = CODEX_HOME_DIR + 'session_index.jsonl'

# CLI entry points
CLAUDE_BIN: str = get_env_str('CROSS_AGENT_CLAUDE_BIN', 'claude')
CODEX_BIN: str = get_env_str('CROSS_AGENT_CODEX_BIN', 'codex')

# a session whose transcript has not been touched for longer than this is not "active"
ACTIVE_WINDOW_MINUTES: int = get_env_int('CROSS_AGENT_ACTIVE_WINDOW_MIN', 240)

# ping-pong guard: how many bridge hops a single conversation may take
MAX_HOPS: int = get_env_int('CROSS_AGENT_MAX_HOPS', 4)

# how long a single relayed turn may take before it is aborted
SEND_TIMEOUT_SECONDS: int = get_env_int('CROSS_AGENT_TIMEOUT', 600)

# 'cwd' = only sessions rooted at the same directory, 'any' = every recorded session
DEFAULT_SCOPE: str = get_env_str('CROSS_AGENT_SCOPE', 'cwd')

# Deliver into the Codex thread open in this editor window through the app-server shim.
# 'auto' uses it when a shim is running, 'off' always relays over the CLI, 'require' fails
# rather than silently falling back to a headless resume the panel will not show.
UI_HOOK_MODE: str = get_env_str('CROSS_AGENT_UI_HOOK', 'auto')

# applied only when the bridge has to spawn a brand new session
CODEX_SANDBOX: str = get_env_str('CROSS_AGENT_CODEX_SANDBOX', 'read-only')
CODEX_MODEL: Optional[str] = get_env_optional('CROSS_AGENT_CODEX_MODEL')
CLAUDE_PERMISSION_MODE: Optional[str] = get_env_optional('CROSS_AGENT_CLAUDE_PERMISSION_MODE')
CLAUDE_MODEL: Optional[str] = get_env_optional('CROSS_AGENT_CLAUDE_MODEL')

# safety valve on how many rollout files a Codex scan opens; only the first line of each is
# read, and the scan stops early once enough matching sessions are found
CODEX_SCAN_LIMIT: int = get_env_int('CROSS_AGENT_CODEX_SCAN_LIMIT', 2000)

# chain state handed down to the agent process spawned by this bridge
ENV_CONVERSATION_ID: str = 'CROSS_AGENT_CONVERSATION_ID'
ENV_HOP: str = 'CROSS_AGENT_HOP'
ENV_BUSY: str = 'CROSS_AGENT_BUSY'
ENV_SENDER: str = 'CROSS_AGENT_SENDER'

AGENT_CLAUDE: str = 'claude'
AGENT_CODEX: str = 'codex'


def ensure_dirs() -> None:
    for path in (HOME_DIR, LOCK_DIR, LOG_DIR):
        os.makedirs(path, exist_ok=True)
