"""Configuration for the cross-agent MCP bridge.

Every knob is overridable through an environment variable so that the bridge can be
tuned from the MCP client configuration (Claude Code `.mcp.json`, Codex `config.toml`)
without touching the code.
"""

import os
import urllib.parse
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

# Deliveries, kept so they outlive the server process that carried them: recorded from the
# moment one is queued (under in-flight/ until it ends), finished ones directly here. Records
# are for reporting only. Nothing reads them back to resume or resend a delivery - a restarted
# server re-sending its queue would ask the peer to do the same work twice.
DELIVERY_DIR: str = HOME_DIR + 'deliveries/'

# delivery records older than this are pruned when the directory is read
DELIVERY_TTL_SECONDS: int = get_env_int('CROSS_AGENT_DELIVERY_TTL', 7 * 24 * 3600)

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

# how long a single relayed turn may take before it is aborted - on the CLI path, where the
# turn is our own subprocess. A panel turn cannot be aborted from here at all; see below.
SEND_TIMEOUT_SECONDS: int = get_env_int('CROSS_AGENT_TIMEOUT', 600)

# How long a panel delivery keeps listening for the peer's turn to end. The panel path can only
# watch: the turn belongs to the editor's own session and goes on whether we listen or not, so
# giving up early never saved any work - it only turned a finished answer into a guess read out
# of the transcript. Peer turns of 500..820s were routine on the day this was measured; the
# default leaves room above that. After this the bridge still watches the transcript for a
# while (outbox.RECOVERY_WINDOW_SECONDS) before closing the delivery.
PANEL_PATIENCE_SECONDS: int = get_env_int('CROSS_AGENT_PANEL_PATIENCE', 3600)

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


# ------------------------------------------------ what a spawned agent process inherits

# An MCP server is started by an editor, and an editor is started from a desktop session that
# has collected API keys, tokens and whatever else the user exports from a shell profile.
# Handing all of it to a resumed peer agent hands it to every command that peer then runs, for
# no benefit: the CLIs need an environment to work in, not this one.
#
# So the child gets a named baseline instead. Everything here is something a CLI cannot be
# expected to run without - where its binary is, whose home directory to read config from,
# what the terminal and locale are, how to reach the network and which certificates to trust.
# Adding a name to this list makes it visible to the peer agent, so it wants a reason.
CHILD_ENV_BASELINE: tuple = (
    # finding and running the binary
    'PATH', 'HOME', 'SHELL', 'USER', 'LOGNAME',
    # locale and terminal: without these the CLIs mangle non-ASCII output
    'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'TERM', 'COLORTERM', 'TZ',
    # scratch space and the XDG config/cache/data roots the CLIs and node store state under
    'TMPDIR', 'TEMP', 'TMP',
    'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME',
    # reaching the network through a corporate proxy, and trusting its certificates
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS',
    # macOS: the Security framework reads the login keychain through HOME, and Core Foundation
    # warns on every process start without this one
    '__CF_USER_TEXT_ENCODING',
    # which session store each CLI reads - the bridge resolves sessions in these same two, so
    # a child pointed at a different one would resume a session nobody here can see
    'CLAUDE_CONFIG_DIR', 'CODEX_HOME',
)

# The bridge's own settings, so an agent it spawned runs a bridge configured exactly like
# this one - same state directory, same timeouts, same hop budget. Named one by one rather
# than swept up by prefix: a prefix would also forward anything a user happens to name
# CROSS_AGENT_something, which is no better than forwarding the whole environment. Each of
# these is a path, a number, a mode or a binary name, and none is a credential.
CHILD_ENV_BRIDGE: tuple = (
    'CROSS_AGENT_HOME', 'CROSS_AGENT_DEBUG', 'CROSS_AGENT_DELIVERY_TTL',
    'CROSS_AGENT_CLAUDE_BIN', 'CROSS_AGENT_CODEX_BIN',
    'CROSS_AGENT_REAL_CLAUDE', 'CROSS_AGENT_REAL_CODEX',
    'CROSS_AGENT_ACTIVE_WINDOW_MIN', 'CROSS_AGENT_MAX_HOPS', 'CROSS_AGENT_TIMEOUT',
    'CROSS_AGENT_PANEL_PATIENCE', 'CROSS_AGENT_SCOPE', 'CROSS_AGENT_UI_HOOK',
    'CROSS_AGENT_CODEX_SANDBOX', 'CROSS_AGENT_CODEX_MODEL', 'CROSS_AGENT_CODEX_SCAN_LIMIT',
    'CROSS_AGENT_CLAUDE_PERMISSION_MODE', 'CROSS_AGENT_CLAUDE_MODEL',
    # so the opt-in survives another hop, rather than a grandchild losing it silently
    'CROSS_AGENT_CHILD_ENV',
    # CROSS_AGENT_SELF is deliberately absent: it forces which agent the caller is taken to
    # be, and a child that adopted its parent's answer would misidentify itself.
)

