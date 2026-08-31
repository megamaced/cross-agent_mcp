"""Discover the currently active Claude Code sessions and Codex threads.

Both products persist every session as a JSONL transcript, so "which session is the
user talking to right now" reduces to "which transcript was written to most recently".

  Claude Code : ~/.claude/projects/<slugified-cwd>/<session-uuid>.jsonl
  Codex       : ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl
"""

import glob
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from . import config, registry


logger = logging.getLogger('cross_agent_mcp.discovery')

# how many leading transcript lines are inspected to recover session metadata
CLAUDE_HEAD_LINES = 120

SCOPE_CWD = 'cwd'
SCOPE_TREE = 'tree'
SCOPE_ANY = 'any'

# how a session's working directory relates to the one being searched; lower sorts first
CWD_EXACT = 0
CWD_DESCENDANT = 1
CWD_ANCESTOR = 2
CWD_UNRELATED = 3

# Which relations each scope accepts. Ancestors are opt-in: every directory sits under the
# home directory, so accepting them by default would let a session opened at ~ answer for
# every project on the machine.
SCOPE_RELATIONS: Dict[str, Any] = {
    SCOPE_CWD: (CWD_EXACT, CWD_DESCENDANT),
    SCOPE_TREE: (CWD_EXACT, CWD_DESCENDANT, CWD_ANCESTOR),
}
SCOPES = (SCOPE_CWD, SCOPE_TREE, SCOPE_ANY)


def slugify_project_dir(cwd: str) -> str:
    """Reproduce Claude Code's project directory name for a working directory."""
    return re.sub(r'[^a-zA-Z0-9]', '-', os.path.realpath(os.path.expanduser(cwd)))


def _safe_mtime(path: str) -> float:
    """A transcript can be rotated away mid-scan; treat that as infinitely old."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _cwd_relation(session_cwd: Optional[str], target_cwd: str) -> Optional[int]:
    """Classify a session's directory against the directory being searched."""
    if not session_cwd:
        return None

    left = os.path.realpath(session_cwd)
    right = os.path.realpath(target_cwd)
    if left == right:
        return CWD_EXACT
    if left.startswith(right + '/'):
        return CWD_DESCENDANT
    if right.startswith(left + '/'):
        return CWD_ANCESTOR
    return None


def _is_in_scope(relation: Optional[int], scope: str) -> bool:
    if scope == SCOPE_ANY:
        return True
    return relation is not None and relation in SCOPE_RELATIONS[scope]


def _sort_key(session: Dict[str, Any]):
    """Closest directory relation first, then most recently written."""
    relation = session.get('cwd_relation')
    return (CWD_UNRELATED if relation is None else relation, -session['mtime'])


def _describe_age(mtime: float) -> Dict[str, Any]:
    age_minutes = (time.time() - mtime) / 60.0
    return {
        'mtime': mtime,
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime)),
        'age_minutes': round(age_minutes, 1),
        'is_active': age_minutes <= config.ACTIVE_WINDOW_MINUTES,
    }


def _shorten(text: str, limit: int = 90) -> str:
    flat = ' '.join(str(text).split())
    return flat[:limit] + ('…' if len(flat) > limit else '')


# ------------------------------------------------------------- Claude Code

def _extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get('text', '') for b in content
                     if isinstance(b, dict) and b.get('type') == 'text']
            return ' '.join(p for p in parts if p)
    return ''


def _parse_claude_session(path: str) -> Optional[Dict[str, Any]]:
    session_id = os.path.basename(path)[:-len('.jsonl')]
    session_cwd: Optional[str] = None
    origin: Optional[str] = None
    title = ''
    has_user_message = False
    is_sidechain_only = True

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for index, line in enumerate(f):
                if index >= CLAUDE_HEAD_LINES:
                    break
                try:
                    entry = json.loads(line)
                except Exception:
                    continue

                if entry.get('type') == 'ai-title' and entry.get('aiTitle'):
                    title = str(entry['aiTitle'])

                if entry.get('type') not in ('user', 'assistant'):
                    continue

                if not entry.get('isSidechain'):
                    is_sidechain_only = False
                if session_cwd is None:
                    session_cwd = entry.get('cwd')
                    origin = entry.get('entrypoint')
                if entry.get('type') == 'user' and not has_user_message:
                    has_user_message = True
                    if not title:
                        title = _shorten(_extract_text(entry.get('message')))
    except Exception as e:
        logger.debug(f'_parse_claude_session [exception]: {path} {e}')
        return None

    if not has_user_message or is_sidechain_only:
        return None

    info: Dict[str, Any] = {
        'agent': config.AGENT_CLAUDE,
        'session_id': session_id,
        'cwd': session_cwd,
        'path': path,
        'origin': origin,
        'title': title,
    }
    info.update(_describe_age(_safe_mtime(path)))
    return info


