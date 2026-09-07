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
    check('submit hands back a delivery id', delivery_id.startswith('req_'), delivery_id)
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


def test_a_short_timeout_is_raised_to_the_configured_budget() -> None:
    """Callers kept passing 30s and 120s from when this blocked. Peer turns run 216s..660s,
    so the short value did nothing but cut them off."""
    captured = {}
    originals = (bridge.caller.detect_caller, bridge._own_session_id,
                 bridge._resolve_target, outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome)

    bridge.caller.detect_caller = lambda: {'agent': config.AGENT_CLAUDE, 'chain': []}
    bridge._own_session_id = lambda agent: 'sender-sid'
    bridge._resolve_target = lambda *a, **kw: {
        'agent': config.AGENT_CODEX, 'session_id': 'peer-sid', 'cwd': None,
        'source': 'name', 'ui_shim': None}
    outbox.OUTBOX.submit = lambda job: (captured.update(timeout=job.timeout)
                                        or 'dlv_test')
    outbox.OUTBOX.await_outcome = lambda job: None

    try:
        result = bridge.send_message(config.AGENT_CODEX, 'hello', timeout=30)
        check('a timeout below the budget is raised to it',
              captured.get('timeout') == config.SEND_TIMEOUT_SECONDS, str(captured))
        check('and the caller is told, not silently overridden',
              'was raised to' in (result.get('warning') or ''), str(result.get('warning')))

        captured.clear()
        bridge.send_message(config.AGENT_CODEX, 'hello', timeout=1800)
        check('a longer timeout is honoured', captured.get('timeout') == 1800, str(captured))
    finally:
        (bridge.caller.detect_caller, bridge._own_session_id,
         bridge._resolve_target, outbox.OUTBOX.submit, outbox.OUTBOX.await_outcome) = originals


def test_an_echoed_token_identifies_which_request_was_answered() -> None:
    """The timestamp rule cannot tell two requests apart, and the panel is the human's session
    too — anything they type there also postdates our request. An echoed id settles it."""
    import datetime

    spoke_at = datetime.datetime(2026, 9, 1, 3, 0, 0, tzinfo=datetime.timezone.utc)
    mine = 'req_1788197970127_ea5c29'
    other = 'req_1788197970127_bbbbbb'

    def transcript(text: str) -> str:
        store = tempfile.mkdtemp(prefix='token-')
        path = store + '/session.jsonl'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'assistant',
                'timestamp': spoke_at.isoformat().replace('+00:00', 'Z'),
                'message': {'content': [{'type': 'text', 'text': text}]},
            }) + '\n')
        return path

    original = discovery.find_session
    try:
        # 토큰이 맞으면 시각이 요청보다 앞서도 답으로 인정한다 — 증거가 추정을 이긴다.
        discovery.find_session = lambda a, s: {'path': transcript(f'끝났습니다.\n{mine}')}
        check('a matching token is accepted even against the clock',
              discovery.last_agent_message(
                  'claude', 'sid', after=spoke_at.timestamp() + 999, token=mine) is not None)

        discovery.find_session = lambda a, s: {'path': transcript(f'다른 답입니다.\n{other}')}
        check('a different token is refused even though it is newer',
              discovery.last_agent_message(
                  'claude', 'sid', after=spoke_at.timestamp() - 999, token=mine) is None)

        # 상대가 토큰을 안 적으면 기존 시각 규칙으로 되돌아간다 — 협조는 보너스지 조건이 아니다.
        discovery.find_session = lambda a, s: {'path': transcript('토큰 없이 답합니다.')}
        check('a peer that ignored the token still falls back to the clock',
              discovery.last_agent_message(
                  'claude', 'sid', after=spoke_at.timestamp() - 1, token=mine) is not None)
        check('and the clock still refuses what predates the request',
              discovery.last_agent_message(
                  'claude', 'sid', after=spoke_at.timestamp() + 1, token=mine) is None)
    finally:
        discovery.find_session = original


def test_a_request_id_is_short_and_carries_its_time() -> None:
    token = outbox.new_request_id()
    check('the token is short enough to copy back', len(token) <= 26, token)
    check('and its millisecond prefix is readable',
          abs(int(token.split('_')[1]) / 1000 - time.time()) < 5, token)
    check('two tokens in the same millisecond still differ',
          outbox.new_request_id() != outbox.new_request_id())


def test_recovery_refuses_a_message_older_than_the_question() -> None:
    """It happened twice: one paragraph written before either request existed came back as the
    answer to both. A message that predates its own question is not an answer."""
    import datetime

    spoke_at = datetime.datetime(2026, 9, 1, 2, 5, 21, tzinfo=datetime.timezone.utc)
    with tempfile.TemporaryDirectory(prefix='transcript-') as store:
        path = store + '/session.jsonl'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'assistant',
                'timestamp': spoke_at.isoformat().replace('+00:00', 'Z'),
                'message': {'content': [{'type': 'text', 'text': '이전에 하던 말'}]},
            }) + '\n')

        original = discovery.find_session
        discovery.find_session = lambda agent, session_id: {'path': path}
        try:
            check('without a cutoff the last message is returned',
                  discovery.last_agent_message('claude', 'sid') == '이전에 하던 말')
            check('a message written before the request is not an answer',
                  discovery.last_agent_message(
                      'claude', 'sid', after=spoke_at.timestamp() + 1) is None)
            check('a message written after it still is',
                  discovery.last_agent_message(
                      'claude', 'sid', after=spoke_at.timestamp() - 1) == '이전에 하던 말')
        finally:
            discovery.find_session = original


