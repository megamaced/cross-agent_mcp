"""Relay a message into the peer agent's live session.

  Claude Code : claude -p --resume <session-id> --output-format json "<message>"
  Codex       : codex exec resume <thread-id> --json "<message>"

Both commands re-enter an existing transcript, so the peer answers with its whole
conversation context intact instead of starting from a blank agent.

Handing the message over is not the same as waiting for the answer. `send_message` only
queues the delivery and returns; `outbox` runs it, and the peer's answer comes back as a
message into the sender's session rather than as this function's return value. Nothing is
locked open for the length of a peer turn, so a turn may take as long as it needs.
"""

import atexit
import contextlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import caller, config, discovery, outbox, registry, uihook


logger = logging.getLogger('cross_agent_mcp.bridge')

# how long a killed process group gets to die before it is SIGKILLed
KILL_GRACE_SECONDS = 5

# how much of the relayed message is used to title a conversation the bridge opened
PANEL_TITLE_LIMIT = 50

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


def _address_line(sender: str, reply_to: Optional[str]) -> str:
    """The sender's own address, written on the envelope the way an email carries From.

    The bridge already routes the answer on its own, so this is not what makes a reply work.
    It matters when the automatic path cannot: it lets the peer send a NEW request straight
    back to the exact session that wrote to it, instead of re-deriving the address from
    whatever happens to look active on this side.
    """
    if not reply_to:
        return ('reply-to: (unknown - this sender has no session of its own, so an automatic '
                'answer cannot be delivered back)\n')
    return f'reply-to: {sender} session {reply_to}\n'


def _build_envelope(sender: str, target: str, conversation_id: str, hop: int, remaining: int,
                    message: str, reply_to: Optional[str] = None) -> str:
    sender_label = AGENT_LABEL.get(sender, sender)
    reply_tool = PEER_TOOL.get(target, 'the cross-agent tool')

    if remaining > 0 and reply_to:
        follow_up = (f'- For a NEW request back to {sender_label}, call `{reply_tool}` with '
                     f'session_id="{reply_to}" ({remaining} bridge hop(s) left). Otherwise '
                     'just answer.')
    elif remaining > 0:
        follow_up = (f'- If you need to send a NEW request back to {sender_label}, call the '
                     f'`{reply_tool}` tool ({remaining} bridge hop(s) left). Otherwise just answer.')
    else:
        follow_up = ('- The hop budget for this conversation is exhausted. Do NOT call any '
                     'cross-agent tool; answer directly.')

    return (
        '=== CROSS-AGENT BRIDGE MESSAGE ===\n'
        f'from: {sender_label} (peer AI agent, not the human user)\n'
        f'{_address_line(sender, reply_to)}'
        f'conversation: {conversation_id} | hop {hop}/{config.MAX_HOPS}\n'
        '\n'
        f'{message}\n'
        '\n'
        '=== HOW TO REPLY ===\n'
        f'- Your final assistant message is relayed back to {sender_label} as a message in its\n'
        '  own session. Take the time the task needs; nothing is parked waiting on you.\n'
        '- Answer the peer directly; do not wait for the human and do not ask for confirmation.\n'
        '- Keep the answer self-contained: the peer sees only your final message.\n'
        f'{follow_up}\n'
    )