def _claude_project_dirs(scope: str, cwd: str) -> List[str]:
    """Project directories worth opening for this search.

    The directory name is a pure character substitution of the path, so containment survives
    as a prefix relation on the slug. That narrows the scan cheaply; the per-session `cwd`
    recorded in the transcript then rejects the false positives the lossy slug lets through.
    """
    entries = [p for p in glob.glob(config.CLAUDE_PROJECTS_DIR + '*') if os.path.isdir(p)]
    if scope == SCOPE_ANY:
        return entries

    target = slugify_project_dir(cwd)
    accepts_ancestor = CWD_ANCESTOR in SCOPE_RELATIONS[scope]
    keep: List[str] = []
    for path in entries:
        name = os.path.basename(path)
        if name == target or name.startswith(target + '-'):
            keep.append(path)
        elif accepts_ancestor and target.startswith(name + '-'):
            keep.append(path)
    return keep


def list_claude_sessions(scope: str, cwd: str, limit: int = 20) -> List[Dict[str, Any]]:
    paths: List[str] = []
    for project_dir in _claude_project_dirs(scope, cwd):
        paths.extend(glob.glob(project_dir + '/*.jsonl'))

    paths = sorted(paths, key=_safe_mtime, reverse=True)

    sessions: List[Dict[str, Any]] = []
    for path in paths:
        if len(sessions) >= limit:
            break

        info = _parse_claude_session(path)
        if not info:
            continue

        relation = _cwd_relation(info.get('cwd'), cwd)
        if not _is_in_scope(relation, scope):
            continue
        info['cwd_relation'] = relation
        sessions.append(info)

    return sorted(sessions, key=_sort_key)


# ------------------------------------------------------------------- Codex

def _load_codex_thread_names() -> Dict[str, str]:
    names: Dict[str, str] = {}
    try:
        with open(config.CODEX_SESSION_INDEX_PATH, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get('id'):
                    names[entry['id']] = entry.get('thread_name') or ''
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f'_load_codex_thread_names [exception]: {e}')
    return names