def test_recovery_refuses_a_message_with_no_timestamp_when_asked_for_one() -> None:
    with tempfile.TemporaryDirectory(prefix='transcript-') as store:
        path = store + '/session.jsonl'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'assistant',
                'message': {'content': [{'type': 'text', 'text': '시각 없는 발화'}]},
            }) + '\n')

        original = discovery.find_session
        discovery.find_session = lambda agent, session_id: {'path': path}
        try:
            check('an undatable message cannot be shown to be an answer',
                  discovery.last_agent_message('claude', 'sid', after=0) is None)
        finally:
            discovery.find_session = original


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
            # The request carried timeout=5, the sort of value a peer on an older build sends.
            # Inheriting it let that peer decide how long we may spend delivering our own
            # answer, and a reply that times out falls back to transcript recovery.
            check('and waits by our floor, not the timeout the requester happened to send',
                  reply is not None and reply.timeout == config.SEND_TIMEOUT_SECONDS,
                  str(reply and reply.timeout))
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


def test_a_busy_peer_is_retried_rather_than_failed() -> None:
    """Both agents handle concurrent input already — Claude waits out the turn, Codex queues —
    they just have a limit. Past it the shim says "busy", which is a moment, not a verdict."""
    original = outbox.BUSY_RETRY_SECONDS
    outbox.BUSY_RETRY_SECONDS = 0.02
    try:
        box = outbox.Outbox()
        attempts = {'count': 0}

        def busy_until_the_third_try(job):
            attempts['count'] += 1
            if attempts['count'] < 3:
                raise outbox.PeerBusyError('the panel session is busy with another turn')
            return {'session_id': job.target_session_id, 'reply': '늦게 받았습니다',
                    'is_new_session': False}

        box.deliver = busy_until_the_third_try
        delivery_id = box.submit(_job(box, 'sid-busy-peer'))
        _drain(box)

        job = box.find(delivery_id)
        check('a busy peer is retried until it is free',
              job.state == outbox.STATE_DELIVERED and attempts['count'] == 3,
              f'{job.state} after {attempts["count"]} attempts')
        check('and the message is delivered, not recovered',
              job.reply == '늦게 받았습니다' and not job.is_reply_recovered)
    finally:
        outbox.BUSY_RETRY_SECONDS = original


def test_a_shim_busy_answer_is_told_apart_from_a_real_failure() -> None:
    busy = {'ok': False, 'error': 'the panel session is busy with another turn'}
    inflight = {'ok': False, 'error': 'another bridged message is already in flight'}
    # a write that never went through: the message did not leave, so nothing is waited for
    broken = {'ok': False, 'error': 'failed to write to the claude process: EPIPE'}
    # a turn that ended badly after the hand-over: the peer has the message, its transcript
    # is where the answer (or the failure) will be
    aborted = {'ok': False, 'accepted': True, 'error': '{"message": "model overloaded"}'}

    original = bridge.uihook.send
    try:
        for response, expected, label in [
            (busy, outbox.PeerBusyError, 'a busy panel'),
            (inflight, outbox.PeerBusyError, 'a message already in flight'),
            (broken, outbox.NotDeliveredError, 'a broken pipe'),
            (aborted, bridge.BridgeError, 'a turn that failed after the hand-over'),
        ]:
            bridge.uihook.send = lambda *a, **kw: response
            try:
                bridge._call_via_panel('hi', 'sid', {'socket': '/s'}, 5, '/w')
                check(f'{label} raises something', False, 'nothing raised')
            except Exception as e:
                check(f'{label} is classified correctly', isinstance(e, expected),
                      f'{type(e).__name__} for {response["error"]!r}')
    finally:
        bridge.uihook.send = original


def test_a_panel_delivery_keeps_watching_after_the_transport_gives_up() -> None:
    """Giving up listening is not the peer giving up working.

    On the panel path the peer is a session we neither started nor stopped, so the socket
    timing out says nothing about its turn — thirteen deliveries were closed as failed today
    while their answers were being written.
    """
    original = (outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS)
    outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = 0.02, 2
    try:
        box = outbox.Outbox()
        box.deliver = lambda job: (_ for _ in ()).throw(
            RuntimeError('IDE panel relay failed: turn did not complete within 600s'))

        looks = {'count': 0}

        def answer_on_the_third_look(job):
            looks['count'] += 1
            return '늦게 도착한 답' if looks['count'] >= 3 else None

        box.recover = answer_on_the_third_look

        job = _job(box, 'sid-panel', wants_reply=True)
        job.ui_shim = {'socket': '/panel'}  # 패널 경로 — 상대는 우리가 죽일 수 없다
        delivery_id = box.submit(job)
        _drain(box)

        finished = box.find(delivery_id)
        check('an answer written after the transport failed is still collected',
              finished.reply == '늦게 도착한 답', f'{finished.state} {finished.reply!r}')
        check('and it is marked as recovered', finished.is_reply_recovered is True)
        check('the transport failure is still recorded', finished.error is not None)
        check('and the record closes as failed, not left as awaiting-peer',
              finished.state == outbox.STATE_FAILED, finished.state)
    finally:
        outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = original