def _build_reply_envelope(sender: str, target: str, conversation_id: str, hop: int,
                          remaining: int, reply: str, reply_to: Optional[str] = None,
                          is_recovered: bool = False) -> str:
    """Wrap a peer's answer so the original sender reads it as an answer, not a new request.

    A recovered answer says so. Recovery reads the peer's last message at the moment the
    transport gave up, and a peer that is still working has a last message too - a line about
    what it is doing next. Delivered unmarked, that reads exactly like a finished answer, and
    the reader acts on a report that was never made.
    """
    sender_label = AGENT_LABEL.get(sender, sender)
    reply_tool = PEER_TOOL.get(target, 'the cross-agent tool')

    if remaining > 0 and reply_to:
        follow_up = (f'- Only if you have a NEW request, call `{reply_tool}` with '
                     f'session_id="{reply_to}" ({remaining} bridge hop(s) left).')
    elif remaining > 0:
        follow_up = (f'- Only if you have a NEW request, call `{reply_tool}` '
                     f'({remaining} bridge hop(s) left).')
    else:
        follow_up = ('- The hop budget for this conversation is exhausted. Do NOT call any '
                     'cross-agent tool.')

    if is_recovered:
        provenance = (
            f'- RECOVERED, NOT RECEIVED. Delivery from {sender_label} failed, so this was read\n'
            f'  out of its transcript: it is whatever {sender_label} had last said at that\n'
            '  moment, which may be a note about what it was still doing rather than its\n'
            '  answer. Treat it as finished only if it reads like a finished answer, and check\n'
            f'  with {sender_label} before acting on it as a report.\n')
    else:
        provenance = '- This is the answer to a message you relayed earlier.\n'

    return (
        '=== CROSS-AGENT BRIDGE REPLY ===\n'
        f'from: {sender_label} (peer AI agent, not the human user)\n'
        f'{_address_line(sender, reply_to)}'
        f'conversation: {conversation_id} | answering hop {hop}/{config.MAX_HOPS}'
        f'{" | recovered from transcript" if is_recovered else ""}\n'
        '\n'
        f'{reply}\n'
        '\n'
        '=== NOTE ===\n'
        f'{provenance}'
        '- Nothing is waiting on you.\n'
        f'{follow_up}\n'
    )


def _summary(text: str) -> str:
    """A one-line trace of a message, for delivery listings."""
    first_line = next((line.strip() for line in text.splitlines()
                       if line.strip() and not line.startswith('===')), '')
    return ' '.join(first_line.split())[:PANEL_TITLE_LIMIT * 2]


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


# Deliveries this process started and has not finished. A CLI runs in its own process group
# so a timeout can take down the whole tool tree with it - which also means it survives us.
_LIVE_CHILDREN: List[subprocess.Popen] = []
_CHILDREN_GUARD = threading.Lock()


def _track_child(process: subprocess.Popen) -> None:
    with _CHILDREN_GUARD:
        _LIVE_CHILDREN.append(process)


def _untrack_child(process: subprocess.Popen) -> None:
    with _CHILDREN_GUARD:
        with contextlib.suppress(ValueError):
            _LIVE_CHILDREN.remove(process)


def terminate_live_children() -> None:
    """Take our deliveries down with us.

    An orphaned delivery is not merely wasted work: it keeps writing to the peer's session
    and its repository with nobody watching, and the busy lock stops protecting that session
    the moment this process dies, because staleness is judged by our pid. A re-request then
    starts a second agent on the same files. That happened - two `claude -p` resumes ran
    concurrently on one session after the window that started the first was reloaded.
    """
    with _CHILDREN_GUARD:
        children = list(_LIVE_CHILDREN)
        _LIVE_CHILDREN.clear()

    for process in children:
        if process.poll() is not None:
            continue
        logger.info(f'terminate_live_children [killing]: pid={process.pid}')
        with contextlib.suppress(Exception):
            _terminate_group(process)


def install_shutdown_guard() -> None:
    """Arrange for in-flight deliveries to die with this server, however it exits."""
    atexit.register(terminate_live_children)

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            previous = signal.getsignal(number)
        except (OSError, ValueError):
            continue

        def handler(signum, frame, _previous=previous):
            terminate_live_children()
            if callable(_previous):
                _previous(signum, frame)
            else:
                raise SystemExit(128 + signum)

        with contextlib.suppress(OSError, ValueError):
            signal.signal(number, handler)


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

    _track_child(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(f'_run_cli [exception]: timeout after {timeout}s, killing process group')
        _terminate_group(process)
        with contextlib.suppress(Exception):
            process.communicate(timeout=KILL_GRACE_SECONDS)
        raise BridgeError(f'peer agent did not answer within {timeout}s')
    finally:
        _untrack_child(process)
        logger.debug('_run_cli [END]')

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _panel_title(sender_agent: str, message: str) -> str:
    """A list entry the user can recognise, instead of the panel's default "New chat"."""
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), '')
    label = AGENT_LABEL.get(sender_agent, sender_agent)
    return f'{label}: {" ".join(first_line.split())[:PANEL_TITLE_LIMIT]}'