def _parse_codex_session(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            entry = json.loads(f.readline())
    except Exception:
        return None

    if entry.get('type') != 'session_meta':
        return None

    payload = entry.get('payload', {})
    session_id = payload.get('session_id') or payload.get('id')
    if not session_id:
        return None

    # sub-thread rollouts belong to a Codex subagent, never to the user's chat
    if payload.get('thread_source') != 'user':
        return None

    info: Dict[str, Any] = {
        'agent': config.AGENT_CODEX,
        'session_id': session_id,
        'cwd': payload.get('cwd'),
        'path': path,
        'origin': payload.get('originator'),
        'title': '',
    }
    info.update(_describe_age(_safe_mtime(path)))
    return info


def list_codex_sessions(scope: str, cwd: str, limit: int = 20,
                        since_mtime: Optional[float] = None) -> List[Dict[str, Any]]:
    """Scan the Codex rollout store, newest first.

    Codex keeps every project's rollouts in one date-partitioned store, so a directory filter
    can only be applied by opening each file. Two bounds keep that honest:

      - `since_mtime` stops the walk at a point in time. Session discovery uses the active
        window, because a transcript older than that cannot be the live session anyway.
      - the count cap only applies when nothing is being filtered out. Truncating a filtered
        scan by file count would silently discard the one session being looked for.
    """
    paths = glob.glob(config.CODEX_SESSIONS_DIR + '**/rollout-*.jsonl', recursive=True)
    paths = sorted(paths, key=_safe_mtime, reverse=True)

    thread_names = _load_codex_thread_names()
    by_session: Dict[str, Dict[str, Any]] = {}
    is_filtering = scope != SCOPE_ANY
    examined = 0

    for path in paths:
        if len(by_session) >= limit:
            break
        if since_mtime is not None and _safe_mtime(path) < since_mtime:
            break
        if not is_filtering and examined >= config.CODEX_SCAN_LIMIT:
            logger.info(f'list_codex_sessions [truncated]: stopped after {examined} of '
                        f'{len(paths)} rollout files')
            break
        examined += 1

        info = _parse_codex_session(path)
        if not info:
            continue

        relation = _cwd_relation(info.get('cwd'), cwd)
        if not _is_in_scope(relation, scope):
            continue

        # a resumed thread produces several rollout files: keep the freshest one
        previous = by_session.get(info['session_id'])
        if previous and previous['mtime'] >= info['mtime']:
            continue
        info['title'] = thread_names.get(info['session_id'], '')
        info['cwd_relation'] = relation
        by_session[info['session_id']] = info

    return sorted(by_session.values(), key=_sort_key)[:limit]


# ------------------------------------------------------------------ shared

def list_sessions(agent: str, scope: str, cwd: str, limit: int = 20,
                  since_mtime: Optional[float] = None) -> List[Dict[str, Any]]:
    if agent == config.AGENT_CLAUDE:
        return list_claude_sessions(scope, cwd, limit)
    if agent == config.AGENT_CODEX:
        return list_codex_sessions(scope, cwd, limit, since_mtime)
    raise ValueError(f'unknown agent: {agent}')


def find_session(agent: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Look up one session by id. Both stores encode the id in the file name."""
    if agent == config.AGENT_CLAUDE:
        for path in glob.glob(config.CLAUDE_PROJECTS_DIR + f'*/{session_id}.jsonl'):
            info = _parse_claude_session(path)
            if info:
                return info
        return None

    if agent == config.AGENT_CODEX:
        paths = glob.glob(config.CODEX_SESSIONS_DIR + f'**/rollout-*-{session_id}.jsonl',
                          recursive=True)
        for path in sorted(paths, key=_safe_mtime, reverse=True):
            info = _parse_codex_session(path)
            if info:
                info['title'] = _load_codex_thread_names().get(session_id, '')
                return info
        return None

    raise ValueError(f'unknown agent: {agent}')


def find_session_by_name(agent: str, name: str, limit: int = 500) -> Optional[Dict[str, Any]]:
    """Look one session up by the title the user sees, not by its uuid.

    The match is exact (case- and whitespace-insensitive) and nothing else. Substring matching
    used to be allowed and it picked the wrong conversation: a Claude session has no name of
    its own - its title is the first thing the human typed - so a path quoted in that first
    message made "koppa_studio" match a months-old session and resume it headlessly.

    A name the caller half-remembers must fail loudly. Guessing is how a message ends up in a
    conversation nobody is watching.
    """
    wanted = ' '.join(name.split()).casefold()
    if not wanted:
        return None

    matches = [
        candidate for candidate in list_sessions(agent, SCOPE_ANY, os.getcwd(), limit=limit)
        if ' '.join(str(candidate.get('title') or '').split()).casefold() == wanted
    ]
    if not matches:
        return None

    best = max(matches, key=lambda s: s['mtime'])
    best['source'] = 'name'
    best['matched_name'] = name
    if len(matches) > 1:
        logger.info(f'find_session_by_name [ambiguous]: {len(matches)} sessions are titled '
                    f'{name!r}; took the freshest ({best["session_id"]})')
    return best


def suggest_session_names(agent: str, name: str, limit: int = 500,
                          suggestions: int = 5) -> List[str]:
    """Titles that merely contain what was asked for - shown when nothing matched exactly.

    These are suggestions for a human to read, never something to deliver into.
    """
    wanted = ' '.join(name.split()).casefold()
    if not wanted:
        return []

    seen: List[str] = []
    for candidate in list_sessions(agent, SCOPE_ANY, os.getcwd(), limit=limit):
        title = str(candidate.get('title') or '').strip()
        if title and wanted in ' '.join(title.split()).casefold():
            seen.append(_shorten(title))
        if len(seen) >= suggestions:
            break
    return seen


# how much of a transcript's tail is read when recovering an answer from it
TAIL_BYTES = 2_000_000


def _tail_lines(path: str) -> List[str]:
    """The end of a transcript. A rollout runs to thousands of lines; the answer is at the end."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # the seek lands mid-line; drop the fragment
            data = f.read()
    except OSError:
        return []
    return data.decode('utf-8', errors='replace').splitlines()


def last_agent_message(agent: str, session_id: str) -> Optional[str]:
    """The final assistant message a session wrote, read straight from its transcript.

    The bridge normally carries an answer back from the process it started. When that process
    dies first - the editor window reloaded, the machine slept - the answer is not gone, it is
    just unread: the peer already wrote it to disk. This reads it from there, so an answer is
    lost only when the peer never produced one.
    """
    session = find_session(agent, session_id)
    if not session:
        return None

    for line in reversed(_tail_lines(session['path'])):
        try:
            entry = json.loads(line)
        except Exception:
            continue

        if agent == config.AGENT_CLAUDE:
            if entry.get('type') != 'assistant':
                continue
            text = _extract_text(entry.get('message'))
        else:
            payload = entry.get('payload')
            if not isinstance(payload, dict) or payload.get('role') != 'assistant':
                continue
            if payload.get('type') not in ('message', 'agent_message'):
                continue
            text = ' '.join(
                block.get('text', '') for block in (payload.get('content') or [])
                if isinstance(block, dict))

        if text.strip():
            return text.strip()
    return None


def find_active_session(agent: str, scope: str, cwd: str,
                        exclude_ids: Optional[List[str]] = None,
                        use_pin: bool = True) -> Optional[Dict[str, Any]]:
    """Resolve the session the user is currently talking to.

    Order of preference:
      1. a pin recorded in the registry (sticky pins never expire)
      2. the most recently written transcript inside the active window

    `use_pin=False` skips step 1. A pin records where to *send*, so it must not answer a
    question about who the caller itself is - see uihook.find_own_session.
    """
    blocked = set(exclude_ids or [])

    pin = registry.get_pin(agent, cwd) if use_pin else None
    if pin and pin.get('session_id') not in blocked:
        pinned = find_session(agent, pin['session_id'])
        if pinned and (pin.get('is_sticky') or pinned['is_active']):
            pinned['source'] = 'pin'
            return pinned

    # candidates are ordered by directory closeness first, so an inactive exact-cwd session
    # must be skipped rather than ending the search on a fresher, more distant one
    active_since = time.time() - config.ACTIVE_WINDOW_MINUTES * 60
    for candidate in list_sessions(agent, scope, cwd, limit=50, since_mtime=active_since):
        if candidate['session_id'] in blocked or not candidate['is_active']:
            continue
        candidate['source'] = 'discovery'
        return candidate

    return None