# Escape hatch: a comma-separated list of extra variable names to pass through, for an
# authentication setup that genuinely needs one (a self-hosted gateway's token, a proxy's
# credential helper). Each name listed is visible to the peer agent and to every command it
# runs, so list the one variable, never a prefix of many.
ENV_CHILD_PASSTHROUGH: str = 'CROSS_AGENT_CHILD_ENV'


def child_env_passthrough() -> tuple:
    raw = os.environ.get(ENV_CHILD_PASSTHROUGH) or ''
    return tuple(name.strip() for name in raw.split(',') if name.strip())


# A proxy setting is on the baseline because a CLI behind one cannot reach anything without
# it. But the variable is a URL, and a URL has a place to put a username and password -
# `https://alice:s3cret@proxy.corp:3128` is an ordinary way to configure an authenticating
# proxy, and it is a credential sitting inside an allowlisted variable.
PROXY_ENV_NAMES: frozenset = frozenset({
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy',
})


def has_embedded_credentials(value: str) -> bool:
    """Whether a proxy setting carries userinfo in any of its entries.

    Parsed rather than pattern-matched. Hand-splitting on `://` and `/` got both ends of this
    wrong: it missed the scheme-relative `//alice:secret@proxy:3128`, and it read the `@` in
    `http://proxy:3128?notify=a@b` as a credential. urlsplit knows where the authority ends,
    and its `username`/`password` handle percent-encoded userinfo without any help.

    A scheme is optional - `user:pass@host:8080` is accepted by the CLIs - so an entry without
    one is prefixed with `//`, or `user:` would be read as the scheme. A value too malformed
    to parse is treated as carrying credentials: it is a proxy setting we cannot vouch for,
    and the failure hint says how to pass it deliberately.
    """
    for raw in value.split(','):
        entry = raw.strip()
        if not entry:
            continue
        if '://' not in entry and not entry.startswith('//'):
            entry = '//' + entry
        try:
            parts = urllib.parse.urlsplit(entry)
            if parts.username or parts.password:
                return True
        except ValueError:
            return True
    return False


def withheld_proxy_vars() -> list:
    """Proxy variables this process has that a child will not be given, and why.

    Withheld whole rather than rewritten. Stripping the credential out would hand the child a
    proxy URL that cannot authenticate, so it would fail at the first request with an error
    about the proxy rather than about the bridge - and the user would be debugging a proxy
    that works perfectly well everywhere else.
    """
    opted_in = set(child_env_passthrough())
    return [name for name in sorted(PROXY_ENV_NAMES)
            if name in os.environ and name not in opted_in
            and has_embedded_credentials(os.environ[name])]


def child_env() -> dict:
    """The environment an agent process spawned by this bridge starts with.

    Built up from nothing rather than filtered down from `os.environ`: a deny-list is only as
    good as its author's imagination, and the thing being kept out is whatever secret this
    particular user happens to export.
    """
    names = list(CHILD_ENV_BASELINE) + list(CHILD_ENV_BRIDGE) + list(child_env_passthrough())
    withheld = set(withheld_proxy_vars())
    return {name: os.environ[name] for name in names
            if name in os.environ and name not in withheld}


def ensure_dirs() -> None:
    for path in (HOME_DIR, LOCK_DIR, LOG_DIR, DELIVERY_DIR):
        os.makedirs(path, exist_ok=True)
