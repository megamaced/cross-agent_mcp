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

from cross_agent_mcp import bridge, config, discovery, outbox, registry  # noqa: E402


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
              'no codex session is named' in raised and 'no session was created' in raised,
              raised[:160])

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


# ------------------------------------------------------ the outbox never blocks

def _job(box: outbox.Outbox, target_session_id, wants_reply=False, summary='msg'):
    return outbox.Job(
        target_agent=config.AGENT_CODEX, target_session_id=target_session_id,
        payload='hello', run_cwd='/tmp', pin_cwd='/tmp', env={}, timeout=5,
        ui_shim=None, title=None, conversation_id='conv_outbox', hop=1,
        sender_agent=config.AGENT_CLAUDE, sender_session_id='sender-sid',
        wants_reply=wants_reply, summary=summary)


def _drain(box: outbox.Outbox, deadline_seconds: float = 5.0) -> None:
    deadline = time.time() + deadline_seconds
    while time.time() < deadline and box.snapshot()['pending']:
        time.sleep(0.02)


def test_submit_does_not_block_the_caller() -> None:
    box = outbox.Outbox()
    released = threading.Event()

    def slow_deliver(job):
        released.wait(3)
        return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

    box.deliver = slow_deliver

    started = time.time()
    delivery_id = box.submit(_job(box, 'sid-slow'))
    elapsed = time.time() - started

    check('submit returns without waiting for the turn', elapsed < 0.5, f'{elapsed:.2f}s')
    check('submit hands back a delivery id', delivery_id.startswith('dlv_'), delivery_id)
    check('the delivery is reported as in flight',
          any(d['delivery_id'] == delivery_id for d in box.snapshot()['pending']))

    released.set()
    _drain(box)
    check('the delivery finishes on its own',
          box.find(delivery_id).state == outbox.STATE_DELIVERED)


def test_same_session_deliveries_are_serialised() -> None:
    box = outbox.Outbox()
    concurrent = []
    live = []
    guard = threading.Lock()

    def watching_deliver(job):
        with guard:
            live.append(job.delivery_id)
            concurrent.append(len(live))
        time.sleep(0.05)
        with guard:
            live.remove(job.delivery_id)
        return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

    box.deliver = watching_deliver
    for _ in range(4):
        box.submit(_job(box, 'sid-shared'))
    _drain(box)

    check('two turns never run on the same session at once',
          concurrent and max(concurrent) == 1, f'peak={max(concurrent) if concurrent else 0}')
    check('every queued delivery still ran', len(concurrent) == 4, str(len(concurrent)))


def test_different_sessions_deliver_in_parallel() -> None:
    box = outbox.Outbox()
    entered = threading.Barrier(2, timeout=3)
    failures = []

    def blocking_deliver(job):
        try:
            entered.wait()
        except threading.BrokenBarrierError:
            failures.append(job.delivery_id)
        return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

    box.deliver = blocking_deliver
    box.submit(_job(box, 'sid-a'))
    box.submit(_job(box, 'sid-b'))
    _drain(box)

    check('deliveries to different sessions are not serialised', not failures,
          f'barrier broke for {failures}')


def test_a_reply_is_delivered_back_and_stops_there() -> None:
    box = outbox.Outbox()
    delivered = []

    def echo_deliver(job):
        delivered.append((job.target_session_id, job.wants_reply))
        return {'session_id': job.target_session_id, 'reply': 'the answer',
                'is_new_session': False}

    box.deliver = echo_deliver
    box.build_reply = lambda job, reply: _job(box, job.sender_session_id, wants_reply=False,
                                              summary=reply)

    box.submit(_job(box, 'sid-peer', wants_reply=True))
    _drain(box)

    check('the request is delivered to the peer', ('sid-peer', True) in delivered)
    check('the answer is delivered back to the sender', ('sender-sid', False) in delivered)
    check('the answer does not trigger another answer', len(delivered) == 2, str(delivered))


