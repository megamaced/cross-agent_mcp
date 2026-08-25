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

        # the group gets SIGTERM first, so give it a moment - polling, because a loaded
        # machine can take noticeably longer than a fixed sleep would allow
        grandchild = int(open(marker).read().strip())
        deadline = time.time() + 10
        while registry._is_pid_alive(grandchild) and time.time() < deadline:
            time.sleep(0.1)

        check('grandchild process is killed with the group',
              not registry._is_pid_alive(grandchild), f'pid {grandchild} still alive')


# ------------------------------- sub-agent threads must never receive a relay

def test_subagent_threads_are_rejected() -> None:
    from cross_agent_mcp.appserver_shim import CodexAppServerShim

    accepts = CodexAppServerShim._accepts_direct_input
    check('a plain panel thread accepts direct input', accepts({'id': 'a'}))
    check('a thread with a parent is a sub-agent',
          not accepts({'id': 'b', 'parentThreadId': 'a'}))
    check('a thread with an agent nickname is a sub-agent',
          not accepts({'id': 'c', 'agentNickname': 'Turing'}))
    check('a thread with an agent role is a sub-agent',
          not accepts({'id': 'd', 'agentRole': 'reviewer'}))
    check('an explicit canAcceptDirectInput=false is honoured',
          not accepts({'id': 'e', 'canAcceptDirectInput': False}))

    # a notification must never be able to introduce an unvetted thread
    shim = CodexAppServerShim.__new__(CodexAppServerShim)
    shim.threads = {}
    shim.injections = {}
    shim.state_lock = threading.Lock()
    shim.last_user_activity = 0.0

    shim._observe_from_server({'method': 'item/started',
                               'params': {'threadId': 'sub-agent-thread'}})
    check('a bare threadId in a notification does not create a target',
          'sub-agent-thread' not in shim.threads, str(shim.threads))

    shim._observe_from_server({'method': 'thread/started',
                               'params': {'thread': {'id': 'sub', 'parentThreadId': 'main'}}})
    check('thread/started for a sub-agent is ignored', 'sub' not in shim.threads, str(shim.threads))

    shim._observe_from_server({'method': 'thread/started',
                               'params': {'thread': {'id': 'main', 'cwd': '/w'}}})
    check('thread/started for a user thread is recorded', 'main' in shim.threads, str(shim.threads))

    # and only the extension's own thread-driving requests may register one
    shim._observe_from_client({'method': 'thread/read', 'params': {'threadId': 'peeked'}})
    check('an unrelated client request does not register a thread',
          'peeked' not in shim.threads, str(shim.threads))

    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'driven'}})
    check('a turn the extension started registers its thread',
          'driven' in shim.threads, str(shim.threads))


# ------------------- a new conversation is the last resort, never a silent one

def test_new_session_is_the_last_resort() -> None:
    from cross_agent_mcp import uihook

    calls = []
    originals = (uihook.is_enabled, uihook.find_live_session, uihook.find_panel_host,
                 discovery.find_active_session, discovery.find_session,
                 discovery.find_session_by_name, registry.get_pin)

    uihook.is_enabled = lambda: True
    uihook.find_panel_host = lambda agent: {'shim': {'pid': 1, 'socket': '/s'}}
    registry.get_pin = lambda agent, cwd: None

    def restore():
        (uihook.is_enabled, uihook.find_live_session, uihook.find_panel_host,
         discovery.find_active_session, discovery.find_session,
         discovery.find_session_by_name, registry.get_pin) = originals

    try:
        # an existing session on disk must win over opening a fresh conversation
        uihook.find_live_session = lambda agent, session_id=None: None
        discovery.find_active_session = lambda agent, scope, cwd, exclude=None: {
            'session_id': 'on-disk', 'source': 'discovery', 'cwd': '/w'}
        target = bridge._resolve_target('codex', None, 'cwd', '/w', False, [])
        check('an existing session outranks opening a new conversation',
              target.get('session_id') == 'on-disk', str(target))

        # only when nothing can be resumed anywhere
        discovery.find_active_session = lambda agent, scope, cwd, exclude=None: None
        target = bridge._resolve_target('codex', None, 'cwd', '/w', False, [])
        check('a new conversation is opened only when nothing exists',
              target.get('source') == 'ide-panel-new', str(target))

        # a named session that matches nothing must fail instead of starting over
        discovery.find_session = lambda agent, sid: None
        discovery.find_session_by_name = lambda agent, name, limit=500: None
        raised = ''
        try:
            bridge._resolve_target('codex', 'studio_v4_orginial', 'cwd', '/w', False, [])
        except bridge.BridgeError as e:
            raised = str(e)
        check('an unmatched session name fails loudly',
              'no codex session matches' in raised and 'was created' in raised, raised[:160])

        # a name that does match is resolved to its id
        discovery.find_session_by_name = lambda agent, name, limit=500: {
            'session_id': 'named-id', 'title': name, 'mtime': 0}
        calls.clear()

        def find_session(agent, sid):
            calls.append(sid)
            return {'session_id': sid} if sid == 'named-id' else None

        discovery.find_session = find_session
        target = bridge._resolve_target('codex', 'studio_v4_orginial', 'cwd', '/w', False, [])
        check('a conversation name resolves to its session',
              target.get('session_id') == 'named-id' and target.get('source') == 'name',
              str(target))
    finally:
        restore()


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
    test_subagent_threads_are_rejected()
    test_new_session_is_the_last_resort()
    test_panel_session_selection()

    print(f'\n{"ALL UNIT CHECKS PASSED" if not FAILURES else str(len(FAILURES)) + " CHECK(S) FAILED"}')
    sys.exit(1 if FAILURES else 0)
