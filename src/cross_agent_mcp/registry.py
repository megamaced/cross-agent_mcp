"""Persistent bridge state: session pins, conversation hop counters, busy locks.

All mutations go through a single flock-protected read-modify-write so that two agent
processes touching the registry at the same time cannot lose an update.
"""

import contextlib
import errno
import fcntl
import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import config, paths


logger = logging.getLogger('cross_agent_mcp.registry')

EMPTY_REGISTRY: Dict[str, Any] = {'version': 1, 'pins': {}, 'conversations': {}}

# conversation records older than this are pruned on write
CONVERSATION_TTL_SECONDS = 24 * 3600

# an auto-created pin is only dropped once its directory has been gone for this long, so a
# momentary stat failure on a network share or a briefly relinked symlink cannot erase it
PIN_GRACE_SECONDS = 3600


class SessionBusyError(Exception):
    """Raised when a session is already holding a live busy lock."""

    def __init__(self, holder: Dict[str, Any]) -> None:
        super().__init__(f'session busy: {holder}')
        self.holder = holder


def _cwd_key(cwd: str) -> str:
    return os.path.realpath(os.path.expanduser(cwd))


def _read_unlocked(path: str) -> Dict[str, Any]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(EMPTY_REGISTRY))
    except Exception as e:
        logger.error(f'_read_unlocked [exception]: {e}')
        return json.loads(json.dumps(EMPTY_REGISTRY))

    for key, default in EMPTY_REGISTRY.items():
        data.setdefault(key, default)
    return data


def _prune(data: Dict[str, Any]) -> None:
    now = time.time()

    conversations = data.get('conversations', {})
    for conv_id in [k for k, v in conversations.items()
                    if now - float(v.get('updated_at', 0)) > CONVERSATION_TTL_SECONDS]:
        conversations.pop(conv_id, None)

    # Drop auto-created pins whose directory is gone. Sticky pins are never touched: they are
    # documented as permanent, and a single failed stat must not silently revoke one.
    for pins in data.get('pins', {}).values():
        for cwd in [k for k, v in pins.items()
                    if not v.get('is_sticky')
                    and now - float(v.get('updated_at', 0)) > PIN_GRACE_SECONDS
                    and not os.path.isdir(k)]:
            pins.pop(cwd, None)


def load_registry() -> Dict[str, Any]:
    return _read_unlocked(config.REGISTRY_PATH)


def update_registry(mutator: Callable[[Dict[str, Any]], Any]) -> Any:
    """Run `mutator` against the registry under an exclusive lock and persist the result."""
    config.ensure_dirs()
    lock_path = config.REGISTRY_PATH + '.lock'

    with open(lock_path, 'a+', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_unlocked(config.REGISTRY_PATH)
            result = mutator(data)
            _prune(data)

            tmp_path = config.REGISTRY_PATH + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, config.REGISTRY_PATH)
            return result
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- session pins

def get_pin(agent: str, cwd: str) -> Optional[Dict[str, Any]]:
    data = load_registry()
    return data.get('pins', {}).get(agent, {}).get(_cwd_key(cwd))


def set_pin(agent: str, cwd: str, session_id: str, session_cwd: str,
            is_sticky: bool = False, is_bridge_created: bool = False) -> None:
    def mutate(data: Dict[str, Any]) -> None:
        pins = data.setdefault('pins', {}).setdefault(agent, {})
        pins[_cwd_key(cwd)] = {
            'session_id': session_id,
            'cwd': session_cwd,
            'is_sticky': is_sticky,
            'is_bridge_created': is_bridge_created,
            'updated_at': time.time(),
        }

    update_registry(mutate)


def touch_pin(agent: str, cwd: str) -> None:
    def mutate(data: Dict[str, Any]) -> None:
        pin = data.get('pins', {}).get(agent, {}).get(_cwd_key(cwd))
        if pin:
            pin['updated_at'] = time.time()

    update_registry(mutate)


def clear_pin(agent: str, cwd: str) -> bool:
    def mutate(data: Dict[str, Any]) -> bool:
        pins = data.get('pins', {}).get(agent, {})
        return pins.pop(_cwd_key(cwd), None) is not None

    return update_registry(mutate)


def list_bridge_created_ids(agent: str) -> List[str]:
    data = load_registry()
    pins = data.get('pins', {}).get(agent, {})
    return [p['session_id'] for p in pins.values() if p.get('is_bridge_created')]


