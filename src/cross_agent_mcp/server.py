"""MCP server exposing the Claude Code ↔ Codex bridge.

Register the same server on both sides: Claude then reaches the live Codex thread with
`send_to_codex`, and Codex reaches the live Claude session with `send_to_claude`.
"""

import asyncio
import functools
import logging
import logging.handlers
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

from mcp.server import MCPServer

from . import bridge, caller, config, discovery, registry, uihook


logger = logging.getLogger('cross_agent_mcp')

SERVER_INSTRUCTIONS = (
    'Bridge between the two coding agents running in this editor. '
    '`send_to_codex` resumes the live Codex thread; `send_to_claude` resumes the live '
    'Claude Code session. Both keep the peer\'s existing conversation context. '
    'When no active peer session exists, a fresh one is created automatically. '
    'Calls are capped by a hop budget so the two agents cannot ping-pong forever.'
)


def init_logging() -> None:
    """Send logs to a file and to stderr only: stdout carries the MCP protocol."""
    config.ensure_dirs()
    package_logger = logging.getLogger('cross_agent_mcp')
    if package_logger.handlers:
        return

    package_logger.setLevel(logging.DEBUG if os.environ.get('CROSS_AGENT_DEBUG') else logging.INFO)

    # the MCP SDK installs its own root handler; without this every record is emitted twice
    package_logger.propagate = False

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    file_handler.setFormatter(formatter)
    package_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    package_logger.addHandler(stream_handler)


async def _run_blocking(func, *args, **kwargs) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))


async def _send(target_agent: str, **kwargs) -> Dict[str, Any]:
    try:
        return await _run_blocking(bridge.send_message, target_agent, **kwargs)
    except bridge.BridgeError as e:
        logger.error(f'_send [exception]: {target_agent} {e}')
        return {'ok': False, 'target_agent': target_agent, 'error': str(e)}
    except Exception as e:
        logger.error(f'_send [exception]: {target_agent} {e}')
        return {'ok': False, 'target_agent': target_agent, 'error': f'{type(e).__name__}: {e}'}


server: MCPServer = MCPServer(
    name='cross-agent',
    title='Cross-Agent Bridge',
    version='0.1.0',
    instructions=SERVER_INSTRUCTIONS,
)


@server.tool(
    name='send_to_codex',
    title='Send a message to the live Codex thread',
    description=(
        'Relay a message to the Codex session the user is currently working in and return '
        'its reply. The Codex thread keeps its full conversation context. '
        'If no active Codex thread exists for this working directory, a new one is created '
        'and reused for later calls. Use this to ask Codex for a review, a second opinion, '
        'or a verification pass. Blocking: it waits for the Codex turn to finish.'
    ),
)
async def send_to_codex(
    message: str,
    session_id: Optional[str] = None,
    new_session: bool = False,
    scope: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    conversation_id: Optional[str] = None,
    raw: bool = False,
) -> Dict[str, Any]:
    """Args:
    message: What to ask Codex. Be self-contained; Codex cannot see this conversation.
    session_id: Target a specific Codex thread id instead of the auto-detected one.
    new_session: Force a brand new Codex thread even when an active one exists.
    scope: 'cwd' (default) = same directory or below, 'tree' = also parent directories,
        'any' = every recorded thread.
    cwd: Working directory used for discovery and for a newly created thread.
    timeout: Seconds to wait for the Codex turn.
    conversation_id: Continue an existing bridge conversation (shares the hop budget).
    raw: Send the message verbatim, without the bridge envelope.
    """
    return await _send(
        config.AGENT_CODEX, message=message, session_id=session_id,
        is_new_session=new_session, scope=scope, cwd=cwd, timeout=timeout,
        conversation_id=conversation_id, is_raw=raw,
    )