def test_a_busy_session_is_waited_out_not_refused() -> None:
    box = outbox.Outbox()
    session_id = 'sid-busy-' + uuid_hex()
    attempts = []

    box.deliver = lambda job: (attempts.append(time.time()) or
                               {'session_id': job.target_session_id, 'reply': '',
                                'is_new_session': False})

    original_retry = outbox.BUSY_RETRY_SECONDS
    outbox.BUSY_RETRY_SECONDS = 0.05
    holder = threading.Event()

    def hold_the_lock():
        with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_other'):
            holder.set()
            time.sleep(0.3)

    keeper = threading.Thread(target=hold_the_lock, daemon=True)
    keeper.start()
    holder.wait(2)

    try:
        delivery_id = box.submit(_job(box, session_id))
        _drain(box)
        job = box.find(delivery_id)
        check('a session another process holds is waited out, not refused',
              job.state == outbox.STATE_DELIVERED, f'state={job.state} error={job.error}')
        check('the delivery ran only after the lock was released', len(attempts) == 1,
              str(len(attempts)))
    finally:
        outbox.BUSY_RETRY_SECONDS = original_retry
        keeper.join(timeout=2)


def test_another_window_is_reachable_only_when_named() -> None:
    """Auto-selection stays in this window; an explicitly named session is chased anywhere."""
    from cross_agent_mcp import uihook

    now = time.time()
    shims = [
        # this window: the extension host is pid 99, which our own chain shares
        {'agent': 'codex', 'pid': 1, 'socket': '/here', 'ancestors': [99], 'started_at': now},
        # another VS Code window: a different extension host entirely
        {'agent': 'codex', 'pid': 2, 'socket': '/there', 'ancestors': [77], 'started_at': now},
    ]
    statuses = {
        '/here': {'ok': True, 'last_user_activity': now - 5,
                  'sessions': [{'session_id': 'this-window', 'cwd': '/w'}]},
        '/there': {'ok': True, 'last_user_activity': now - 1,
                   'sessions': [{'session_id': 'other-window', 'cwd': '/w2'}]},
    }

    originals = (uihook.list_shims, uihook.process_ancestry,
                 uihook.read_status, uihook._transcript_mtime)
    uihook.list_shims = lambda agent=None: [s for s in shims if not agent or s['agent'] == agent]
    uihook.process_ancestry = lambda pid, depth=12: [99]
    uihook.read_status = lambda shim: statuses[shim['socket']]
    uihook._transcript_mtime = lambda agent, session_id: 0.0

    try:
        local_ids = [s['session_id'] for s in uihook.find_live_sessions('codex')]
        check('this window lists only its own tabs', local_ids == ['this-window'], str(local_ids))

        foreign_ids = [s['session_id'] for s in uihook.find_foreign_sessions('codex')]
        check('other windows are listed separately', foreign_ids == ['other-window'],
              str(foreign_ids))

        # the other window's tab was typed into more recently, so a heuristic that ignored
        # window boundaries would pick it - auto-selection must not
        check('auto-selection never leaves this window',
              uihook.find_live_session('codex')['session_id'] == 'this-window')

        named = uihook.find_live_session('codex', 'other-window')
        check('a named session in another window is found', named is not None)
        check('it is delivered through that window\'s own shim',
              named is not None and named['shim']['socket'] == '/there')
        check('it is marked as belonging to another window',
              named is not None and named.get('is_foreign_window') is True)

        check('a named session in this window still resolves locally',
              uihook.find_live_session('codex', 'this-window')['shim']['socket'] == '/here')
        check('a session open in no window at all is still unmatched',
              uihook.find_live_session('codex', 'nowhere') is None)
    finally:
        (uihook.list_shims, uihook.process_ancestry,
         uihook.read_status, uihook._transcript_mtime) = originals


