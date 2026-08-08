"""Unit checks for the bridge's guards and discovery rules. Spends no agent turns.

    PYTHONPATH=src .venv/bin/python tests/unit_guards.py
"""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src')

from cross_agent_mcp import bridge, config, discovery, registry  # noqa: E402


FAILURES = []


def check(label: str, condition: bool, detail: str = '') -> None:
    if condition:
        print(f'[ok] {label}')
    else:
        print(f'[FAIL] {label} {detail}')
        FAILURES.append(label)


# ------------------------------------------------- busy lock is really exclusive

def test_busy_lock_is_exclusive() -> None:
    session_id = 'unit-lock-' + os.urandom(4).hex()
    outcomes = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        try:
            with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_unit'):
                outcomes.append('won')
                time.sleep(0.3)
        except registry.SessionBusyError:
            outcomes.append('refused')

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check('busy lock admits exactly one concurrent claimer',
          outcomes.count('won') == 1 and outcomes.count('refused') == 5, str(outcomes))
    check('busy lock is released afterwards',
          registry.read_busy_lock(config.AGENT_CODEX, session_id) is None)


def test_busy_lock_release_respects_owner() -> None:
    session_id = 'unit-owner-' + os.urandom(4).hex()
    path = registry._lock_path(config.AGENT_CODEX, session_id)

    with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_a'):
        # a foreign holder overwrites the record; our release must leave it alone
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'pid': os.getpid(), 'token': 'someone-else',
                       'conversation_id': 'conv_b', 'started_at': time.time()}, f)

    survivor = registry.read_busy_lock(config.AGENT_CODEX, session_id)
    check('release does not delete another holder\'s lock',
          survivor is not None and survivor.get('token') == 'someone-else', str(survivor))
    os.remove(path)


# ------------------------------------------------------------ pin pruning rules

def test_prune_spares_sticky_pins() -> None:
    missing_dir = '/nonexistent-cross-agent-unit/' + os.urandom(4).hex()
    stale = time.time() - registry.PIN_GRACE_SECONDS - 60

    registry.set_pin(config.AGENT_CODEX, missing_dir, 'sticky-sid', missing_dir, is_sticky=True)
    registry.set_pin(config.AGENT_CLAUDE, missing_dir, 'auto-sid', missing_dir, is_sticky=False)

    def age_them(data):
        for agent in (config.AGENT_CODEX, config.AGENT_CLAUDE):
            data['pins'][agent][registry._cwd_key(missing_dir)]['updated_at'] = stale

    registry.update_registry(age_them)
    registry.update_registry(lambda data: None)  # any write triggers _prune

    check('sticky pin survives a missing directory',
          registry.get_pin(config.AGENT_CODEX, missing_dir) is not None)
    check('stale auto pin on a missing directory is pruned',
          registry.get_pin(config.AGENT_CLAUDE, missing_dir) is None)

    registry.clear_pin(config.AGENT_CODEX, missing_dir)


def test_prune_keeps_fresh_auto_pins() -> None:
    missing_dir = '/nonexistent-cross-agent-unit/' + os.urandom(4).hex()
    registry.set_pin(config.AGENT_CODEX, missing_dir, 'fresh-sid', missing_dir, is_sticky=False)
    registry.update_registry(lambda data: None)

    check('fresh auto pin survives a transient directory miss',
          registry.get_pin(config.AGENT_CODEX, missing_dir) is not None)
    registry.clear_pin(config.AGENT_CODEX, missing_dir)


# ------------------------------------------------------------ cwd scope rules

def test_cwd_relations() -> None:
    rel = discovery._cwd_relation
    check('exact cwd', rel('/a/b', '/a/b') == discovery.CWD_EXACT)
    check('session below search dir', rel('/a/b/c', '/a/b') == discovery.CWD_DESCENDANT)
    check('session above search dir', rel('/a', '/a/b') == discovery.CWD_ANCESTOR)
    check('sibling path is unrelated', rel('/a/bb', '/a/b') is None)
    check("scope 'cwd' rejects ancestors",
          not discovery._is_in_scope(discovery.CWD_ANCESTOR, discovery.SCOPE_CWD))
    check("scope 'tree' accepts ancestors",
          discovery._is_in_scope(discovery.CWD_ANCESTOR, discovery.SCOPE_TREE))
    check("scope 'any' accepts anything",
          discovery._is_in_scope(None, discovery.SCOPE_ANY))


# -------------------------------------- codex scan filters before it truncates

def _write_rollout(store: str, session_id: str, cwd: str, mtime: float) -> None:
    day_dir = store + '/2026/08/08'
    os.makedirs(day_dir, exist_ok=True)
    path = f'{day_dir}/rollout-2026-08-08T00-00-00-{session_id}.jsonl'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({'type': 'session_meta', 'payload': {
            'session_id': session_id, 'cwd': cwd, 'originator': 'codex_vscode',
            'thread_source': 'user'}}) + '\n')
    os.utime(path, (mtime, mtime))