@server.tool(
    name='send_to_claude',
    title='Send a message to the live Claude Code session',
    description=(
        'Relay a message to the Claude Code session the user is currently working in and '
        'return its reply. The Claude session keeps its full conversation context. '
        'If no active Claude session exists for this working directory, a new one is created '
        'and reused for later calls. Use this to ask Claude to implement, refactor or explain '
        'something. Blocking: it waits for the Claude turn to finish.'
    ),
)
async def send_to_claude(
    message: str,
    session_id: Optional[str] = None,
    new_session: bool = False,
    scope: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    conversation_id: Optional[str] = None,
    raw: bool = False,
    allow_same_agent: bool = False,
) -> Dict[str, Any]:
    """Args:
    message: What to ask Claude. Be self-contained; Claude cannot see this conversation.
    session_id: Target a specific Claude session id instead of the auto-detected one.
    new_session: Force a brand new Claude session even when an active one exists.
    scope: 'cwd' (default) = same directory or below, 'tree' = also parent directories,
        'any' = every recorded session.
    cwd: Working directory used for discovery and for a newly created session.
    timeout: Seconds to wait for the Claude turn.
    conversation_id: Continue an existing bridge conversation (shares the hop budget).
    raw: Send the message verbatim, without the bridge envelope.
    allow_same_agent: Allow a Claude session to message another Claude session.
    """
    return await _send(
        config.AGENT_CLAUDE, message=message, session_id=session_id,
        is_new_session=new_session, scope=scope, cwd=cwd, timeout=timeout,
        conversation_id=conversation_id, is_raw=raw, allows_same_agent=allow_same_agent,
    )


