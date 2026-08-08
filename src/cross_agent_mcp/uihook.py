"""Reach the peer session that is open in *this* editor window.

Each panel shim registers a socket together with the pid chain it was launched from. The
Codex extension and the Claude Code extension are both children of the same VS Code extension
host, so the shim whose ancestry shares the nearest pid with this process belongs to the
window the user is looking at.
"""

import glob
import json
import logging
import os
import socket
from typing import Any, Dict, List, Optional

from . import config
from .panel import REGISTRY_DIR, process_ancestry


logger = logging.getLogger('cross_agent_mcp.uihook')

CONNECT_TIMEOUT_SECONDS = 3

UI_HOOK_AUTO = 'auto'
UI_HOOK_OFF = 'off'
UI_HOOK_REQUIRE = 'require'


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        return getattr(e, 'errno', None) == 1  # EPERM means it exists but is not ours
    return True


def list_shims(agent: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every registered panel shim, with dead registrations cleaned up."""
    shims: List[Dict[str, Any]] = []
    for path in glob.glob(REGISTRY_DIR + '*.json'):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                record = json.load(f)
        except Exception:
            continue

        pid = int(record.get('pid', -1))
        if not _is_pid_alive(pid) or not os.path.exists(record.get('socket', '')):
            try:
                os.remove(path)
            except OSError:
                pass
            continue

        if agent and record.get('agent') != agent:
            continue

        record['registry_path'] = path
        shims.append(record)
    return shims


def find_local_shims(agent: str) -> List[Dict[str, Any]]:
    """Every shim for this agent belonging to this editor window.

    An extension keeps one process per open conversation, so a single window normally has
    several. They all share the same extension host, so the nearest shared ancestor selects
    the window and the list keeps every tab inside it.
    """
    own_chain = process_ancestry(os.getpid())
    scored: List[tuple] = []

    for shim in list_shims(agent):
        theirs = {int(p) for p in shim.get('ancestors') or []}
        for distance, pid in enumerate(own_chain):
            if pid > 1 and pid in theirs:
                scored.append((distance, shim))
                break

    if not scored:
        return []

    nearest = min(distance for distance, _ in scored)
    local = []
    for distance, shim in scored:
        if distance == nearest:
            shim['shared_ancestor_distance'] = distance
            local.append(shim)
    return local


def find_local_shim(agent: str) -> Optional[Dict[str, Any]]:
    """The single shim for this window, when there is only one worth talking about."""
    session = find_live_session(agent)
    if session:
        return session['shim']
    shims = find_local_shims(agent)
    return shims[0] if shims else None


def _request(socket_path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(CONNECT_TIMEOUT_SECONDS)
    try:
        connection.connect(socket_path)
        connection.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))

        connection.settimeout(timeout)
        buffer = b''
        while b'\n' not in buffer:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
        if not buffer:
            return {'ok': False, 'error': 'shim closed the connection without answering'}
        return json.loads(buffer.split(b'\n', 1)[0].decode('utf-8'))
    finally:
        try:
            connection.close()
        except OSError:
            pass


def read_status(shim: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _request(shim['socket'], {'op': 'status'}, CONNECT_TIMEOUT_SECONDS)
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def find_live_sessions(agent: str) -> List[Dict[str, Any]]:
    """Every panel session of this window, the one the user is working in first.

    A window keeps one process per conversation tab and nothing anywhere records which tab is
    focused, so the ordering is built from the strongest evidence available:

      1. when the human last typed into that tab, observed by the shim
      2. failing that (no tab has been typed into since the shims started, e.g. right after a
         window reload) the transcript's last write, which survives across restarts
      3. failing that, the most recently launched process - the most recently opened tab

    Observed input strictly outranks transcript time, because a bridged turn also touches the
    transcript and must never make the bridge keep picking its own last target.
    """
    sessions: List[Dict[str, Any]] = []

    for shim in find_local_shims(agent):
        status = read_status(shim)
        if not status.get('ok'):
            continue
        shim_activity = float(status.get('last_user_activity') or 0)

        for entry in (status.get('sessions') or status.get('threads') or []):
            session = dict(entry)
            session['session_id'] = session.get('session_id') or session.get('thread_id')
            if not session['session_id']:
                continue
            session['shim'] = shim
            session['shim_pid'] = shim.get('pid')
            session['last_user_activity'] = float(
                session.get('last_user_activity') or shim_activity)
            session['started_at'] = float(shim.get('started_at') or 0)
            session['transcript_mtime'] = _transcript_mtime(agent, session['session_id'])
            sessions.append(session)

    has_observed_input = any(s['last_user_activity'] for s in sessions)
    if has_observed_input:
        sessions.sort(key=lambda s: (s['last_user_activity'], s['started_at']), reverse=True)
    else:
        sessions.sort(key=lambda s: (s['transcript_mtime'], s['started_at']), reverse=True)
    return sessions


def _transcript_mtime(agent: str, session_id: str) -> float:
    from . import discovery
    try:
        found = discovery.find_session(agent, session_id)
    except Exception:
        return 0.0
    return float((found or {}).get('mtime') or 0)


def find_live_session(agent: str, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The panel session to deliver to: a specific one when asked, else the active one."""
    sessions = find_live_sessions(agent)
    if session_id:
        return next((s for s in sessions if s['session_id'] == session_id), None)
    return sessions[0] if sessions else None


def send(text: str, shim: Dict[str, Any], session_id: Optional[str], timeout: int) -> Dict[str, Any]:
    """Hand a message to the live panel session and wait for the turn to finish."""
    payload: Dict[str, Any] = {'op': 'send', 'text': text, 'timeout': timeout}
    if session_id:
        payload['sessionId'] = session_id

    logger.info(f'send [BEGIN]: via {shim.get("agent")} panel shim '
                f'pid={shim.get("pid")} session={session_id}')
    try:
        # the shim answers only once the turn completes, so allow the full turn budget
        return _request(shim['socket'], payload, timeout + CONNECT_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error(f'send [exception]: {e}')
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    finally:
        logger.info('send [END]')


def is_enabled() -> bool:
    return config.UI_HOOK_MODE != UI_HOOK_OFF