def _call_via_panel(message: str, session_id: Optional[str], ui_shim: Dict[str, Any],
                    timeout: int, cwd: str, title: Optional[str] = None) -> Dict[str, Any]:
    """Deliver through the editor panel shim, so the exchange shows up in the panel."""
    response = uihook.send(message, ui_shim, session_id, timeout, cwd, title)
    if not response.get('ok'):
        raise BridgeError(f'IDE panel relay failed: {response.get("error")}')

    return {
        'session_id': response.get('sessionId') or session_id or '',
        'reply': str(response.get('reply') or '').strip(),
        'is_new_session': bool(response.get('wasCreated')),
        'usage': None,
        'cost_usd': None,
    }


def _call_claude(message: str, session_id: Optional[str], cwd: str, env: Dict[str, str],
                 timeout: int, ui_shim: Optional[Dict[str, Any]] = None,
                 title: Optional[str] = None) -> Dict[str, Any]:
    """Resume (or create) a Claude Code session and return its final message."""
    if ui_shim:
        return _call_via_panel(message, session_id, ui_shim, timeout, cwd, title)

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
                timeout: int, ui_shim: Optional[Dict[str, Any]] = None,
                title: Optional[str] = None) -> Dict[str, Any]:
    """Deliver to a Codex thread and return its final message.

    With a shim available the turn is started on the app-server the editor panel is attached
    to, so the exchange shows up in the panel. Otherwise the thread is resumed over the CLI,
    which keeps the context but stays invisible to the panel until it is reopened.
    """
    if ui_shim:
        return _call_via_panel(message, session_id, ui_shim, timeout, cwd, title)

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


def _panel_session(target_agent: str, wanted_id: Optional[str],
                   exclude_ids: List[str]) -> Optional[Dict[str, Any]]:
    """An already-open conversation in this window's panel, or None."""
    if not uihook.is_enabled():
        return None

    live = uihook.find_live_session(target_agent, wanted_id)
    if not live or live['session_id'] in exclude_ids:
        if not wanted_id and config.UI_HOOK_MODE == uihook.UI_HOOK_REQUIRE:
            raise BridgeError(
                f'CROSS_AGENT_UI_HOOK=require but no live {target_agent} panel session was found '
                f'for this editor window. Check that {PANEL_SETTING.get(target_agent)} is set and '
                'that a panel is open.')
        return None

    return {
        'agent': target_agent,
        'session_id': live['session_id'],
        'cwd': live.get('cwd'),
        'source': ('ide-panel-other-window' if live.get('is_foreign_window') else 'ide-panel'),
        'ui_shim': live['shim'],
        'is_active': True,
        'mtime': live.get('last_seen', time.time()),
    }


def _new_panel_conversation(target_agent: str) -> Optional[Dict[str, Any]]:
    """Last resort: a panel process that can host a fresh conversation."""
    if not uihook.is_enabled():
        return None

    host = uihook.find_panel_host(target_agent)
    if not host:
        return None

    return {
        'agent': target_agent,
        'session_id': None,
        'cwd': None,
        'source': 'ide-panel-new',
        'ui_shim': host['shim'],
        'is_active': True,
        'mtime': time.time(),
    }