def test_a_cli_delivery_does_not_wait_for_a_turn_that_was_killed() -> None:
    """The CLI turn was our own subprocess and the timeout killed its process group, so
    nothing more will be written and waiting would only stall the queue behind it."""
    original = (outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS)
    outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = 0.02, 5
    try:
        box = outbox.Outbox()
        box.deliver = lambda job: (_ for _ in ()).throw(
            RuntimeError('peer agent did not answer within 600s'))
        looks = {'count': 0}
        box.recover = lambda job: (looks.update(count=looks['count'] + 1) or None)

        started = time.time()
        delivery_id = box.submit(_job(box, 'sid-cli', wants_reply=True))  # ui_shim 없음 = CLI 경로
        _drain(box)
        elapsed = time.time() - started

        check('a killed turn is not waited on', looks['count'] == 1, str(looks))
        check('so the queue is not held open', elapsed < 1.0, f'{elapsed:.2f}s')
        check('and the delivery closes as failed',
              box.find(delivery_id).state == outbox.STATE_FAILED)
    finally:
        outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = original


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


# ------------------------------------------- recovery reads finished turns, not fragments

def _claude_line(text: str, stop_reason, request_id: str, at: float, kind: str = 'text') -> str:
    import datetime
    stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat()
    block = ({'type': 'thinking', 'thinking': text} if kind == 'thinking'
             else {'type': 'text', 'text': text})
    return json.dumps({
        'type': 'assistant', 'requestId': request_id,
        'timestamp': stamp.replace('+00:00', 'Z'),
        'message': {'stop_reason': stop_reason, 'content': [block]},
    })