def test_codex_scan_filters_before_limit() -> None:
    original_dir, original_limit = config.CODEX_SESSIONS_DIR, config.CODEX_SCAN_LIMIT
    with tempfile.TemporaryDirectory(prefix='codex-store-') as store:
        target_cwd = store + '/wanted'
        os.makedirs(target_cwd, exist_ok=True)
        now = time.time()

        # 20 newer threads from unrelated directories, then the one we actually want
        for i in range(20):
            _write_rollout(store, f'0000000{i:04d}-0000-0000-0000-00000000000{i % 10}',
                           store + f'/other-{i}', now - i)
        _write_rollout(store, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', target_cwd, now - 100)

        config.CODEX_SESSIONS_DIR = store + '/'
        config.CODEX_SCAN_LIMIT = 5  # smaller than the number of unrelated newer rollouts
        try:
            found = discovery.list_codex_sessions(discovery.SCOPE_CWD, target_cwd, limit=5)
        finally:
            config.CODEX_SESSIONS_DIR, config.CODEX_SCAN_LIMIT = original_dir, original_limit

    check('cwd filter runs before the scan cap',
          len(found) == 1 and found[0]['session_id'] == 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
          str([s['session_id'] for s in found]))


# --------------------------------------- timeout kills the whole process group

def test_timeout_kills_descendants() -> None:
    with tempfile.TemporaryDirectory(prefix='killgroup-') as work_dir:
        marker = work_dir + '/child.pid'
        command = ['/bin/bash', '-c', f'sleep 45 & echo $! > {marker}; wait']

        raised = False
        try:
            bridge._run_cli(command, work_dir, dict(os.environ), timeout=2)
        except bridge.BridgeError as e:
            raised = 'did not answer within' in str(e)

        check('timeout surfaces as a BridgeError', raised)

        time.sleep(0.7)
        grandchild = int(open(marker).read().strip())
        check('grandchild process is killed with the group',
              not registry._is_pid_alive(grandchild), f'pid {grandchild} still alive')


# --------------------------------------- which conversation tab a relay lands in

def test_panel_session_selection() -> None:
    from cross_agent_mcp import uihook

    now = time.time()
    shims = [
        {'agent': 'claude', 'pid': 1, 'socket': '/s1', 'ancestors': [99], 'started_at': now - 300},
        {'agent': 'claude', 'pid': 2, 'socket': '/s2', 'ancestors': [99], 'started_at': now - 200},
        {'agent': 'claude', 'pid': 3, 'socket': '/s3', 'ancestors': [99], 'started_at': now - 100},
    ]
    statuses = {
        '/s1': {'ok': True, 'last_user_activity': 0,
                'sessions': [{'session_id': 'old-tab', 'cwd': '/w'}]},
        '/s2': {'ok': True, 'last_user_activity': now - 5,
                'sessions': [{'session_id': 'typed-tab', 'cwd': '/w'}]},
        '/s3': {'ok': True, 'last_user_activity': 0,
                'sessions': [{'session_id': 'newest-tab', 'cwd': '/w'}]},
    }
    transcripts = {'old-tab': now - 900, 'typed-tab': now - 900, 'newest-tab': now - 10}

    originals = (uihook.list_shims, uihook.process_ancestry,
                 uihook.read_status, uihook._transcript_mtime)
    uihook.list_shims = lambda agent=None: [s for s in shims if not agent or s['agent'] == agent]
    uihook.process_ancestry = lambda pid, depth=12: [99]
    uihook.read_status = lambda shim: statuses[shim['socket']]
    uihook._transcript_mtime = lambda agent, session_id: transcripts.get(session_id, 0.0)

    try:
        check('the tab the human typed into wins',
              uihook.find_live_session('claude')['session_id'] == 'typed-tab',
              str([s['session_id'] for s in uihook.find_live_sessions('claude')]))

        check('an explicit session id overrides the ordering',
              uihook.find_live_session('claude', 'old-tab')['session_id'] == 'old-tab')
        check('a session that is not open in any panel is not matched',
              uihook.find_live_session('claude', 'not-open') is None)

        # nobody has typed since the shims started: fall back to the freshest transcript
        statuses['/s2']['last_user_activity'] = 0
        check('with no observed input the freshest transcript wins',
              uihook.find_live_session('claude')['session_id'] == 'newest-tab',
              str([s['session_id'] for s in uihook.find_live_sessions('claude')]))

        # and with no evidence at all, the most recently opened tab
        for session_id in transcripts:
            transcripts[session_id] = 0.0
        check('with no evidence at all the newest tab wins',
              uihook.find_live_session('claude')['session_id'] == 'newest-tab')
    finally:
        (uihook.list_shims, uihook.process_ancestry,
         uihook.read_status, uihook._transcript_mtime) = originals


if __name__ == '__main__':
    test_busy_lock_is_exclusive()
    test_busy_lock_release_respects_owner()
    test_prune_spares_sticky_pins()
    test_prune_keeps_fresh_auto_pins()
    test_cwd_relations()
    test_codex_scan_filters_before_limit()
    test_timeout_kills_descendants()
    test_panel_session_selection()

    print(f'\n{"ALL UNIT CHECKS PASSED" if not FAILURES else str(len(FAILURES)) + " CHECK(S) FAILED"}')
    sys.exit(1 if FAILURES else 0)