def test_the_caller_identifies_its_own_session_exactly() -> None:
    """Who we are comes from the shim hosting us, never from a pin saying where to send."""
    from cross_agent_mcp import uihook

    now = time.time()
    shims = [
        # the shim this process is running under: its pid is in our own ancestry
        {'agent': 'claude', 'pid': 500, 'socket': '/mine', 'ancestors': [99], 'started_at': now},
        # a sibling tab in the same window - local, but not hosting us
        {'agent': 'claude', 'pid': 501, 'socket': '/sibling', 'ancestors': [99],
         'started_at': now},
    ]
    statuses = {
        '/mine': {'ok': True, 'last_user_activity': 0,
                  'sessions': [{'session_id': 'me', 'cwd': '/w'}]},
        '/sibling': {'ok': True, 'last_user_activity': now,
                     'sessions': [{'session_id': 'not-me', 'cwd': '/w'}]},
    }

    originals = (uihook.list_shims, uihook.process_ancestry,
                 uihook.read_status, uihook._transcript_mtime)
    uihook.list_shims = lambda agent=None: [s for s in shims if not agent or s['agent'] == agent]
    uihook.process_ancestry = lambda pid, depth=12: [500, 99]
    uihook.read_status = lambda shim: statuses[shim['socket']]
    uihook._transcript_mtime = lambda agent, session_id: 0.0

    try:
        own = uihook.find_own_session('claude')
        # the sibling was typed into more recently, so "the active tab" would pick it
        check('our own session is the one whose shim hosts us',
              own is not None and own['session_id'] == 'me',
              str(own and own['session_id']))
    finally:
        (uihook.list_shims, uihook.process_ancestry,
         uihook.read_status, uihook._transcript_mtime) = originals


def test_a_pin_never_answers_who_the_caller_is() -> None:
    """The regression: a stale pin stood in as the return address and the reply went nowhere."""
    from cross_agent_mcp import uihook

    originals = (uihook.is_enabled, uihook.find_own_session, discovery.find_active_session)
    recorded = {}

    def record_lookup(agent, scope, cwd, exclude_ids=None, use_pin=True):
        recorded['use_pin'] = use_pin
        return {'session_id': 'from-transcript'}

    uihook.is_enabled = lambda: True
    uihook.find_own_session = lambda agent: {'session_id': 'from-panel'}
    discovery.find_active_session = record_lookup

    try:
        check('the panel answers before anything on disk is consulted',
              bridge._own_session_id('claude') == 'from-panel')

        uihook.find_own_session = lambda agent: None
        check('with no panel it falls back to the transcript',
              bridge._own_session_id('claude') == 'from-transcript')
        check('and that fallback is asked WITHOUT pins',
              recorded.get('use_pin') is False, str(recorded))
    finally:
        (uihook.is_enabled, uihook.find_own_session, discovery.find_active_session) = originals


def test_the_envelope_carries_a_return_address() -> None:
    envelope = bridge._build_envelope('claude', 'codex', 'conv_x', 1, 3, 'body', 'sender-sid')
    check('the envelope states where a reply should go',
          'reply-to: claude session sender-sid' in envelope)
    check('and tells the peer how to address a new request',
          'session_id="sender-sid"' in envelope)

    anonymous = bridge._build_envelope('claude', 'codex', 'conv_x', 1, 3, 'body', None)
    check('a sender with no session says so instead of leaving it blank',
          'reply-to: (unknown' in anonymous)
    check('and does not hand out a bogus session id',
          'session_id="' not in anonymous)


def test_a_recovered_answer_says_it_may_not_be_finished() -> None:
    """Recovery reads the peer's last message, which is not always its answer.

    It happened: a relay timed out while the peer was still working, recovery picked up the
    line it had just written about what it was doing next, and that arrived looking exactly
    like a finished report.
    """
    plain = bridge._build_reply_envelope('codex', 'claude', 'conv_x', 1, 3, '작업 완료', 'sid')
    check('a received answer is not hedged', 'RECOVERED' not in plain)
    check('and says plainly where it came from',
          'This is the answer to a message you relayed earlier.' in plain)

    recovered = bridge._build_reply_envelope(
        'codex', 'claude', 'conv_x', 1, 3, '확인 중입니다', 'sid', is_recovered=True)
    check('a recovered answer is marked in the header',
          'recovered from transcript' in recovered)
    check('and warns it may be mid-work rather than an answer',
          'RECOVERED, NOT RECEIVED' in recovered
          and 'still doing rather than its' in recovered, recovered)