def _requested_session_id(target_agent: str, session_id: Optional[str],
                          cwd: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve what the caller asked for into a session id.

    `session_id` may be a real id or the conversation's name, because that is what a human
    hands the agent. A sticky pin stands in when nothing was named.
    """
    if session_id:
        if discovery.find_session(target_agent, session_id):
            return session_id, session_id
        named = discovery.find_session_by_name(target_agent, session_id)
        if named:
            logger.info(f'_requested_session_id [resolved by name]: '
                        f'{session_id!r} -> {named["session_id"]}')
            return named['session_id'], session_id
        near = discovery.suggest_session_names(target_agent, session_id)
        hint = (f' Titles containing it: {", ".join(repr(t) for t in near)}. Names match '
                'exactly, so pass one of these in full or use the session id.'
                if near else '')
        raise BridgeError(
            f'no {target_agent} session is named {session_id!r}, and no session has that id. '
            f'Nothing was sent and no session was created.{hint} '
            'Use list_agent_sessions to see what exists.')

    pin = registry.get_pin(target_agent, cwd)
    if pin and pin.get('is_sticky'):
        return pin.get('session_id'), None
    return None, None


def _resolve_target(target_agent: str, session_id: Optional[str], scope: str, cwd: str,
                    is_new_forced: bool, exclude_ids: List[str]) -> Optional[Dict[str, Any]]:
    """Pick the conversation a relay lands in.

    Starting a fresh conversation is the LAST resort: it silently drops whatever context the
    caller meant to reach, which in the middle of a long task looks like the peer forgetting
    everything. Everything else is tried first.
    """
    if is_new_forced:
        return _new_panel_conversation(target_agent)

    wanted_id, requested = _requested_session_id(target_agent, session_id, cwd)

    # 1. the session that was named (or pinned) - in the panel if it happens to be open there
    if wanted_id:
        panel = _panel_session(target_agent, wanted_id, exclude_ids)
        if panel:
            return panel

        found = discovery.find_session(target_agent, wanted_id)
        if not found:
            label = requested or wanted_id
            raise BridgeError(
                f'{target_agent} session {label!r} no longer exists. Nothing was sent and no '
                'session was created; clear the pin with pin_agent_session or name another one.')
        found['source'] = 'name' if requested else 'pin'
        return found

    # 2. the conversation the user is working in, in this window's panel
    panel = _panel_session(target_agent, None, exclude_ids)
    if panel:
        return panel

    # 3. an existing session on disk. The panel will not show the exchange, but the peer keeps
    #    its context - always better than starting over
    active = discovery.find_active_session(target_agent, scope, cwd, exclude_ids)
    if active:
        return active

    # 4. nothing to resume anywhere
    return _new_panel_conversation(target_agent)


# --------------------------------------------------------- outbox delivery

def _deliver(job: outbox.Job) -> Dict[str, Any]:
    """Run one queued delivery. Called on an outbox worker thread, never on the caller's."""
    result = CALLERS[job.target_agent](job.payload, job.target_session_id, job.run_cwd,
                                       job.env, job.timeout, job.ui_shim, job.title)

    if result['is_new_session'] and result['session_id']:
        registry.set_pin(job.target_agent, job.pin_cwd, result['session_id'], job.run_cwd,
                         is_sticky=False, is_bridge_created=True)
    elif job.target_session_id:
        registry.touch_pin(job.target_agent, job.pin_cwd)
    return result


def _build_reply_job(job: outbox.Job, reply: str) -> Optional[outbox.Job]:
    """Turn the peer's answer into a delivery aimed back at whoever started the exchange.

    The answer does not spend a hop: it closes the hop the request already paid for. Only a
    genuinely new request costs budget, which keeps MAX_HOPS meaning what it used to mean.
    """
    if not job.sender_session_id:
        logger.info(f'_build_reply_job [skipped]: {job.delivery_id} has no sender session to '
                    'answer into; the reply is only in the peer transcript')
        return None

    hop = int(registry.get_conversation(job.conversation_id).get('hops', job.hop))
    remaining = max(config.MAX_HOPS - hop, 0)
    answering_id = job.resolved_session_id or job.target_session_id
    payload = _build_reply_envelope(job.target_agent, job.sender_agent, job.conversation_id,
                                    hop, remaining, reply, answering_id,
                                    is_recovered=job.is_reply_recovered)

    # resolved fresh: the sender's panel may have opened, closed or moved during the turn
    panel: Optional[Dict[str, Any]] = None
    with contextlib.suppress(Exception):
        panel = _panel_session(job.sender_agent, job.sender_session_id, [])

    # An answer runs where the sender's session lives, not where the request was aimed. A
    # Claude transcript is filed under its own project directory, so resuming it from the
    # target's directory fails with "No conversation found" even though the session is fine.
    reply_cwd = job.pin_cwd
    known = discovery.find_session(job.sender_agent, job.sender_session_id)
    if known and known.get('cwd') and os.path.isdir(known['cwd']):
        reply_cwd = known['cwd']

    child_busy = [f'{job.target_agent}:{answering_id}'] if answering_id else []

    return outbox.Job(
        target_agent=job.sender_agent,
        target_session_id=job.sender_session_id,
        payload=payload,
        run_cwd=reply_cwd,
        pin_cwd=reply_cwd,
        env=_child_env(job.conversation_id, hop, job.target_agent, child_busy),
        timeout=job.timeout,
        ui_shim=(panel or {}).get('ui_shim'),
        title=_panel_title(job.target_agent, reply),
        conversation_id=job.conversation_id,
        hop=hop,
        sender_agent=job.target_agent,
        sender_session_id=answering_id,
        wants_reply=False,
        summary=_summary(reply),
    )


def _recover_reply(job: outbox.Job) -> Optional[str]:
    """Read the peer's answer out of its own transcript when the transport did not bring it.

    Both agents write every turn to a JSONL transcript, so a delivery that reached the peer
    has its answer on disk even when the process carrying it died first. Recovery costs one
    file read and cannot ask the peer to redo the work, which re-sending would.
    """
    session_id = job.resolved_session_id or job.target_session_id
    if not session_id:
        return None
    # Only what the peer wrote after we reached it can be an answer to what we asked.
    return discovery.last_agent_message(job.target_agent, session_id, after=job.started_at)


outbox.OUTBOX.deliver = _deliver
outbox.OUTBOX.build_reply = _build_reply_job
outbox.OUTBOX.recover = _recover_reply


def _own_session_id(sender_agent: str) -> Optional[str]:
    """The caller's own session - the return address the peer's answer is delivered to.

    Asked of the panel first, which knows it exactly: the shim hosting this process is an
    ancestor of it. Only when there is no panel does this fall back to the transcript on
    disk, and even then without pins - a pin says where to send, and letting it answer this
    question addressed a reply to a session that had not existed for weeks.
    """
    if sender_agent not in CALLERS:
        return None

    if uihook.is_enabled():
        own = uihook.find_own_session(sender_agent)
        if own:
            return own['session_id']

    # rooted at the directory this server was launched from, not at the `cwd` argument,
    # which may point anywhere
    found = discovery.find_active_session(
        sender_agent, discovery.SCOPE_CWD, os.getcwd(), use_pin=False)
    return found['session_id'] if found else None


def send_message(target_agent: str, message: str, session_id: Optional[str] = None,
                 is_new_session: bool = False, scope: Optional[str] = None,
                 cwd: Optional[str] = None, timeout: Optional[int] = None,
                 conversation_id: Optional[str] = None, is_raw: bool = False,
                 allows_same_agent: bool = False) -> Dict[str, Any]:
    """Queue `message` for the peer agent's active session and return once it is accepted.

    The peer's answer is not this function's return value. It arrives later as a message in
    the caller's own session, delivered by the outbox.
    """
    started_at = time.time()
    config.ensure_dirs()

    if not message or not message.strip():
        raise BridgeError('message must not be empty')

    scope = scope or config.DEFAULT_SCOPE
    if scope not in discovery.SCOPES:
        raise BridgeError(f"scope must be one of {', '.join(discovery.SCOPES)}, got: {scope}")

    cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else os.getcwd()

    # The timeout bounds the peer's turn, not the caller's wait - nobody waits any more. So a
    # value below the configured budget has no upside and one real effect: it kills work that
    # would have finished. Callers carried the habit over from when this blocked, and turns
    # were being cut off at 30s and 120s while peer turns here run 216s..660s.
    requested_timeout = timeout
    timeout = max(timeout or config.SEND_TIMEOUT_SECONDS, config.SEND_TIMEOUT_SECONDS)

    identity = caller.detect_caller()
    sender_agent = identity['agent']

    self_session_id = _own_session_id(sender_agent)

    # Naming a session and asking for a brand new one are opposite intentions. Honouring both
    # would open a fresh conversation while the caller believes it reached the one it named.
    if session_id and is_new_session:
        raise BridgeError(
            'session_id and new_session cannot be combined: one targets an existing '
            f'conversation, the other opens a new one. Nothing was sent. Drop new_session to '
            f'reach {session_id!r}, or drop session_id to start a new conversation.')

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

    record = registry.bump_conversation(conversation_id, sender_agent, target_agent)
    hop = int(record.get('hops', 1))
    remaining = max(config.MAX_HOPS - hop, 0)

    payload = message if is_raw else _build_envelope(
        sender_agent, target_agent, conversation_id, hop, remaining, message, self_session_id)

    run_cwd = cwd
    if target and target.get('cwd') and os.path.isdir(target['cwd']):
        run_cwd = target['cwd']

    # Only the session actually being written to is off limits. The sender is deliberately
    # left out: it is not parked waiting any more, so the peer relaying back into it is a
    # normal message rather than a deadlock.
    child_busy = list(busy)
    if target_id:
        child_busy.append(f'{target_agent}:{target_id}')

    job = outbox.Job(
        target_agent=target_agent,
        target_session_id=target_id,
        payload=payload,
        run_cwd=run_cwd,
        pin_cwd=cwd,
        env=_child_env(conversation_id, hop, sender_agent, child_busy),
        timeout=timeout,
        ui_shim=(target or {}).get('ui_shim'),
        title=_panel_title(sender_agent, message),
        conversation_id=conversation_id,
        hop=hop,
        sender_agent=sender_agent,
        sender_session_id=self_session_id,
        wants_reply=True,
        summary=_summary(message),
    )
    delivery_id = outbox.OUTBOX.submit(job)

    logger.info(f'send_message [accepted]: {sender_agent}->{target_agent} '
                f'session={target_id or "NEW"} conv={conversation_id} hop={hop} '
                f'delivery={delivery_id}')

    is_new_target = target_id is None
    warnings: List[str] = []
    if is_new_target:
        warnings.append(
            f'No existing {target_agent} session was reachable for {run_cwd}, so a NEW '
            'conversation will be started. It has none of the earlier context. Tell the user '
            'this happened, and pass session_id (an id or the conversation name) or '
            'pin_agent_session to target a specific one.')
    if not self_session_id:
        warnings.append(
            'Your own session could not be identified, so the peer\'s answer cannot be '
            'delivered back here. It will exist only in the peer\'s transcript.')
    if requested_timeout is not None and requested_timeout < timeout:
        warnings.append(
            f'timeout={requested_timeout}s was raised to {timeout}s. It bounds the peer\'s '
            'turn, not your wait - this call already returned - so a shorter value only '
            'aborts work that would have finished. Pass a larger one to allow more time.')

    return {
        'ok': True,
        'accepted': True,
        'delivery_id': delivery_id,
        'note': ('Queued, not answered. This result carries no reply: the peer\'s answer '
                 'arrives later as a separate message in this session. Do not invent, predict '
                 'or wait for it - finish what you are doing and report that the message was '
                 'sent. Check bridge_status for delivery state.'),
        'warning': ' '.join(warnings) or None,
        'target_agent': target_agent,
        'target_session_id': target_id,
        'session_origin': 'created' if is_new_target else (target or {}).get('source', 'unknown'),
        'will_create_session': is_new_target,
        'delivery': 'ide-panel' if (target or {}).get('ui_shim') else 'cli-resume',
        'is_visible_in_panel': bool((target or {}).get('ui_shim')),
        'queue_depth': outbox.OUTBOX.depth(job.key()),
        'sender_agent': sender_agent,
        'reply_lands_in_session': self_session_id,
        'conversation_id': conversation_id,
        'is_new_conversation': is_new_conversation,
        'hop': hop,
        'hops_remaining': remaining,
        'scope': scope,
        'cwd': run_cwd,
        'elapsed_seconds': round(time.time() - started_at, 1),
    }
