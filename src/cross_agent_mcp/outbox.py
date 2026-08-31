"""Background delivery of bridged messages, so no agent ever blocks on a peer's turn.

A relay used to be a blocking call: the sender waited inside the tool while the peer ran a
whole turn, which is why both sessions had to be locked - a peer relaying back into a sender
that is parked waiting would deadlock. Peer turns in real use run for minutes (216s..660s
observed), so the wait was also the thing that hit the timeout.

Here the tool call only hands the message over. A worker carries out the delivery, and when
the peer's turn produces an answer the worker relays that answer back into the sender's
session as its own message. The sender sees the reply as an inbound turn instead of a return
value, which is the same information without anybody holding a lock.

Ordering: one worker thread per target session, so two messages aimed at the same session are
delivered one after another. Different sessions proceed in parallel. This matters beyond
fairness - the agent CLIs keep a single writer per transcript, so overlapping turns on one
session are rejected by the CLI itself.
"""

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from . import config, registry


logger = logging.getLogger('cross_agent_mcp.outbox')

# a worker with an empty queue waits this long for more work before retiring, so a burst of
# messages to one session reuses a single thread instead of starting one per message
IDLE_LINGER_SECONDS = 30

# how long a worker keeps retrying a session another process holds a busy lock on
BUSY_RETRY_SECONDS = 5

# completed jobs kept for status reporting
HISTORY_LIMIT = 50

# how much of a peer's answer the delivery record carries. The answer is normally read in the
# session it was delivered into; this copy is what a caller with no session of its own - a
# headless call, a test harness - has to work with.
REPLY_PREVIEW_LIMIT = 2000

STATE_QUEUED = 'queued'
STATE_DELIVERING = 'delivering'
STATE_AWAITING = 'awaiting-peer'
STATE_DELIVERED = 'delivered'
STATE_FAILED = 'failed'

# How long to keep looking for an answer after the transport gave up, and how often to look.
#
# Giving up listening is not the same as the peer giving up working. On the panel path the
# peer is a session we neither own nor can stop: the socket wait ended, the turn did not.
# Thirteen deliveries were closed as failed today while their answers were being written.
RECOVERY_WINDOW_SECONDS = 900
RECOVERY_POLL_SECONDS = 15


def new_request_id() -> str:
    """`req_<epoch ms>_<6 hex>` — short enough for a peer to copy back without mangling it.

    The millisecond prefix is the useful half: a token read out of a log, a delivery record or
    the peer's transcript says when its request went out, and sorting the tokens sorts the
    requests. The random tail only has to survive two requests leaving in the same millisecond.
    """
    return f'req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}'


def _write_record(record: Dict[str, Any]) -> None:
    """Keep a finished delivery where the next server process can still read it.

    Only what `describe()` returns is written - never the payload or the child environment,
    which carries every variable this process was started with.
    """
    try:
        config.ensure_dirs()
        path = config.DELIVERY_DIR + f"{record['delivery_id']}.json"
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({**record, 'finished_at': time.time()}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f'_write_record [exception]: {e}')


def read_records(limit: int = 20) -> List[Dict[str, Any]]:
    """Finished deliveries from disk, newest first, pruning what has aged out."""
    records: List[Dict[str, Any]] = []
    try:
        config.ensure_dirs()
        names = os.listdir(config.DELIVERY_DIR)
    except OSError:
        return records

    now = time.time()
    for name in names:
        if not name.endswith('.json'):
            continue
        path = config.DELIVERY_DIR + name
        try:
            with open(path, 'r', encoding='utf-8') as f:
                record = json.load(f)
        except Exception:
            with contextlib.suppress(OSError):
                os.remove(path)
            continue

        if now - float(record.get('finished_at') or 0) > config.DELIVERY_TTL_SECONDS:
            with contextlib.suppress(OSError):
                os.remove(path)
            continue
        records.append(record)

    records.sort(key=lambda r: float(r.get('finished_at') or 0), reverse=True)
    return records[:limit]