def _human_line(text: str, at: float) -> str:
    import datetime
    stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat()
    return json.dumps({'type': 'user', 'timestamp': stamp.replace('+00:00', 'Z'),
                       'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}})


def test_recovery_waits_for_a_claude_turn_to_finish() -> None:
    """The incident: a 600s relay timed out, recovery read "reverting provenance now", and the
    requester acted on it as a report. That line was written between two tool calls, and the
    transcript says so - stop_reason=tool_use. Only a turn that ended has an answer."""
    sent_at = 1_788_000_000.0
    with tempfile.TemporaryDirectory(prefix='claude-turns-') as store:
        path = store + '/session.jsonl'
        original = discovery.find_session
        discovery.find_session = lambda agent, sid: {'path': path}
        try:
            lines = [_claude_line('`--source`를 넣느라 provenance가 갈렸습니다. 원래 값으로 되돌립니다.',
                                  'tool_use', 'r1', sent_at + 60)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('claude', 'sid', after=sent_at)
            check('a message written between tool calls is not an answer',
                  progress['answer'] is None, str(progress))
            check('and the peer is reported as still working', progress['is_working'] is True)

            # the turn ends: thinking and text arrive as two entries of one response
            lines += [_claude_line('정리하자면', 'end_turn', 'r2', sent_at + 300, kind='thinking'),
                      _claude_line('원복 완료. 11장 등록했습니다.', 'end_turn', 'r2', sent_at + 301)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('claude', 'sid', after=sent_at)
            check('the finished turn is the answer',
                  progress['answer'] == '원복 완료. 11장 등록했습니다.', str(progress['answer']))
            check('and the peer is no longer working', progress['is_working'] is False)
            check('last_agent_message reads the same finished turn',
                  discovery.last_agent_message('claude', 'sid', after=sent_at)
                  == '원복 완료. 11장 등록했습니다.')

            # the human asks something next: the answer stands, and the peer is busy again
            lines.append(_human_line('다음 건 진행해', sent_at + 400))
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('claude', 'sid', after=sent_at)
            check('a new human question does not unmake the finished answer',
                  progress['answer'] == '원복 완료. 11장 등록했습니다.')
            check('but does mean the peer is working again', progress['is_working'] is True)
        finally:
            discovery.find_session = original


def _codex_line(kind: str, payload: dict, at: float) -> str:
    import datetime
    stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat()
    return json.dumps({'timestamp': stamp.replace('+00:00', 'Z'), 'type': kind,
                       'payload': payload})


def test_recovery_reads_the_codex_turn_the_app_server_closed() -> None:
    """A rollout brackets each turn with task_started/task_complete, and the completion names
    the answer. Anything the agent said in between is narration between tool calls."""
    sent_at = 1_788_000_000.0
    token = 'req_1788000000000_abcdef'
    with tempfile.TemporaryDirectory(prefix='codex-turns-') as store:
        path = store + '/rollout.jsonl'
        original = discovery.find_session
        discovery.find_session = lambda agent, sid: {'path': path}
        try:
            lines = [
                _codex_line('event_msg', {'type': 'task_started', 'turn_id': 'tA'}, sent_at + 5),
                _codex_line('response_item', {'type': 'message', 'role': 'assistant',
                                              'content': [{'type': 'output_text',
                                                           'text': '2차 요청서를 확인했습니다. 시작합니다.'}]},
                            sent_at + 30),
            ]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('codex', 'sid', after=sent_at, token=token)
            check('an assistant message inside an open turn is not an answer',
                  progress['answer'] is None, str(progress))
            check('and the thread is reported as working', progress['is_working'] is True)

            lines.append(_codex_line('event_msg', {
                'type': 'task_complete', 'turn_id': 'tA',
                'last_agent_message': f'생성 완료: 11장.\n{token}'}, sent_at + 500))
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            progress = discovery.peer_progress('codex', 'sid', after=sent_at, token=token)
            check('task_complete carries the answer',
                  (progress['answer'] or '').startswith('생성 완료: 11장.'), str(progress['answer']))
            check('and the thread is idle', progress['is_working'] is False)

            # a completion that names no message falls back to the turn's last spoken item
            lines = [
                _codex_line('event_msg', {'type': 'task_started', 'turn_id': 'tB'}, sent_at + 5),
                _codex_line('event_msg', {'type': 'item_completed', 'turn_id': 'tB',
                                          'item': {'type': 'AgentMessage', 'text': '마지막 항목 발화'}},
                            sent_at + 40),
                _codex_line('event_msg', {'type': 'task_complete', 'turn_id': 'tB',
                                          'last_agent_message': None}, sent_at + 41),
            ]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            check('an empty completion is filled from the turn\'s last agent item',
                  discovery.last_agent_message('codex', 'sid', after=sent_at) == '마지막 항목 발화')

            # a rollout without task events (older Codex) still yields its last message
            lines = [_codex_line('response_item', {'type': 'message', 'role': 'assistant',
                                                   'content': [{'type': 'output_text',
                                                                'text': '옛 형식의 답'}]},
                                 sent_at + 10)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            check('a legacy rollout falls back to its last message',
                  discovery.last_agent_message('codex', 'sid', after=sent_at) == '옛 형식의 답')
        finally:
            discovery.find_session = original


def test_a_later_human_turn_does_not_hide_the_echoed_answer() -> None:
    """The panel is the human's session too. After the peer answered us, the human asked it
    something else - both turns postdate our request, and only one of them echoes our id."""
    sent_at = 1_788_000_000.0
    mine = 'req_1788000000000_aaaaaa'
    with tempfile.TemporaryDirectory(prefix='claude-turns-') as store:
        path = store + '/session.jsonl'
        original = discovery.find_session
        discovery.find_session = lambda agent, sid: {'path': path}
        try:
            lines = [_claude_line(f'등록 완료했습니다.\n{mine}', 'end_turn', 'r1', sent_at + 100),
                     _claude_line('네, 다음은 소라 세트입니다.', 'end_turn', 'r2', sent_at + 900)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            check('the turn that echoes our id is the answer, not the newer human exchange',
                  (discovery.last_agent_message('claude', 'sid', after=sent_at, token=mine) or '')
                  .startswith('등록 완료했습니다.'))

            # a peer that echoes nothing: timing decides, as before
            lines = [_claude_line('토큰 없이 답합니다.', 'end_turn', 'r1', sent_at + 100)]
            open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            check('a peer that echoed nothing still falls back to the clock',
                  discovery.last_agent_message('claude', 'sid', after=sent_at, token=mine)
                  == '토큰 없이 답합니다.')
        finally:
            discovery.find_session = original


# ------------------------------------ the outbox tells the truth about how a delivery ended

def test_giving_up_watching_closes_the_delivery_as_failed() -> None:
    """The residue: 26 records sat in awaiting-peer forever, because the watch set that state
    and nothing set it back when the watch ended empty."""
    original = (outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS)
    outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = 0.02, 0.1
    try:
        box = outbox.Outbox()
        box.deliver = lambda job: (_ for _ in ()).throw(
            RuntimeError('IDE panel relay failed: turn still running after 3600s'))
        box.recover = lambda job: None

        job = _job(box, 'sid-silent', wants_reply=True)
        job.ui_shim = {'socket': '/panel'}
        delivery_id = box.submit(job)
        _drain(box)

        finished = box.find(delivery_id)
        check('a watch that ends empty closes the delivery as failed',
              finished.state == outbox.STATE_FAILED, finished.state)
        check('and keeps the transport error', finished.error is not None)
    finally:
        outbox.RECOVERY_POLL_SECONDS, outbox.RECOVERY_WINDOW_SECONDS = original


def test_a_reply_counts_as_delivered_once_the_peer_takes_it() -> None:
    """36 replies were recorded as failed today. Each had landed; what timed out was the
    sender's own next turn, which nobody needed to wait for."""
    awaits = []
    original = (bridge.uihook.send, bridge.uihook.await_turn)
    bridge.uihook.send = lambda *a, **kw: {
        'ok': False, 'pending': True, 'accepted': True, 'injectionId': 'xagent-1',
        'sessionId': 'sender-sid', 'error': 'turn still running after 0s'}
    bridge.uihook.await_turn = lambda shim, injection_id, timeout: (
        awaits.append(injection_id) or {'ok': True, 'sessionId': 'sender-sid', 'reply': 'x'})
    accepted = []
    try:
        result = bridge._call_via_panel('answer', 'sender-sid', {'socket': '/s'}, 600, '/w',
                                        on_accepted=lambda r: accepted.append(r), wants_result=False)
        check('a reply returns as soon as the peer has it', result['session_id'] == 'sender-sid')
        check('without waiting for the sender\'s next turn', awaits == [], str(awaits))
        check('and acceptance is reported', len(accepted) == 1)
    finally:
        bridge.uihook.send, bridge.uihook.await_turn = original


def test_a_panel_request_is_listened_to_until_the_turn_ends() -> None:
    """Turns of 500..820s were routine and the socket wait was 600s. Now the first wait only
    covers the hand-over; the answer is collected with as many awaits as the turn takes."""
    awaits = []
    original = (bridge.uihook.send, bridge.uihook.await_turn)
    bridge.uihook.send = lambda *a, **kw: {
        'ok': False, 'pending': True, 'accepted': True, 'injectionId': 'xagent-2',
        'sessionId': 'peer-sid'}

    def await_turn(shim, injection_id, timeout):
        awaits.append(timeout)
        if len(awaits) < 3:
            return {'ok': False, 'pending': True, 'accepted': True, 'injectionId': injection_id,
                    'sessionId': 'peer-sid', 'partial': '아직'}
        return {'ok': True, 'sessionId': 'peer-sid', 'reply': '11장 등록 완료'}

    bridge.uihook.await_turn = await_turn
    try:
        result = bridge._call_via_panel('do it', 'peer-sid', {'socket': '/s'}, 600, '/w',
                                        wants_result=True, patience=3600)
        check('the answer is collected after the turn ends', result['reply'] == '11장 등록 완료')
        check('across as many awaits as it took', len(awaits) == 3, str(awaits))
        check('each bounded by the await chunk',
              all(t <= bridge.PANEL_AWAIT_CHUNK_SECONDS for t in awaits), str(awaits))

        # patience is a ceiling, not a verdict: past it the transcript watch takes over
        def never_ends(shim, injection_id, timeout):
            time.sleep(0.02)
            return {'ok': False, 'pending': True, 'accepted': True, 'injectionId': injection_id}

        bridge.uihook.await_turn = never_ends
        try:
            bridge._call_via_panel('do it', 'peer-sid', {'socket': '/s'}, 600, '/w',
                                   wants_result=True, patience=0.01)
            check('running out of patience raises', False, 'nothing raised')
        except bridge.BridgeError as e:
            check('running out of patience hands over to the transcript watch',
                  'still running' in str(e) and 'transcript' in str(e), str(e))
        except Exception as e:
            check('running out of patience raises a BridgeError', False, repr(e))
    finally:
        bridge.uihook.send, bridge.uihook.await_turn = original


def test_a_refusal_is_told_apart_from_a_broken_transport() -> None:
    cases = [
        ({'ok': False, 'accepted': False, 'error': 'thread x is not open in this panel'},
         outbox.NotDeliveredError, 'a current shim saying it never landed'),
        ({'ok': False, 'accepted': True, 'error': '{"message": "turn failed"}'},
         bridge.BridgeError, 'a current shim reporting a failed turn'),
        ({'ok': False, 'error': 'IDE panel relay failed: {"code": -32600, "message": "thread not found: 01a0"}'},
         outbox.NotDeliveredError, 'an older shim saying thread not found'),
        ({'ok': False, 'error': 'turn did not complete within 600s'},
         bridge.BridgeError, 'an older shim timing out'),
        ({'ok': False, 'error': 'the panel session is busy with another turn'},
         outbox.PeerBusyError, 'a busy peer'),
        ({'ok': False, 'accepted': False, 'error': 'panel shim unreachable: ConnectionRefusedError'},
         outbox.NotDeliveredError, 'a socket nobody answers'),
    ]
    for response, expected, label in cases:
        try:
            bridge._raise_for_panel_failure(response)
            check(f'{label} raises', False, 'nothing raised')
        except Exception as e:
            check(f'{label} is classified as {expected.__name__}', isinstance(e, expected),
                  type(e).__name__)
    check('a pending answer is not a failure',
          bridge._raise_for_panel_failure({'ok': False, 'pending': True}) is None)


def test_an_undelivered_request_is_refused_while_the_caller_is_still_there() -> None:
    """The lost-order case: send_to_codex said accepted=true, the app server said "thread not
    found" a second later, and nobody was told. The caller is still on the line for that
    second, so the refusal goes straight back to it."""
    notices = []
    originals = (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
                 bridge.uihook.send, outbox.OUTBOX.build_notice, bridge.registry.touch_pin)
    bridge.caller.detect_caller = lambda: {'agent': config.AGENT_CLAUDE, 'chain': []}
    bridge._own_session_id = lambda agent: 'sender-sid'
    bridge._resolve_target = lambda *a, **kw: {
        'agent': config.AGENT_CODEX, 'session_id': 'peer-' + uuid_hex(), 'cwd': None,
        'source': 'ide-panel', 'ui_shim': {'socket': '/nowhere', 'pid': 1}}
    bridge.uihook.send = lambda *a, **kw: {
        'ok': False, 'accepted': False,
        'error': '{"code": -32600, "message": "thread not found: 01a06f72"}'}
    outbox.OUTBOX.build_notice = lambda job: notices.append(job.delivery_id)
    bridge.registry.touch_pin = lambda agent, cwd: None
    try:
        started = time.time()
        result = bridge.send_message(config.AGENT_CODEX, 'register the images',
                                     conversation_id='conv_refused_' + uuid_hex())
        check('the refusal comes back in the tool result',
              result.get('ok') is False and result.get('accepted') is False, str(result)[:200])
        check('naming the cause', 'thread not found' in (result.get('error') or ''),
              str(result.get('error')))
        check('and saying the peer never got it', result.get('is_undelivered') is True)
        check('within seconds, not after a recovery window',
              time.time() - started < outbox.EARLY_FAILURE_WINDOW_SECONDS + 2,
              f'{time.time() - started:.1f}s')
        time.sleep(0.2)
        check('no notice is sent on top of the direct answer', notices == [], str(notices))
    finally:
        (bridge.caller.detect_caller, bridge._own_session_id, bridge._resolve_target,
         bridge.uihook.send, outbox.OUTBOX.build_notice, bridge.registry.touch_pin) = originals


def test_a_late_failure_is_announced_into_the_senders_session() -> None:
    """When the caller has already been told "accepted" and the delivery then fails, the
    failure has to travel the same way an answer would."""
    box = outbox.Outbox()
    outcomes = []

    def deliver(job):
        if job.kind == outbox.KIND_NOTICE:
            outcomes.append(('notice', job.target_session_id))
            return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}
        raise outbox.NotDeliveredError('IDE panel relay refused: thread not found: 01a0')

    box.deliver = deliver
    box.recover = lambda job: outcomes.append(('recover', job.delivery_id))
    box.build_notice = lambda job: _notice(box, job)

    job = _job(box, 'sid-gone', wants_reply=True)
    job.report_failures_until = 0.0  # the caller left long ago
    delivery_id = box.submit(job)
    _drain(box)

    failed = box.find(delivery_id)
    check('the request closes as failed', failed.state == outbox.STATE_FAILED)
    check('marked as never delivered', failed.is_undelivered is True)
    check('nothing is recovered for a message that never landed',
          ('recover', delivery_id) not in outcomes, str(outcomes))
    check('and a notice is delivered to the sender', ('notice', 'sender-sid') in outcomes,
          str(outcomes))

    # the same failure inside the caller's window is the caller's to hear, not a notice
    outcomes.clear()
    job = _job(box, 'sid-gone', wants_reply=True)
    job.report_failures_until = time.time() + 5
    box.submit(job)
    _drain(box)
    check('a failure the caller is told about directly is not also announced',
          not any(kind == 'notice' for kind, _ in outcomes), str(outcomes))


def _notice(box: outbox.Outbox, failed: outbox.Job) -> outbox.Job:
    return outbox.Job(
        target_agent=failed.sender_agent, target_session_id=failed.sender_session_id,
        payload='=== CROSS-AGENT BRIDGE DELIVERY FAILED ===', run_cwd='/tmp', pin_cwd='/tmp',
        env={}, timeout=5, ui_shim=None, title=None, conversation_id=failed.conversation_id,
        hop=failed.hop, sender_agent=failed.target_agent,
        sender_session_id=failed.target_session_id, wants_reply=False, summary='notice',
        kind=outbox.KIND_NOTICE)


def test_a_reply_that_did_not_land_is_rerouted_and_retried() -> None:
    """The sender's tab was reopened while the peer worked: new process, new socket, and the
    old route refuses the connection. The answer is re-aimed and tried again."""
    original = outbox.UNDELIVERED_RETRY_SECONDS
    outbox.UNDELIVERED_RETRY_SECONDS = 0.01
    try:
        box = outbox.Outbox()
        attempts = {'count': 0}
        rerouted = []

        def deliver(job):
            attempts['count'] += 1
            if attempts['count'] == 1:
                raise outbox.NotDeliveredError('panel shim unreachable: ConnectionRefusedError')
            return {'session_id': job.target_session_id, 'reply': '', 'is_new_session': False}

        box.deliver = deliver
        box.reroute = lambda job: rerouted.append(job.delivery_id)

        delivery_id = box.submit(_job(box, 'sender-sid'))  # wants_reply=False: a reply
        _drain(box)
        job = box.find(delivery_id)
        check('a reply that never landed is tried again', job.state == outbox.STATE_DELIVERED,
              f'{job.state} {job.error}')
        check('after being re-aimed', rerouted == [delivery_id], str(rerouted))
        check('and the attempt count says so', job.attempts == 2, str(job.attempts))

        # a request is not retried blindly: its sender is told and decides
        attempts['count'] = 0
        rerouted.clear()
        delivery_id = box.submit(_job(box, 'peer-sid', wants_reply=True))
        _drain(box)
        job = box.find(delivery_id)
        check('a request that never landed is not resent on its own',
              job.state == outbox.STATE_FAILED and job.attempts == 1 and not rerouted,
              f'{job.state} attempts={job.attempts} rerouted={rerouted}')
    finally:
        outbox.UNDELIVERED_RETRY_SECONDS = original


# --------------------------------------------------- the shims keep a turn past the socket

class FakeAppServer:
    """Answers the shim's injected requests the way `codex app-server` would."""

    def __init__(self, shim, is_thread_loaded: bool) -> None:
        self.shim = shim
        self.is_thread_loaded = is_thread_loaded
        self.writes = []
        self.completes_turns = True

    def write(self, payload: str) -> None:
        message = json.loads(payload)
        self.writes.append(message['method'])
        threading.Timer(0.01, self._respond, args=(message,)).start()

    def _respond(self, message: dict) -> None:
        method, request_id, params = message['method'], message['id'], message['params']
        if method == 'thread/resume':
            self.is_thread_loaded = True
            self.shim._observe_from_server({'id': request_id, 'result': {
                'thread': {'id': params['threadId']}}})
        elif method == 'turn/start':
            if not self.is_thread_loaded:
                self.shim._observe_from_server({'id': request_id, 'error': {
                    'code': -32600, 'message': f'thread not found: {params["threadId"]}'}})
                return
            self.shim._observe_from_server({'id': request_id, 'result': {'turn': {'id': 'turn-1'}}})
            if self.completes_turns:
                self.complete(params['threadId'])

    def complete(self, thread_id: str) -> None:
        self.shim._observe_from_server({'method': 'item/completed', 'params': {
            'threadId': thread_id, 'turnId': 'turn-1',
            'item': {'type': 'agentMessage', 'text': 'PANEL_OK'}}})
        self.shim._observe_from_server({'method': 'turn/completed', 'params': {
            'threadId': thread_id, 'turn': {'id': 'turn-1', 'status': 'completed'}}})


def _codex_shim():
    from cross_agent_mcp.appserver_shim import CodexAppServerShim
    shim = CodexAppServerShim.__new__(CodexAppServerShim)
    shim.threads = {}
    shim.injections = {}
    shim.opens = {}
    shim.turns = {}
    shim.state_lock = threading.Lock()
    shim.stdin_lock = threading.Lock()
    shim.last_user_activity = 0.0
    return shim


def test_the_codex_shim_reloads_a_thread_the_app_server_forgot() -> None:
    """Six orders were lost to "thread not found" today, and the only cure was a human typing
    into the Codex panel - which makes the extension send thread/resume. The shim sends it."""
    shim = _codex_shim()
    server = FakeAppServer(shim, is_thread_loaded=False)
    shim.write_to_child = server.write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-1'}})

    result = shim.inject('hello', 'thr-1', timeout=5)
    check('a thread the app server forgot is resumed and the turn goes through',
          result.get('ok') is True and result.get('reply') == 'PANEL_OK', str(result)[:200])
    check('by resuming between the two attempts',
          server.writes == ['turn/start', 'thread/resume', 'turn/start'], str(server.writes))
    check('and the thread is known to be loaded again', shim._is_loaded('thr-1'))

    # a resume that fails leaves an honest refusal, marked as never delivered
    shim = _codex_shim()
    server = FakeAppServer(shim, is_thread_loaded=False)
    original_respond = server._respond

    def never_resumes(message):
        if message['method'] == 'thread/resume':
            shim._observe_from_server({'id': message['id'], 'error': {
                'code': -32600, 'message': 'no rollout found for thread'}})
            return
        original_respond(message)

    server._respond = never_resumes
    shim.write_to_child = server.write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-2'}})
    result = shim.inject('hello', 'thr-2', timeout=5)
    check('a thread that cannot be brought back is refused, not waited on',
          result.get('ok') is False and result.get('accepted') is False
          and 'thread not found' in result.get('error', '')
          and 'thread/resume' in result.get('error', ''), str(result)[:300])


def test_the_codex_shim_resumes_first_when_it_saw_the_thread_close() -> None:
    shim = _codex_shim()
    server = FakeAppServer(shim, is_thread_loaded=True)
    shim.write_to_child = server.write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-3'}})

    shim._observe_from_server({'method': 'thread/closed', 'params': {'threadId': 'thr-3'}})
    check('thread/closed marks the thread as unloaded', not shim._is_loaded('thr-3'))
    check('but does not forget it', 'thr-3' in shim.threads)

    server.is_thread_loaded = False
    result = shim.inject('hello', None, timeout=5)
    check('a thread seen closing is resumed before the turn is started',
          server.writes[:2] == ['thread/resume', 'turn/start'] and result.get('ok') is True,
          f'{server.writes} {str(result)[:120]}')

    shim._observe_from_server({'method': 'thread/status/changed', 'params': {
        'threadId': 'thr-3', 'status': {'type': 'notLoaded'}}})
    check('status notLoaded marks it as unloaded too', not shim._is_loaded('thr-3'))


def test_a_codex_turn_outliving_the_first_wait_can_be_awaited() -> None:
    """The 600s cut. The shim used to drop the turn when the socket wait ended; now the socket
    returns a receipt at acceptance and the turn is collected later, however long it takes."""
    shim = _codex_shim()
    server = FakeAppServer(shim, is_thread_loaded=True)
    server.completes_turns = False
    shim.write_to_child = server.write
    shim._observe_from_client({'method': 'turn/start', 'params': {'threadId': 'thr-4'}})

    receipt = shim.inject('long task', 'thr-4', timeout=600, accept_timeout=2)
    check('the hand-over returns a receipt, not an answer',
          receipt.get('pending') is True and receipt.get('accepted') is True
          and receipt.get('injectionId'), str(receipt)[:200])
    check('with the turn id the app server assigned', receipt.get('turnId') == 'turn-1')

    still = shim.await_turn(receipt['injectionId'], timeout=0.05)
    check('awaiting before the turn ends says so', still.get('pending') is True, str(still)[:120])

    threading.Timer(0.05, server.complete, args=('thr-4',)).start()
    done = shim.await_turn(receipt['injectionId'], timeout=2)
    check('awaiting after the turn ends returns the answer',
          done.get('ok') is True and done.get('reply') == 'PANEL_OK', str(done)[:200])

    again = shim.await_turn(receipt['injectionId'], timeout=0.05)
    check('a collected turn is gone', again.get('ok') is False and 'no turn' in again.get('error', ''))

    server.completes_turns = True
    old_style = shim.inject('x', 'thr-4', timeout=5)
    check('an old-style send without acceptTimeout still waits for the whole turn',
          old_style.get('ok') is True and old_style.get('reply') == 'PANEL_OK', str(old_style)[:160])


def test_the_claude_shim_hands_over_and_reports_later() -> None:
    from cross_agent_mcp.claude_shim import ClaudeStreamShim
    shim = ClaudeStreamShim.__new__(ClaudeStreamShim)
    shim.session_id = 'sess-1'
    shim.cwd = '/w'
    shim.is_turn_active = False
    shim.injection = None
    shim.turns = {}
    shim.state_lock = threading.Lock()
    shim.stdin_lock = threading.Lock()
    shim.last_seen = time.time()
    shim.last_user_activity = 0.0
    writes = []
    shim.write_to_child = lambda payload: writes.append(json.loads(payload))

    receipt = shim.inject('hello', 'sess-1', timeout=600, accept_timeout=2)
    check('the write going through is the receipt',
          receipt.get('pending') is True and receipt.get('accepted') is True, str(receipt)[:160])
    check('and the panel session is marked busy with it', shim.is_turn_active is True)

    shim._observe_from_agent({'type': 'assistant', 'session_id': 'sess-1',
                              'message': {'content': [{'type': 'text', 'text': '작업 중'}]}})
    still = shim.await_turn(receipt['injectionId'], timeout=0.05)
    check('mid-turn narration is only a partial', still.get('pending') is True
          and still.get('partial') == '작업 중', str(still)[:160])

    shim._observe_from_agent({'type': 'result', 'session_id': 'sess-1', 'result': '완료했습니다'})
    done = shim.await_turn(receipt['injectionId'], timeout=1)
    check('the CLI\'s own result ends the turn', done.get('ok') is True
          and done.get('reply') == '완료했습니다', str(done)[:160])
    check('and frees the session for the next message',
          shim.injection is None and shim.is_turn_active is False)

    check('a message for another session is refused as never landed',
          shim.inject('x', 'other', timeout=1).get('accepted') is False)


def test_a_long_panel_turn_keeps_its_busy_lock() -> None:
    """Patience now runs past two turn budgets, which is where another process used to judge a
    lock abandoned and start a second turn on the same session."""
    session_id = 'unit-ttl-' + os.urandom(4).hex()
    path = registry._lock_path(config.AGENT_CODEX, session_id)

    def age(by_seconds: float) -> None:
        record = json.load(open(path, encoding='utf-8'))
        record['started_at'] -= by_seconds
        json.dump(record, open(path, 'w', encoding='utf-8'))

    with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_ttl', ttl_seconds=5000):
        age(config.SEND_TIMEOUT_SECONDS * 2 + 60)
        check('a lock that declared its patience survives past two turn budgets',
              registry.read_busy_lock(config.AGENT_CODEX, session_id) is not None)
        age(5000)
        check('but not past its own patience',
              registry.read_busy_lock(config.AGENT_CODEX, session_id) is None)

    with registry.busy_lock(config.AGENT_CODEX, session_id, 'conv_ttl'):
        age(config.SEND_TIMEOUT_SECONDS * 2 + 60)
        check('a lock without a declared patience is judged as before',
              registry.read_busy_lock(config.AGENT_CODEX, session_id) is None)


def test_delivery_report_rereads_the_peer_transcript() -> None:
    """The follow-up read koppa asked for: a recovered fragment, or a notice that the peer may
    still be working, and a way to look again once it has finished."""
    original_dir = outbox.config.DELIVERY_DIR
    original_progress = bridge.discovery.peer_progress
    with tempfile.TemporaryDirectory(prefix='cross-agent-deliveries-') as store:
        outbox.config.DELIVERY_DIR = store + '/'
        try:
            box = outbox.Outbox()
            box.deliver = lambda job: (_ for _ in ()).throw(RuntimeError('turn still running'))
            box.recover = lambda job: None
            job = _job(box, 'peer-sid', wants_reply=True)
            delivery_id = box.submit(job)
            _drain(box)
            outbox._write_record(box.find(delivery_id).describe())

            asked = {}

            def progress(agent, session_id, after=None, token=None):
                asked.update(agent=agent, session_id=session_id, after=after, token=token)
                return {'answer': '늦게 완성된 최종 보고', 'answered_at': 1.0, 'is_working': False,
                        'last_turn_finished_at': 1.0, 'transcript_mtime': 1.0}

            bridge.discovery.peer_progress = progress
            report = bridge.delivery_report(delivery_id)
            check('a known delivery is reported', report.get('ok') is True, str(report)[:200])
            check('with a fresh read of the peer transcript',
                  report.get('peer_transcript', {}).get('answer') == '늦게 완성된 최종 보고')
            check('filtered by the request time and matched by its id',
                  asked.get('after') == job.started_at and asked.get('token') == delivery_id,
                  str(asked))
            check('an unknown delivery says so',
                  bridge.delivery_report('req_0_000000').get('ok') is False)
        finally:
            outbox.config.DELIVERY_DIR = original_dir
            bridge.discovery.peer_progress = original_progress


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
    test_a_short_timeout_is_raised_to_the_configured_budget()
    test_an_echoed_token_identifies_which_request_was_answered()
    test_a_request_id_is_short_and_carries_its_time()
    test_recovery_refuses_a_message_older_than_the_question()
    test_recovery_refuses_a_message_with_no_timestamp_when_asked_for_one()
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
    test_a_busy_peer_is_retried_rather_than_failed()
    test_a_shim_busy_answer_is_told_apart_from_a_real_failure()
    test_a_panel_delivery_keeps_watching_after_the_transport_gives_up()
    test_a_cli_delivery_does_not_wait_for_a_turn_that_was_killed()
    test_recovery_is_skipped_when_the_transport_already_answered()
    test_a_conversations_own_name_is_what_it_is_called_by()
    test_a_session_name_matches_exactly_or_not_at_all()
    test_a_named_session_is_never_silently_created()
    test_naming_a_session_and_forcing_a_new_one_is_refused()
    test_a_delivery_does_not_outlive_the_server_that_started_it()
    test_recovery_waits_for_a_claude_turn_to_finish()
    test_recovery_reads_the_codex_turn_the_app_server_closed()
    test_a_later_human_turn_does_not_hide_the_echoed_answer()
    test_giving_up_watching_closes_the_delivery_as_failed()
    test_a_reply_counts_as_delivered_once_the_peer_takes_it()
    test_a_panel_request_is_listened_to_until_the_turn_ends()
    test_a_refusal_is_told_apart_from_a_broken_transport()
    test_an_undelivered_request_is_refused_while_the_caller_is_still_there()
    test_a_late_failure_is_announced_into_the_senders_session()
    test_a_reply_that_did_not_land_is_rerouted_and_retried()
    test_the_codex_shim_reloads_a_thread_the_app_server_forgot()
    test_the_codex_shim_resumes_first_when_it_saw_the_thread_close()
    test_a_codex_turn_outliving_the_first_wait_can_be_awaited()
    test_the_claude_shim_hands_over_and_reports_later()
    test_a_long_panel_turn_keeps_its_busy_lock()
    test_delivery_report_rereads_the_peer_transcript()

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