def test_the_recovered_flag_reaches_the_envelope() -> None:
    original = discovery.find_session
    discovery.find_session = lambda agent, session_id: None
    try:
        request = outbox.Job(
            target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
            run_cwd='/w', pin_cwd='/w', env={}, timeout=5, ui_shim=None, title=None,
            conversation_id='conv_r', hop=1, sender_agent=config.AGENT_CLAUDE,
            sender_session_id='sender-sid', wants_reply=True, summary='req')
        request.is_reply_recovered = True

        reply = bridge._build_reply_job(request, '확인 중입니다')
        check('a job recovered from a transcript builds a marked envelope',
              reply is not None and 'RECOVERED, NOT RECEIVED' in reply.payload)

        request.is_reply_recovered = False
        plain = bridge._build_reply_job(request, '작업 완료')
        check('and one that arrived normally does not',
              plain is not None and 'RECOVERED' not in plain.payload)
    finally:
        discovery.find_session = original


def test_a_reply_runs_where_the_senders_session_lives() -> None:
    """A Claude transcript is filed under its own project dir; resuming elsewhere fails."""
    with tempfile.TemporaryDirectory(prefix='sender-home-') as sender_home:
        original = discovery.find_session
        discovery.find_session = lambda agent, session_id: (
            {'session_id': session_id, 'cwd': sender_home} if session_id == 'sender-sid' else None)
        try:
            request = outbox.Job(
                target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
                run_cwd='/elsewhere', pin_cwd='/elsewhere', env={}, timeout=5, ui_shim=None,
                title=None, conversation_id='conv_reply', hop=1,
                sender_agent=config.AGENT_CLAUDE, sender_session_id='sender-sid',
                wants_reply=True, summary='req')
            reply = bridge._build_reply_job(request, 'the answer')

            check('the reply is aimed at the sender session',
                  reply is not None and reply.target_session_id == 'sender-sid')
            check('and runs in that session\'s own directory, not the request\'s',
                  reply is not None and reply.run_cwd == sender_home, str(reply and reply.run_cwd))
            check('a reply expects no reply of its own',
                  reply is not None and reply.wants_reply is False)
        finally:
            discovery.find_session = original

    orphan = outbox.Job(
        target_agent=config.AGENT_CODEX, target_session_id='peer-sid', payload='x',
        run_cwd='/elsewhere', pin_cwd='/elsewhere', env={}, timeout=5, ui_shim=None, title=None,
        conversation_id='conv_reply', hop=1, sender_agent=config.AGENT_CLAUDE,
        sender_session_id=None, wants_reply=True, summary='req')
    check('a sender with no session produces no undeliverable reply job',
          bridge._build_reply_job(orphan, 'the answer') is None)


def test_a_message_queued_in_the_wakeup_gap_is_not_lost() -> None:
    """The lost-wakeup window: a submit that notifies before the worker starts waiting.

    Reproduced deterministically by submitting from inside the worker's own empty _next, so
    the notify provably lands while nobody is waiting on the condition. A worker that then
    waits unconditionally sleeps out the full idle linger on an already-queued message.
    """
    box = outbox.Outbox()
    box.deliver = lambda job: {'session_id': job.target_session_id, 'reply': '',
                               'is_new_session': False}

    original_next = box._next
    injected = {'delivery_id': None}

    def next_with_injection(key):
        job = original_next(key)
        if job is None and injected['delivery_id'] is None:
            injected['delivery_id'] = box.submit(_job(box, 'sid-gap'))
        return job

    box._next = next_with_injection
    box.submit(_job(box, 'sid-gap'))

    _drain(box, deadline_seconds=3)

    injected_job = box.find(injected['delivery_id']) if injected['delivery_id'] else None
    check('a message queued in the wakeup gap is picked up, not slept through',
          injected_job is not None and injected_job.state == outbox.STATE_DELIVERED,
          f'state={getattr(injected_job, "state", None)} '
          f'(idle linger is {outbox.IDLE_LINGER_SECONDS}s)')