class Job:
    """One message on its way to a peer session."""

    def __init__(self, target_agent: str, target_session_id: Optional[str], payload: str,
                 run_cwd: str, pin_cwd: str, env: Dict[str, str], timeout: int,
                 ui_shim: Optional[Dict[str, Any]], title: Optional[str],
                 conversation_id: str, hop: int, sender_agent: str,
                 sender_session_id: Optional[str], wants_reply: bool,
                 summary: str, delivery_id: Optional[str] = None) -> None:
        self.delivery_id = delivery_id or new_request_id()
        self.target_agent = target_agent
        self.target_session_id = target_session_id
        self.payload = payload
        self.run_cwd = run_cwd
        # the caller's own directory, which keys the session pin - not necessarily run_cwd
        self.pin_cwd = pin_cwd
        self.env = env
        self.timeout = timeout
        self.ui_shim = ui_shim
        self.title = title
        self.conversation_id = conversation_id
        self.hop = hop
        self.sender_agent = sender_agent
        self.sender_session_id = sender_session_id
        # A reply carries no reply of its own: that is what terminates the exchange.
        self.wants_reply = wants_reply
        self.summary = summary

        self.state = STATE_QUEUED
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.reply = ''
        self.reply_length = 0
        self.is_reply_recovered = False
        self.resolved_session_id: Optional[str] = None

    def key(self) -> str:
        """Deliveries sharing this key are serialised."""
        return f'{self.target_agent}:{self.target_session_id or "new"}'

    def describe(self) -> Dict[str, Any]:
        return {
            'delivery_id': self.delivery_id,
            'state': self.state,
            'target_agent': self.target_agent,
            'target_session_id': self.resolved_session_id or self.target_session_id,
            'sender_agent': self.sender_agent,
            'conversation_id': self.conversation_id,
            'hop': self.hop,
            'is_reply': not self.wants_reply,
            'summary': self.summary,
            'queued_seconds': round((self.started_at or time.time()) - self.created_at, 1),
            'elapsed_seconds': (round((self.finished_at or time.time()) - self.started_at, 1)
                                if self.started_at else None),
            'reply_length': self.reply_length or None,
            'reply_preview': self.reply[:REPLY_PREVIEW_LIMIT] or None,
            # true when the answer was read out of the peer's transcript instead of being
            # handed back by the process this server started
            'is_reply_recovered': self.is_reply_recovered or None,
            'error': self.error,
        }


