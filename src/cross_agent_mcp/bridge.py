"""Relay a message into the peer agent's live session and bring the reply back.

  Claude Code : claude -p --resume <session-id> --output-format json "<message>"
  Codex       : codex exec resume <thread-id> --json "<message>"

Both commands re-enter an existing transcript, so the peer answers with its whole
conversation context intact instead of starting from a blank agent.
"""

import contextlib
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import caller, config, discovery, registry, uihook


logger = logging.getLogger('cross_agent_mcp.bridge')

# how long a killed process group gets to die before it is SIGKILLed
KILL_GRACE_SECONDS = 5

AGENT_LABEL: Dict[str, str] = {
    config.AGENT_CLAUDE: 'Claude Code',
    config.AGENT_CODEX: 'Codex',
}
PEER_TOOL: Dict[str, str] = {
    config.AGENT_CLAUDE: 'send_to_codex',
    config.AGENT_CODEX: 'send_to_claude',
}


class BridgeError(Exception):
    """Raised for every condition the calling agent should see as a tool failure."""


# ------------------------------------------------------------- chain context

def _busy_from_env() -> List[str]:
    raw = os.environ.get(config.ENV_BUSY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return [str(v) for v in value] if isinstance(value, list) else []
    except Exception:
        return []


def _resolve_conversation_id(conversation_id: Optional[str]) -> Tuple[str, bool]:
    if conversation_id:
        return conversation_id, False
    inherited = os.environ.get(config.ENV_CONVERSATION_ID)
    if inherited:
        return inherited, False
    return 'conv_' + uuid.uuid4().hex[:12], True


def _build_envelope(sender: str, target: str, conversation_id: str,
                    hop: int, remaining: int, message: str) -> str:
    sender_label = AGENT_LABEL.get(sender, sender)
    reply_tool = PEER_TOOL.get(target, 'the cross-agent tool')

    if remaining > 0:
        follow_up = (f'- If you need to send a NEW request back to {sender_label}, call the '
                     f'`{reply_tool}` tool ({remaining} bridge hop(s) left). Otherwise just answer.')
    else:
        follow_up = ('- The hop budget for this conversation is exhausted. Do NOT call any '
                     'cross-agent tool; answer directly.')

    return (
        '=== CROSS-AGENT BRIDGE MESSAGE ===\n'
        f'from: {sender_label} (peer AI agent, not the human user)\n'
        f'conversation: {conversation_id} | hop {hop}/{config.MAX_HOPS}\n'
        '\n'
        f'{message}\n'
        '\n'
        '=== HOW TO REPLY ===\n'
        f'- Your final assistant message is relayed verbatim back to {sender_label}.\n'
        '- Answer the peer directly; do not wait for the human and do not ask for confirmation.\n'
        '- Keep the answer self-contained: the peer sees only your final message.\n'
        f'{follow_up}\n'
    )


def _child_env(conversation_id: str, hop: int, sender: str, busy: List[str]) -> Dict[str, str]:
    env = dict(os.environ)
    env[config.ENV_CONVERSATION_ID] = conversation_id
    env[config.ENV_HOP] = str(hop)
    env[config.ENV_SENDER] = sender
    env[config.ENV_BUSY] = json.dumps(busy)
    return env


# ----------------------------------------------------------------- CLI calls

def _terminate_group(process: subprocess.Popen) -> None:
    """Kill the CLI *and* the tool-call subprocesses it spawned.

    The agent CLIs run builds, test suites and shell commands as their own children. Killing
    only the direct child would leave those running unsupervised after a timeout.
    """
    try:
        group_id = os.getpgid(process.pid)
    except OSError:
        process.kill()
        return

    with contextlib.suppress(OSError):
        os.killpg(group_id, signal.SIGTERM)
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(group_id, signal.SIGKILL)


def _run_cli(command: List[str], cwd: str, env: Dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    logger.debug(f'_run_cli [BEGIN]: cwd={cwd} cmd={command[:4]}')
    try:
        # start_new_session puts the CLI in its own process group so the whole tree is killable
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise BridgeError(f'CLI not found: {command[0]}')

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(f'_run_cli [exception]: timeout after {timeout}s, killing process group')
        _terminate_group(process)
        with contextlib.suppress(Exception):
            process.communicate(timeout=KILL_GRACE_SECONDS)
        raise BridgeError(f'peer agent did not answer within {timeout}s')
    finally:
        logger.debug('_run_cli [END]')

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _call_via_panel(message: str, session_id: Optional[str], ui_shim: Dict[str, Any],
                    timeout: int) -> Dict[str, Any]:
    """Deliver through the editor panel shim, so the exchange shows up in the panel."""
    response = uihook.send(message, ui_shim, session_id, timeout)
    if not response.get('ok'):
        raise BridgeError(f'IDE panel relay failed: {response.get("error")}')

    return {
        'session_id': response.get('sessionId') or session_id or '',
        'reply': str(response.get('reply') or '').strip(),
        'is_new_session': False,
        'usage': None,
        'cost_usd': None,
    }


def _call_claude(message: str, session_id: Optional[str], cwd: str, env: Dict[str, str],
                 timeout: int, ui_shim: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resume (or create) a Claude Code session and return its final message."""
    if ui_shim:
        return _call_via_panel(message, session_id, ui_shim, timeout)

    is_new = session_id is None
    target_id = session_id or str(uuid.uuid4())

    command = [config.CLAUDE_BIN, '-p', '--output-format', 'json']
    command += ['--session-id', target_id] if is_new else ['--resume', target_id]
    if config.CLAUDE_PERMISSION_MODE:
        command += ['--permission-mode', config.CLAUDE_PERMISSION_MODE]
    if config.CLAUDE_MODEL:
        command += ['--model', config.CLAUDE_MODEL]
    command.append(message)

    completed = _run_cli(command, cwd, env, timeout)

    payload: Optional[Dict[str, Any]] = None
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            candidate = json.loads(line)
        except Exception:
            continue
        if isinstance(candidate, dict) and candidate.get('type') == 'result':
            payload = candidate

    if payload is None:
        detail = (completed.stderr or completed.stdout or '').strip()[-800:]
        raise BridgeError(f'claude CLI returned no result (exit={completed.returncode}): {detail}')

    if payload.get('is_error'):
        raise BridgeError(f'claude CLI error: {str(payload.get("result"))[:800]}')

    return {
        'session_id': payload.get('session_id') or target_id,
        'reply': str(payload.get('result') or '').strip(),
        'is_new_session': is_new,
        'usage': payload.get('usage'),
        'cost_usd': payload.get('total_cost_usd'),
    }


def _call_codex(message: str, session_id: Optional[str], cwd: str, env: Dict[str, str],
                timeout: int, ui_shim: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deliver to a Codex thread and return its final message.

    With a shim available the turn is started on the app-server the editor panel is attached
    to, so the exchange shows up in the panel. Otherwise the thread is resumed over the CLI,
    which keeps the context but stays invisible to the panel until it is reopened.
    """
    if ui_shim:
        return _call_via_panel(message, session_id, ui_shim, timeout)

    is_new = session_id is None

    if is_new:
        command = [config.CODEX_BIN, 'exec', '--json', '--skip-git-repo-check',
                   '-s', config.CODEX_SANDBOX, '-C', cwd]
        if config.CODEX_MODEL:
            command += ['-m', config.CODEX_MODEL]
    else:
        # `exec resume` intentionally exposes no sandbox/cwd flags: it inherits the
        # settings the live session was started with.
        command = [config.CODEX_BIN, 'exec', 'resume', session_id, '--json', '--skip-git-repo-check']
    command.append(message)

    completed = _run_cli(command, cwd, env, timeout)

    thread_id: Optional[str] = None
    reply = ''
    errors: List[str] = []

    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue

        event_type = event.get('type')
        if event_type == 'thread.started':
            thread_id = event.get('thread_id') or thread_id
        elif event_type == 'item.completed':
            item = event.get('item') or {}
            if item.get('type') == 'agent_message' and item.get('text'):
                reply = str(item['text'])
        elif event_type in ('error', 'turn.failed'):
            errors.append(json.dumps(event, ensure_ascii=False)[:400])

    if not reply:
        detail = '; '.join(errors) or (completed.stderr or completed.stdout or '').strip()[-800:]
        raise BridgeError(f'codex CLI returned no agent message (exit={completed.returncode}): {detail}')

    return {
        'session_id': thread_id or session_id or '',
        'reply': reply.strip(),
        'is_new_session': is_new,
        'usage': None,
        'cost_usd': None,
    }


CALLERS = {config.AGENT_CLAUDE: _call_claude, config.AGENT_CODEX: _call_codex}


# ------------------------------------------------------------------ dispatch

PANEL_SETTING = {
    config.AGENT_CODEX: 'chatgpt.cliExecutable -> codex-shim.sh',
    config.AGENT_CLAUDE: 'claudeCode.claudeProcessWrapper -> claude-shim.sh',
}


def _resolve_ide_panel(target_agent: str, exclude_ids: List[str], session_id: Optional[str],
                       cwd: str) -> Optional[Dict[str, Any]]:
    """The peer session open in this editor window, reached through its panel shim.

    An explicit session id wins, then a sticky pin, then the tab the user last typed into.
    A requested session that is not open in any panel falls through to the CLI path.
    """
    if not uihook.is_enabled():
        return None

    wanted = session_id
    if not wanted:
        pin = registry.get_pin(target_agent, cwd)
        if pin and pin.get('is_sticky'):
            wanted = pin.get('session_id')

    live = uihook.find_live_session(target_agent, wanted)
    if not live and wanted:
        return None

    if not live or live.get('session_id') in exclude_ids:
        if config.UI_HOOK_MODE == uihook.UI_HOOK_REQUIRE:
            raise BridgeError(
                f'CROSS_AGENT_UI_HOOK=require but no live {target_agent} panel session was found '
                f'for this editor window. Check that {PANEL_SETTING.get(target_agent)} is set and '
                'that a panel is open.')
        return None

    return {
        'agent': target_agent,
        'session_id': live['session_id'],
        'cwd': live.get('cwd'),
        'source': 'ide-panel',
        'ui_shim': live['shim'],
        'is_active': True,
        'mtime': live.get('last_seen', time.time()),
    }


def _resolve_target(target_agent: str, session_id: Optional[str], scope: str, cwd: str,
                    is_new_forced: bool, exclude_ids: List[str]) -> Optional[Dict[str, Any]]:
    if is_new_forced:
        return None

    # the panel the user is actually looking at wins over anything inferred from transcripts
    panel = _resolve_ide_panel(target_agent, exclude_ids, session_id, cwd)
    if panel:
        return panel

    if session_id:
        found = discovery.find_session(target_agent, session_id)
        if not found:
            raise BridgeError(f'{target_agent} session not found: {session_id}')
        found['source'] = 'explicit'
        return found

    return discovery.find_active_session(target_agent, scope, cwd, exclude_ids)


def send_message(target_agent: str, message: str, session_id: Optional[str] = None,
                 is_new_session: bool = False, scope: Optional[str] = None,
                 cwd: Optional[str] = None, timeout: Optional[int] = None,
                 conversation_id: Optional[str] = None, is_raw: bool = False,
                 allows_same_agent: bool = False) -> Dict[str, Any]:
    """Relay `message` to the peer agent's active session and return its reply."""
    started_at = time.time()
    config.ensure_dirs()

    if not message or not message.strip():
        raise BridgeError('message must not be empty')

    scope = scope or config.DEFAULT_SCOPE
    if scope not in discovery.SCOPES:
        raise BridgeError(f"scope must be one of {', '.join(discovery.SCOPES)}, got: {scope}")

    cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()
    timeout = timeout or config.SEND_TIMEOUT_SECONDS

    identity = caller.detect_caller()
    sender_agent = identity['agent']

    # The caller's own session is the freshest transcript on its own side, rooted at the
    # directory this server was launched from - not at the `cwd` argument, which may point
    # anywhere. Never relay into it, or the agent would end up waiting for itself.
    self_session_id: Optional[str] = None
    if sender_agent in CALLERS:
        own = discovery.find_active_session(sender_agent, discovery.SCOPE_CWD, os.getcwd())
        self_session_id = own['session_id'] if own else None

    if sender_agent == target_agent and not allows_same_agent and not session_id:
        raise BridgeError(
            f'refusing to relay a message from {target_agent} back into {target_agent}. '
            f'Use `{PEER_TOOL.get(sender_agent, "the peer tool")}` to reach the other agent, '
            'or pass an explicit session_id together with allows_same_agent=true.')

    busy = _busy_from_env()
    exclude_ids = [s.split(':', 1)[1] for s in busy if s.startswith(target_agent + ':')]
    if sender_agent == target_agent and self_session_id:
        exclude_ids.append(self_session_id)

    conversation_id, is_new_conversation = _resolve_conversation_id(conversation_id)
    hops_used = registry.get_conversation(conversation_id).get('hops', 0)
    if hops_used >= config.MAX_HOPS:
        raise BridgeError(
            f'conversation {conversation_id} reached the hop limit ({config.MAX_HOPS}). '
            'Answer with what you already have instead of relaying again.')

    target = _resolve_target(target_agent, session_id, scope, cwd, is_new_session, exclude_ids)
    target_id = target['session_id'] if target else None

    # The claim comes before the hop is charged, so a relay that loses the race for a busy
    # session does not consume budget. A brand new session has no transcript to protect yet.
    with contextlib.ExitStack() as stack:
        if target_id:
            try:
                stack.enter_context(registry.busy_lock(target_agent, target_id, conversation_id))
            except registry.SessionBusyError as e:
                raise BridgeError(
                    f'{target_agent} session {target_id} is already waiting on a bridged reply '
                    f'(conversation {e.holder.get("conversation_id")}). Answer directly instead '
                    'of relaying back into it.')

        # Mark our own session busy too. Panel delivery bypasses the child-process env, so this
        # file lock is what stops the peer from relaying straight back into a session that is
        # already blocked waiting for it.
        if self_session_id:
            with contextlib.suppress(registry.SessionBusyError):
                stack.enter_context(
                    registry.busy_lock(sender_agent, self_session_id, conversation_id))

        record = registry.bump_conversation(conversation_id, sender_agent, target_agent)
        hop = int(record.get('hops', 1))
        remaining = max(config.MAX_HOPS - hop, 0)

        payload = message if is_raw else _build_envelope(
            sender_agent, target_agent, conversation_id, hop, remaining, message)

        run_cwd = cwd
        if target and target.get('cwd') and os.path.isdir(target['cwd']):
            run_cwd = target['cwd']

        child_busy = list(busy)
        if self_session_id:
            child_busy.append(f'{sender_agent}:{self_session_id}')
        if target_id:
            child_busy.append(f'{target_agent}:{target_id}')

        env = _child_env(conversation_id, hop, sender_agent, child_busy)

        logger.info(f'send_message [BEGIN]: {sender_agent}->{target_agent} '
                    f'session={target_id or "NEW"} conv={conversation_id} hop={hop}')

        ui_shim = (target or {}).get('ui_shim')
        result = CALLERS[target_agent](payload, target_id, run_cwd, env, timeout, ui_shim)

    if result['is_new_session'] and result['session_id']:
        registry.set_pin(target_agent, cwd, result['session_id'], run_cwd,
                         is_sticky=False, is_bridge_created=True)
    elif target_id:
        registry.touch_pin(target_agent, cwd)

    logger.info(f'send_message [END]: {sender_agent}->{target_agent} '
                f'session={result["session_id"]} chars={len(result["reply"])}')

    return {
        'ok': True,
        'reply': result['reply'],
        'target_agent': target_agent,
        'target_session_id': result['session_id'],
        'session_origin': 'created' if result['is_new_session'] else (target or {}).get('source', 'unknown'),
        'was_session_created': result['is_new_session'],
        'delivery': 'ide-panel' if (target or {}).get('ui_shim') else 'cli-resume',
        'is_visible_in_panel': bool((target or {}).get('ui_shim')),
        'sender_agent': sender_agent,
        'conversation_id': conversation_id,
        'is_new_conversation': is_new_conversation,
        'hop': hop,
        'hops_remaining': remaining,
        'scope': scope,
        'cwd': run_cwd,
        'elapsed_seconds': round(time.time() - started_at, 1),
        'cost_usd': result.get('cost_usd'),
    }