def test_a_finished_delivery_outlives_the_process_that_carried_it() -> None:
    """The observed loss: the answer arrived, then the editor reloaded and took it away."""
    original_dir = outbox.config.DELIVERY_DIR
    with tempfile.TemporaryDirectory(prefix='cross-agent-deliveries-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        try:
            box = outbox.Outbox()
            box.deliver = lambda job: {'session_id': job.target_session_id,
                                       'reply': 'the answer', 'is_new_session': False}
            delivery_id = box.submit(_job(box, 'sid-persist'))
            _drain(box)

            # a new Outbox is what the next server process starts with
            revived = outbox.Outbox().snapshot()
            kept = next((r for r in revived['earlier']
                         if r['delivery_id'] == delivery_id), None)
            check('a finished delivery is readable by the next process', kept is not None)
            check('and it still carries the answer',
                  kept is not None and kept.get('reply_preview') == 'the answer',
                  str(kept))

            raw = open(store + f'/{delivery_id}.json', encoding='utf-8').read()
            check('the record does not persist the message payload', 'hello' not in raw)
            check('nor the child environment', 'PATH' not in raw)
        finally:
            outbox.config.DELIVERY_DIR = original_dir


def test_an_answer_is_recovered_from_the_peer_transcript() -> None:
    """A delivery that breaks after the peer answered must not throw the answer away."""
    original_dir = outbox.config.DELIVERY_DIR
    with tempfile.TemporaryDirectory(prefix='cross-agent-deliveries-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        try:
            box = outbox.Outbox()
            asked = []

            def broken_deliver(job):
                raise RuntimeError('peer agent did not answer within 600s')

            box.deliver = broken_deliver
            box.recover = lambda job: (asked.append(job.target_session_id)
                                       or '트랜스크립트에서 회수한 답')

            delivery_id = box.submit(_job(box, 'sid-recover', wants_reply=True))
            replies = []
            box.build_reply = lambda job, reply: (replies.append(reply) or None)
            _drain(box)

            job = box.find(delivery_id)
            check('the transport failure is still recorded honestly',
                  job.state == outbox.STATE_FAILED and job.error is not None)
            check('but the answer is recovered from the peer transcript',
                  job.reply == '트랜스크립트에서 회수한 답', job.reply)
            check('and it is marked as recovered, not received',
                  job.is_reply_recovered is True)
            check('recovery asks about the target session', asked == ['sid-recover'], str(asked))
            check('a recovered answer is still relayed to the sender',
                  replies == ['트랜스크립트에서 회수한 답'], str(replies))
        finally:
            outbox.config.DELIVERY_DIR = original_dir


def test_recovery_is_skipped_when_the_transport_already_answered() -> None:
    box = outbox.Outbox()
    box.deliver = lambda job: {'session_id': job.target_session_id,
                               'reply': 'received normally', 'is_new_session': False}
    attempts = []
    box.recover = lambda job: attempts.append(job.delivery_id)

    delivery_id = box.submit(_job(box, 'sid-normal'))
    _drain(box)

    job = box.find(delivery_id)
    check('a delivered answer is not second-guessed', not attempts, str(attempts))
    check('and is not labelled recovered', job.is_reply_recovered is False)


def _write_claude_transcript(path: str, names: list, first_message: str) -> None:
    """A transcript shaped like Claude Code's: the name entry repeats as the session grows."""
    lines = []
    for name in names[:1]:
        lines.append(json.dumps({'type': 'custom-title', 'customTitle': name}))
    lines.append(json.dumps({
        'type': 'user', 'isSidechain': False, 'cwd': '/w', 'entrypoint': 'claude-vscode',
        'message': {'content': first_message}}))
    for name in names[1:]:
        lines.append(json.dumps({'type': 'custom-title', 'customTitle': name}))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def test_a_conversations_own_name_is_what_it_is_called_by() -> None:
    """The incident's real cause: the name was there and the bridge was not reading it.

    A Claude conversation carries the name the human gave it, shown at the top of its panel and
    stored in the transcript. The bridge was inventing a title from the first message instead,
    so "the koppa_studio session" matched nothing it should have - and matched a path quoted
    inside an unrelated session's opening prompt.
    """
    with tempfile.TemporaryDirectory(prefix='claude-titles-') as store:
        named = store + '/11111111-1111-1111-1111-111111111111.jsonl'
        _write_claude_transcript(named, ['koppa_studio'], '.')
        parsed = discovery._parse_claude_session(named)
        check('a named conversation is titled by its name, not its first message',
              parsed is not None and parsed['title'] == 'koppa_studio',
              str(parsed and parsed['title']))
        check('and is marked as named', parsed is not None and parsed['is_named'] is True)

        # renames happen - 3 of the 11 named sessions on this machine had been renamed
        renamed = store + '/22222222-2222-2222-2222-222222222222.jsonl'
        _write_claude_transcript(renamed, ['studio_v4', 'studio_v4_orginial', 'studio_v4 2nd'],
                                 'first prompt')
        parsed = discovery._parse_claude_session(renamed)
        check('a renamed conversation answers to its current name',
              parsed is not None and parsed['title'] == 'studio_v4 2nd',
              str(parsed and parsed['title']))

        unnamed = store + '/33333333-3333-3333-3333-333333333333.jsonl'
        _write_claude_transcript(unnamed, [], 'implement the thing in /src/koppa_studio')
        parsed = discovery._parse_claude_session(unnamed)
        check('an unnamed conversation still falls back to its first message',
              parsed is not None and 'koppa_studio' in parsed['title'])
        check('but is not marked as named', parsed is not None and parsed['is_named'] is False)

        # the fallback title is not a name: it must not answer to a word inside it
        originals = discovery.list_sessions
        discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: [
            discovery._parse_claude_session(unnamed)]
        try:
            check('a word quoted in an unnamed conversation is still not a name',
                  discovery.find_session_by_name('claude', 'koppa_studio') is None)
        finally:
            discovery.list_sessions = originals


def test_a_session_name_matches_exactly_or_not_at_all() -> None:
    """The incident: 'koppa_studio' matched a path quoted inside an old session's first message.

    A Claude session has no name of its own - its title is whatever the human typed first - so
    substring matching turned any quoted path into a name and resumed a months-old session
    headlessly, where nobody was watching it work.
    """
    sessions = [
        {'session_id': 'old-one', 'title': 'K-Oppa Studio 4를 구현하라. 쓰기는 '
                                            '/Users/x/source_code/koppa_studio 아래', 'mtime': 200.0},
        {'session_id': 'audit', 'title': 'Codex 감사 — koppa_studio_v4 결함', 'mtime': 100.0},
        {'session_id': 'named', 'title': 'Studio primer', 'mtime': 50.0},
    ]
    original = discovery.list_sessions
    discovery.list_sessions = lambda agent, scope, cwd, limit=500, **kw: list(sessions)

    try:
        check('a name that only appears inside a title does not match',
              discovery.find_session_by_name('claude', 'koppa_studio') is None)
        check('an exact title still matches',
              (discovery.find_session_by_name('claude', 'Studio primer') or {})
              .get('session_id') == 'named')
        check('matching ignores case and spacing',
              (discovery.find_session_by_name('claude', '  studio   PRIMER ') or {})
              .get('session_id') == 'named')

        near = discovery.suggest_session_names('claude', 'koppa_studio')
        check('the near misses are offered as suggestions instead', len(near) == 2, str(near))
    finally:
        discovery.list_sessions = original


def test_a_named_session_is_never_silently_created() -> None:
    originals = (discovery.find_session, discovery.find_session_by_name,
                 discovery.suggest_session_names)
    discovery.find_session = lambda agent, session_id: None
    discovery.find_session_by_name = lambda agent, name: None
    discovery.suggest_session_names = lambda agent, name, **kw: ['Some other title']

    try:
        bridge._requested_session_id('codex', 'no-such-name', '/w')
        check('an unknown session name fails instead of opening a new conversation', False,
              'no error raised')
    except bridge.BridgeError as e:
        message = str(e)
        check('an unknown session name fails instead of opening a new conversation',
              'no session was created' in message, message)
        check('and the error offers the titles it did see',
              'Some other title' in message, message)
    finally:
        (discovery.find_session, discovery.find_session_by_name,
         discovery.suggest_session_names) = originals


def test_naming_a_session_and_forcing_a_new_one_is_refused() -> None:
    try:
        bridge.send_message('codex', 'hello', session_id='some-name', is_new_session=True)
        check('session_id and new_session cannot be combined', False, 'no error raised')
    except bridge.BridgeError as e:
        check('session_id and new_session cannot be combined',
              'cannot be combined' in str(e) and 'Nothing was sent' in str(e), str(e))


def test_a_delivery_does_not_outlive_the_server_that_started_it() -> None:
    """An orphaned delivery keeps working unwatched, and frees the lock guarding its session."""
    with tempfile.TemporaryDirectory(prefix='orphan-') as work_dir:
        marker = work_dir + '/child.pid'
        command = ['/bin/bash', '-c', f'sleep 45 & echo $! > {marker}; wait']
        started = threading.Event()

        def deliver():
            started.set()
            with contextlib_suppress():
                bridge._run_cli(command, work_dir, dict(os.environ), timeout=40)

        worker = threading.Thread(target=deliver, daemon=True)
        worker.start()
        started.wait(5)

        deadline = time.time() + 5
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.05)
        grandchild = int(open(marker).read().strip())
        check('the delivery is running before we shut down',
              registry._is_pid_alive(grandchild))

        bridge.terminate_live_children()

        deadline = time.time() + 10
        while registry._is_pid_alive(grandchild) and time.time() < deadline:
            time.sleep(0.1)
        check('shutting the server down takes its deliveries with it',
              not registry._is_pid_alive(grandchild), f'pid {grandchild} still alive')
        worker.join(timeout=5)


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(Exception)


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


def run_all() -> None:
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
    test_another_window_is_reachable_only_when_named()
    test_the_caller_identifies_its_own_session_exactly()
    test_a_pin_never_answers_who_the_caller_is()
    test_the_envelope_carries_a_return_address()
    test_a_recovered_answer_says_it_may_not_be_finished()
    test_the_recovered_flag_reaches_the_envelope()
    test_a_reply_runs_where_the_senders_session_lives()
    test_submit_does_not_block_the_caller()
    test_same_session_deliveries_are_serialised()
    test_different_sessions_deliver_in_parallel()
    test_a_reply_is_delivered_back_and_stops_there()
    test_a_busy_session_is_waited_out_not_refused()
    test_a_message_queued_in_the_wakeup_gap_is_not_lost()
    test_a_finished_delivery_outlives_the_process_that_carried_it()
    test_an_answer_is_recovered_from_the_peer_transcript()
    test_recovery_is_skipped_when_the_transport_already_answered()
    test_a_conversations_own_name_is_what_it_is_called_by()
    test_a_session_name_matches_exactly_or_not_at_all()
    test_a_named_session_is_never_silently_created()
    test_naming_a_session_and_forcing_a_new_one_is_refused()
    test_a_delivery_does_not_outlive_the_server_that_started_it()

if __name__ == '__main__':
    # Delivery records are written by any finished job, so a test run left rows like
    # "sid-normal" in the real ~/.cross-agent/deliveries/ and they sat there among genuine
    # ones - which cost real time when a lost reply had to be found among them. Redirect the
    # whole run rather than each test: the next test to submit a job is covered without
    # anyone remembering to.
    with tempfile.TemporaryDirectory(prefix='cross-agent-test-deliveries-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        run_all()

    print(f'\n{"ALL UNIT CHECKS PASSED" if not FAILURES else str(len(FAILURES)) + " CHECK(S) FAILED"}')
    sys.exit(1 if FAILURES else 0)
