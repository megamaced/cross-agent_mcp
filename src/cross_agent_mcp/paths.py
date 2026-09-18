"""Check a caller-supplied session id before anything builds a path or a glob out of it.

A session id arrives over the MCP boundary, from an agent that got it from a human, and it
ends up in three filesystem-sensitive places: the glob that locates a Claude transcript, the
glob that locates a Codex rollout, and the name of the busy-lock file that claims a session.
Normal ids are UUIDs, but "normally a UUID" is not a property of the input - it is a property
of the happy path, and the happy path is not what a boundary check is for.

So there is one helper, used by all three. It answers a single question - is this string
allowed to become part of a path - and the containment check answers the other one: did the
path we built out of it stay inside the store it was supposed to be in.
"""

import os
import re
from typing import Any

from . import config


# Long enough for any id either product has ever issued, short enough that nothing can push a
# path near a system limit.
SESSION_ID_MAX_LENGTH = 128

# Hex, dashes, dots and underscores - everything a Claude session uuid or a Codex thread id is
# made of, and nothing that means anything to a path or to glob: no separators, no `..` (which
# needs a separator to traverse anywhere), no `*?[]{}`, no `~`, no whitespace, no control
# characters. Leading dots are out too, so an id can never name a hidden file.
SESSION_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


class InvalidSessionId(ValueError):
    """A session id that must not be allowed to reach the filesystem."""


def canonical_session_id(session_id: Any) -> str:
    """The checked form of a caller-supplied session id, or InvalidSessionId.

    Only surrounding whitespace is forgiven: an id copied out of a panel or a log tends to
    bring some with it, and that is a transcription artefact rather than a different id.
    """
    if not isinstance(session_id, str):
        raise InvalidSessionId(f'session id must be a string, got {type(session_id).__name__}')

    candidate = session_id.strip()
    if not candidate:
        raise InvalidSessionId('session id is empty')
    if len(candidate) > SESSION_ID_MAX_LENGTH:
        raise InvalidSessionId(
            f'session id is {len(candidate)} characters, over the {SESSION_ID_MAX_LENGTH} limit')
    if not SESSION_ID_PATTERN.match(candidate):
        raise InvalidSessionId(
            f'session id {session_id!r} contains characters that are not allowed in one: only '
            'letters, digits, dot, dash and underscore, starting with a letter or digit')
    return candidate


def is_session_id(session_id: Any) -> bool:
    """Whether this string could be an id at all.

    The bridge accepts either an id or a conversation name in the same argument, so "not a
    valid id" has to be an ordinary answer rather than an error: a name with a space in it is
    not malformed, it is simply not an id, and the name lookup is where it belongs.
    """
    try:
        canonical_session_id(session_id)
    except InvalidSessionId:
        return False
    return True


def is_within(path: str, root: str) -> bool:
    """Whether `path` really sits under `root`, symlinks and all.

    The ids that build these paths are checked first, so this is the second lock on the same
    door: it holds even if a pattern is widened later, or a store turns out to contain a
    symlink pointing somewhere else entirely.
    """
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
    except OSError:
        return False
    return real_path == real_root or real_path.startswith(real_root.rstrip(os.sep) + os.sep)


def contained(paths: Any, root: str) -> list:
    """The paths of `paths` that are inside `root`; the rest are dropped."""
    return [p for p in paths if is_within(p, root)]


def lock_path(agent: str, session_id: str) -> str:
    """The busy-lock file for one session, refusing to name one it cannot vouch for.

    A lock is only ever claimed for a session that has already been resolved, so an id that
    does not check out here is a bug or an attack, never a user typing a conversation name -
    and either way nothing should be created on disk for it.
    """
    if agent not in (config.AGENT_CLAUDE, config.AGENT_CODEX):
        raise InvalidSessionId(f'unknown agent: {agent!r}')

    path = config.LOCK_DIR + f'{agent}__{canonical_session_id(session_id)}.lock'
    if not is_within(path, config.LOCK_DIR):
        raise InvalidSessionId(f'lock path for {session_id!r} escapes {config.LOCK_DIR}')
    return path