# ------------------------------------------------------------- hop accounting

def bump_conversation(conversation_id: str, sender: str, target: str) -> Dict[str, Any]:
    """Increment the hop counter and return the conversation record."""
    def mutate(data: Dict[str, Any]) -> Dict[str, Any]:
        conversations = data.setdefault('conversations', {})
        record = conversations.setdefault(conversation_id, {'hops': 0, 'trail': []})
        record['hops'] = int(record.get('hops', 0)) + 1
        record['updated_at'] = time.time()
        record.setdefault('trail', []).append(f'{sender}->{target}')
        record['trail'] = record['trail'][-20:]
        return json.loads(json.dumps(record))

    return update_registry(mutate)


def get_conversation(conversation_id: str) -> Dict[str, Any]:
    data = load_registry()
    return data.get('conversations', {}).get(conversation_id, {'hops': 0, 'trail': []})


# --------------------------------------------------------------- busy locking

def _lock_path(agent: str, session_id: str) -> str:
    """Where one session's busy lock lives. Raises rather than name a file for a bad id."""
    return paths.lock_path(agent, session_id)


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def read_busy_lock(agent: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Return the live lock record for a session, clearing it when it is stale."""
    path = _lock_path(agent, session_id)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            record = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(path)
        return None

    # The holder says how long it may legitimately hold on; a lock without that field was
    # written by an older build that never held one past two turn budgets.
    ttl = float(record.get('ttl_seconds') or config.SEND_TIMEOUT_SECONDS * 2)
    is_stale = (not _is_pid_alive(int(record.get('pid', -1)))
                or time.time() - float(record.get('started_at', 0)) > ttl)
    if is_stale:
        with contextlib.suppress(OSError):
            os.remove(path)
        return None

    return record


def _claim_lock_file(path: str, payload: str) -> bool:
    """Create the lock file, or report that somebody else already owns it."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False

    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(payload)
    return True


def _release_lock_file(path: str, token: str) -> None:
    """Remove the lock only while we still own it, never somebody else's fresh claim."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            record = json.load(f)
    except FileNotFoundError:
        return
    except Exception:
        record = None

    if record is None or record.get('token') == token:
        with contextlib.suppress(OSError):
            os.remove(path)


@contextlib.contextmanager
def busy_lock(agent: str, session_id: str, conversation_id: str,
              ttl_seconds: Optional[float] = None) -> Iterator[None]:
    """Claim a session for the duration of the block.

    The claim is an atomic O_EXCL create, so checking whether a session is busy and marking
    it busy are one step: two relays racing for the same session cannot both win.

    `ttl_seconds` is how long the claim stays believable to other processes. A panel delivery
    now listens for as long as the peer's turn takes, which is longer than two turn budgets;
    without saying so, another server would judge the lock abandoned and start a second turn
    on the same session.
    """
    config.ensure_dirs()
    path = _lock_path(agent, session_id)
    token = uuid.uuid4().hex
    payload = json.dumps({
        'pid': os.getpid(),
        'token': token,
        'agent': agent,
        'session_id': session_id,
        'conversation_id': conversation_id,
        'started_at': time.time(),
        'ttl_seconds': ttl_seconds or config.SEND_TIMEOUT_SECONDS * 2,
    })

    is_claimed = False
    # one retry: read_busy_lock clears an abandoned record, and the retry re-races for it
    for _ in range(2):
        if _claim_lock_file(path, payload):
            is_claimed = True
            break
        holder = read_busy_lock(agent, session_id)
        if holder:
            raise SessionBusyError(holder)

    if not is_claimed:
        raise SessionBusyError({'agent': agent, 'session_id': session_id})

    try:
        yield
    finally:
        _release_lock_file(path, token)


def list_busy_locks() -> List[Dict[str, Any]]:
    config.ensure_dirs()
    locks: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(config.LOCK_DIR)):
        if not name.endswith('.lock'):
            continue
        agent, _, rest = name[:-5].partition('__')
        try:
            record = read_busy_lock(agent, rest)
        except paths.InvalidSessionId:
            # a lock file this build would never have written; listing is a read-only report
            # and has no business failing over one
            logger.info(f'list_busy_locks [skipped]: {name}')
            continue
        if record:
            locks.append(record)
    return locks