class Outbox:
    """Queues, workers and the small amount of history `bridge_status` reports."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._queues: Dict[str, List[Job]] = {}
        self._wakeups: Dict[str, threading.Condition] = {}
        self._workers: Dict[str, threading.Thread] = {}
        self._pending: Dict[str, Job] = {}
        self._history: List[Job] = []

        # injected by bridge to avoid an import cycle
        self.deliver: Optional[Callable[[Job], Dict[str, Any]]] = None
        self.build_reply: Optional[Callable[[Job, str], Optional['Job']]] = None
        self.recover: Optional[Callable[[Job], Optional[str]]] = None

    # ------------------------------------------------------------- submission

    def submit(self, job: Job) -> str:
        key = job.key()
        with self._guard:
            self._pending[job.delivery_id] = job
            self._queues.setdefault(key, []).append(job)
            condition = self._wakeups.setdefault(key, threading.Condition())

            worker = self._workers.get(key)
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._work, args=(key,), name=f'outbox:{key}', daemon=True)
                self._workers[key] = worker
                worker.start()

        with condition:
            condition.notify_all()

        logger.info(f'submit [queued]: {job.delivery_id} -> {key} conv={job.conversation_id}')
        return job.delivery_id

    def depth(self, key: str) -> int:
        with self._guard:
            return len(self._queues.get(key, []))

    # ---------------------------------------------------------------- workers

    def _next(self, key: str) -> Optional[Job]:
        with self._guard:
            queue = self._queues.get(key) or []
            return queue.pop(0) if queue else None

    def _retire(self, key: str) -> bool:
        """Drop this worker's registration, unless work arrived while it was deciding."""
        with self._guard:
            if self._queues.get(key):
                return False
            self._queues.pop(key, None)
            self._workers.pop(key, None)
            self._wakeups.pop(key, None)
            return True

    def _work(self, key: str) -> None:
        while True:
            job = self._next(key)
            if job is None:
                condition = self._wakeups.get(key)
                if condition is None:
                    return
                # Re-check while holding the condition. A submit that landed between the
                # failed _next and here cannot notify until we wait, so without this the
                # worker would sleep out the full linger on an already-queued message.
                with condition:
                    if self._next_is_empty(key):
                        condition.wait(IDLE_LINGER_SECONDS)
                if self._next_is_empty(key) and self._retire(key):
                    return
                continue

            self._run(job)

    def _next_is_empty(self, key: str) -> bool:
        with self._guard:
            return not self._queues.get(key)

    def _run(self, job: Job) -> None:
        job.state = STATE_DELIVERING
        job.started_at = time.time()
        logger.info(f'_run [BEGIN]: {job.delivery_id} {job.sender_agent}->{job.target_agent} '
                    f'session={job.target_session_id or "NEW"} conv={job.conversation_id}')

        try:
            result = self._deliver_with_lock(job)
            job.resolved_session_id = result.get('session_id') or job.target_session_id
            reply = str(result.get('reply') or '').strip()
            job.reply = reply
            job.reply_length = len(reply)
            job.state = STATE_DELIVERED
        except Exception as e:
            job.state = STATE_FAILED
            job.error = f'{type(e).__name__}: {e}'
            logger.error(f'_run [exception]: {job.delivery_id} {job.error}')

        # The peer may have answered even when we did not receive it - a transport that broke
        # after the turn, a process that died holding the result. The answer is on disk in the
        # peer's own transcript either way, so ask there before giving up on it.
        if not job.reply:
            recovered = self._recover_with_patience(job)
            if recovered:
                job.reply = recovered
                job.reply_length = len(recovered)
                job.is_reply_recovered = True
                logger.info(f'_run [recovered]: {job.delivery_id} read the answer from the '
                            f'{job.target_agent} transcript ({len(recovered)} chars)')

        try:
            if job.reply and job.wants_reply:
                self._send_reply(job, job.reply)
        finally:
            job.finished_at = time.time()
            self._archive(job)
            logger.info(f'_run [END]: {job.delivery_id} state={job.state} '
                        f'reply_chars={job.reply_length}')

    def _deliver_with_lock(self, job: Job) -> Dict[str, Any]:
        """Hold the cross-process busy lock only while the turn actually runs.

        Same-session ordering is already handled by this worker being the only one for the key.
        The file lock still matters because a second MCP server process - the peer's own - can
        aim at the same session from outside this process.
        """
        if self.deliver is None:
            raise RuntimeError('outbox has no delivery function installed')

        if not job.target_session_id:
            return self.deliver(job)

        deadline = time.time() + job.timeout
        while True:
            try:
                with registry.busy_lock(job.target_agent, job.target_session_id,
                                        job.conversation_id):
                    return self.deliver(job)
            except registry.SessionBusyError:
                if time.time() >= deadline:
                    raise
                logger.debug(f'_deliver_with_lock [busy]: {job.target_session_id}, retrying')
                time.sleep(BUSY_RETRY_SECONDS)

    def _recover_with_patience(self, job: Job) -> Optional[str]:
        """Look once, then keep looking while the peer could still be writing.

        Only the panel path waits. There the peer is a session we neither started nor stopped,
        so our socket giving up says nothing about its turn. On the CLI path the turn *was*
        our subprocess and a timeout killed its process group, so nothing further will be
        written and waiting would only stall the queue behind it.
        """
        text = self._recover(job)
        if text or job.ui_shim is None:
            return text

        job.state = STATE_AWAITING
        logger.info(f'_recover_with_patience [waiting]: {job.delivery_id} the transport gave '
                    f'up but {job.target_agent} may still be working; watching its transcript')

        deadline = time.time() + RECOVERY_WINDOW_SECONDS
        while time.time() < deadline:
            time.sleep(RECOVERY_POLL_SECONDS)
            text = self._recover(job)
            if text:
                logger.info(f'_recover_with_patience [answered]: {job.delivery_id} after '
                            f'{round(time.time() - (job.started_at or time.time()))}s')
                return text
        logger.info(f'_recover_with_patience [gave up]: {job.delivery_id} no answer within '
                    f'{RECOVERY_WINDOW_SECONDS}s of the transport failing')
        return None

    def _recover(self, job: Job) -> Optional[str]:
        if self.recover is None:
            return None
        try:
            return self.recover(job)
        except Exception as e:
            logger.error(f'_recover [exception]: {job.delivery_id} {e}')
            return None

    def _send_reply(self, job: Job, reply: str) -> None:
        if self.build_reply is None:
            return
        try:
            reply_job = self.build_reply(job, reply)
        except Exception as e:
            logger.error(f'_send_reply [exception]: {job.delivery_id} {e}')
            return
        if reply_job is not None:
            self.submit(reply_job)

    # --------------------------------------------------------------- reporting

    def _archive(self, job: Job) -> None:
        with self._guard:
            self._pending.pop(job.delivery_id, None)
            self._history.append(job)
            del self._history[:-HISTORY_LIMIT]
        _write_record(job.describe())

    def find(self, delivery_id: str) -> Optional[Job]:
        with self._guard:
            job = self._pending.get(delivery_id)
            if job:
                return job
            return next((j for j in reversed(self._history)
                         if j.delivery_id == delivery_id), None)

    def snapshot(self, limit: int = 20) -> Dict[str, Any]:
        with self._guard:
            pending = [j.describe() for j in self._pending.values()]
            recent = [j.describe() for j in reversed(self._history[-limit:])]
        pending.sort(key=lambda d: d['queued_seconds'], reverse=True)

        # Deliveries this process carried are in memory; earlier ones survive on disk, which
        # is how an answer outlives the server that received it.
        seen = {d['delivery_id'] for d in recent}
        earlier = [r for r in read_records(limit) if r['delivery_id'] not in seen]
        return {'pending': pending, 'recent': recent, 'earlier': earlier}


OUTBOX = Outbox()