@server.tool(
    name='list_agent_sessions',
    title='List discoverable Claude and Codex sessions',
    description=(
        'Show the Claude Code sessions and Codex threads the bridge can reach, newest first, '
        'together with how long ago each was touched and whether it counts as active.'
    ),
)
async def list_agent_sessions(
    agent: str = 'both',
    scope: Optional[str] = None,
    cwd: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Args:
    agent: 'claude', 'codex' or 'both'.
    scope: 'cwd' (default), 'tree' or 'any'.
    cwd: Working directory to scope the lookup to.
    limit: Maximum number of sessions per agent.
    """
    scope = scope or config.DEFAULT_SCOPE
    target_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()
    agents: List[str] = ([config.AGENT_CLAUDE, config.AGENT_CODEX]
                         if agent == 'both' else [agent])

    result: Dict[str, Any] = {
        'ok': True,
        'scope': scope,
        'cwd': target_cwd,
        'active_window_minutes': config.ACTIVE_WINDOW_MINUTES,
    }
    for name in agents:
        try:
            result[name] = await _run_blocking(
                discovery.list_sessions, name, scope, target_cwd, limit)
        except Exception as e:
            result['ok'] = False
            result[name] = {'error': str(e)}
    return result


@server.tool(
    name='bridge_status',
    title='Inspect bridge state',
    description=(
        'Report which agent this MCP server is running under, the resolved peer sessions, '
        'the active hop budget, and any session currently waiting on a bridged reply. '
        'Use it to diagnose why a relay was refused.'
    ),
)
async def bridge_status(cwd: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    """Args:
    cwd: Working directory to resolve sessions against.
    scope: 'cwd' (default), 'tree' or 'any'.
    """
    scope = scope or config.DEFAULT_SCOPE
    target_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()

    identity = await _run_blocking(caller.detect_caller)
    resolved: Dict[str, Any] = {}
    for name in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        try:
            resolved[name] = await _run_blocking(
                discovery.find_active_session, name, scope, target_cwd, None)
        except Exception as e:
            resolved[name] = {'error': str(e)}

    panels: Dict[str, Any] = {}
    for name in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        sessions = (await _run_blocking(uihook.find_live_sessions, name)
                    if uihook.is_enabled() else [])
        panels[name] = {
            'selected_session_id': sessions[0]['session_id'] if sessions else None,
            'open_sessions': [{
                'session_id': s['session_id'],
                'cwd': s.get('cwd'),
                'shim_pid': s.get('shim_pid'),
                'last_user_activity': s.get('last_user_activity'),
                'started_at': s.get('started_at'),
            } for s in sessions],
        }

    return {
        'ok': True,
        'running_under': identity['agent'],
        'process_chain': identity['chain'],
        'cwd': target_cwd,
        'scope': scope,
        'resolved_sessions': resolved,
        'ide_panels': {
            'mode': config.UI_HOOK_MODE,
            'note': ('open_sessions lists every conversation tab of this editor window, ordered '
                     'by when the human last typed into it; selected_session_id is where a relay '
                     'would land. Use pin_agent_session to force a different one. A peer with no '
                     'open_sessions falls back to a headless CLI resume the panel will not show.'),
            **panels,
        },
        'settings': {
            'active_window_minutes': config.ACTIVE_WINDOW_MINUTES,
            'max_hops': config.MAX_HOPS,
            'timeout_seconds': config.SEND_TIMEOUT_SECONDS,
            'codex_sandbox_for_new_sessions': config.CODEX_SANDBOX,
            'claude_permission_mode': config.CLAUDE_PERMISSION_MODE,
            'claude_bin': config.CLAUDE_BIN,
            'codex_bin': config.CODEX_BIN,
            'home_dir': config.HOME_DIR,
        },
        'inherited_chain': {
            'conversation_id': os.environ.get(config.ENV_CONVERSATION_ID),
            'hop': os.environ.get(config.ENV_HOP),
            'sender': os.environ.get(config.ENV_SENDER),
            'busy': os.environ.get(config.ENV_BUSY),
        },
        'busy_locks': await _run_blocking(registry.list_busy_locks),
        'pins': registry.load_registry().get('pins', {}),
    }


@server.tool(
    name='pin_agent_session',
    title='Pin a peer session',
    description=(
        'Force every later relay for this working directory to target one specific session id. '
        'A pin survives inactivity, unlike auto-discovery. Call with session_id empty to clear.'
    ),
)
async def pin_agent_session(
    agent: str,
    session_id: str = '',
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Args:
    agent: 'claude' or 'codex'.
    session_id: Session/thread id to pin, or empty to remove the pin.
    cwd: Working directory the pin applies to.
    """
    if agent not in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        return {'ok': False, 'error': f"agent must be 'claude' or 'codex', got: {agent}"}

    target_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()

    if not session_id:
        removed = await _run_blocking(registry.clear_pin, agent, target_cwd)
        return {'ok': True, 'agent': agent, 'cwd': target_cwd, 'was_pin_removed': removed}

    found = await _run_blocking(discovery.find_session, agent, session_id)
    if not found:
        return {'ok': False, 'error': f'{agent} session not found: {session_id}'}

    await _run_blocking(registry.set_pin, agent, target_cwd, session_id,
                        found.get('cwd') or target_cwd, True, False)
    return {'ok': True, 'agent': agent, 'cwd': target_cwd, 'pinned': found}


def run_check() -> int:
    """Print what the bridge can currently see, then exit. Not part of the MCP protocol."""
    identity = caller.detect_caller()
    scope = config.DEFAULT_SCOPE
    cwd = os.getcwd()

    print(f'cross-agent MCP {__import__("cross_agent_mcp").__version__}')
    print(f'  cwd            : {cwd}')
    print(f'  running under  : {identity["agent"]}')
    print(f'  claude bin     : {shutil.which(config.CLAUDE_BIN) or "NOT FOUND: " + config.CLAUDE_BIN}')
    print(f'  codex bin      : {shutil.which(config.CODEX_BIN) or "NOT FOUND: " + config.CODEX_BIN}')
    print(f'  scope          : {scope} (active window {config.ACTIVE_WINDOW_MINUTES} min)')
    print(f'  max hops       : {config.MAX_HOPS}, timeout {config.SEND_TIMEOUT_SECONDS}s')
    print(f'  new codex sandbox      : {config.CODEX_SANDBOX}')
    print(f'  claude permission mode : {config.CLAUDE_PERMISSION_MODE or "(agent default)"}')
    print(f'  state dir      : {config.HOME_DIR}')

    for agent in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        resolved = discovery.find_active_session(agent, scope, cwd)
        if resolved:
            print(f'\n  active {agent}: {resolved["session_id"]}')
            print(f'      via {resolved.get("source")}, {resolved["age_minutes"]} min ago, '
                  f'cwd={resolved.get("cwd")}')
            if resolved.get('title'):
                print(f'      title: {resolved["title"]}')
        else:
            print(f'\n  active {agent}: none in scope -> a new session would be created')

    locks = registry.list_busy_locks()
    print(f'\n  busy locks     : {len(locks)}')
    return 0


def main() -> None:
    init_logging()

    if '--check' in sys.argv[1:]:
        sys.exit(run_check())

    if sys.stdin.isatty():
        print('cross-agent MCP is a stdio server: it expects MCP JSON-RPC on stdin and is meant '
              'to be launched by Claude Code or Codex, not run by hand.\n'
              'Run with --check to inspect the bridge state instead. Waiting on stdin, Ctrl-C to quit.',
              file=sys.stderr)

    logger.info(f'main [BEGIN]: cross-agent MCP server, cwd={os.getcwd()}')
    try:
        server.run('stdio')
    except KeyboardInterrupt:
        logger.info('main [END]: interrupted')
    finally:
        logging.shutdown()


if __name__ == '__main__':
    main()
