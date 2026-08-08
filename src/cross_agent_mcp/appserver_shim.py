"""Transparent stdio shim between the VS Code Codex extension and `codex app-server`.

The extension launches Codex as `codex ... app-server ...` and speaks newline-delimited
JSON-RPC to it over the child's stdio. That connection is the only way into the session the
user actually sees, so the bridge borrows it: this shim sits in the middle, forwards every
byte untouched, and exposes a side socket where the bridge can hand in a message. The message
is written into the same stream as a `turn/start` request, so the app-server answers it like
any other turn and the extension renders it in the panel.

Point the extension at this shim with the `chatgpt.cliExecutable` setting.

Failure is always fail-open: anything the shim cannot parse or handle is still forwarded, and
if the side channel breaks the extension keeps working as if the shim were not there.
"""

import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from . import config
from .panel import PanelShim


# our injected requests use string ids in a private namespace, so they can never collide
# with the integer ids the extension hands out
INJECT_ID_PREFIX = 'xagent-'

# `codex app-server` has sub-subcommands that are not the stdio server; leave those alone
APP_SERVER_SUBCOMMANDS = {'daemon', 'proxy', 'generate-ts', 'generate-json-schema', 'help'}


def find_real_codex() -> Optional[str]:
    """Locate the genuine Codex binary this shim should wrap."""
    override = os.environ.get('CROSS_AGENT_REAL_CODEX')
    if override and os.path.exists(override):
        return override

    # the VS Code extension ships its own build; prefer the newest one installed
    candidates = glob.glob(os.path.expanduser('~/.vscode/extensions/openai.chatgpt-*/bin/*/codex'))
    candidates += glob.glob(os.path.expanduser('~/.vscode-insiders/extensions/openai.chatgpt-*/bin/*/codex'))
    if candidates:
        return sorted(candidates)[-1]

    found = shutil.which('codex')
    return found if found and os.path.realpath(found) != os.path.realpath(sys.argv[0]) else None


def is_app_server_invocation(argv: List[str]) -> bool:
    """True only for the plain stdio app-server the extension talks to."""
    if 'app-server' not in argv or '--help' in argv or '-h' in argv:
        return False

    index = argv.index('app-server')
    for token in argv[index + 1:]:
        if token.startswith('-'):
            continue
        return token not in APP_SERVER_SUBCOMMANDS
    return True


class Injection:
    """One bridged message waiting for the app-server to finish its turn."""

    def __init__(self, thread_id: str, request_id: str) -> None:
        self.thread_id = thread_id
        self.request_id = request_id
        self.turn_id: Optional[str] = None
        self.messages: List[str] = []
        self.error: Optional[str] = None
        self.done = threading.Event()


class CodexAppServerShim(PanelShim):

    agent = config.AGENT_CODEX

    def __init__(self, argv: List[str], real_codex: str) -> None:
        super().__init__([real_codex] + argv, argv)
        self.real_codex = real_codex
        self.threads: Dict[str, Dict[str, Any]] = {}
        self.injections: Dict[str, Injection] = {}
        # only turns the extension itself starts count as the human being here; injected
        # turns are written straight to the child and never pass through the observer
        self.last_user_activity = 0.0

    # ------------------------------------------------------------ observation

    def _note_thread(self, thread_id: Optional[str], cwd: Optional[str] = None) -> None:
        if not thread_id or not isinstance(thread_id, str):
            return
        with self.state_lock:
            record = self.threads.setdefault(thread_id, {'thread_id': thread_id, 'cwd': None})
            record['last_seen'] = time.time()
            if cwd:
                record['cwd'] = cwd

    def _observe_from_client(self, message: Dict[str, Any]) -> None:
        """Learn which threads the panel is driving."""
        params = message.get('params')
        if not isinstance(params, dict):
            return

        if message.get('method') in ('turn/start', 'turn/steer'):
            with self.state_lock:
                self.last_user_activity = time.time()
                thread_id = params.get('threadId')
                if isinstance(thread_id, str) and thread_id in self.threads:
                    self.threads[thread_id]['last_user_activity'] = self.last_user_activity

        cwd = params.get('cwd') if isinstance(params.get('cwd'), str) else None
        self._note_thread(params.get('threadId'), cwd)

        thread = params.get('thread')
        if isinstance(thread, dict):
            self._note_thread(thread.get('id'), cwd)

    def _observe_from_server(self, message: Dict[str, Any]) -> bool:
        """Feed a server message to any waiting injection. Returns True to swallow it."""
        message_id = message.get('id')
        if isinstance(message_id, str) and message_id.startswith(INJECT_ID_PREFIX):
            with self.state_lock:
                injection = self.injections.get(message_id)
            if injection:
                if 'error' in message:
                    injection.error = json.dumps(message['error'], ensure_ascii=False)[:500]
                    injection.done.set()
                result = message.get('result')
                if isinstance(result, dict) and isinstance(result.get('turnId'), str):
                    injection.turn_id = result['turnId']
            # never hand the extension a response to a request it never sent
            return True

        method = message.get('method')
        params = message.get('params') if isinstance(message.get('params'), dict) else {}

        if method == 'thread/started':
            thread = params.get('thread')
            if isinstance(thread, dict):
                self._note_thread(thread.get('id'), thread.get('cwd'))
        elif isinstance(params.get('threadId'), str):
            self._note_thread(params['threadId'])

        with self.state_lock:
            waiting = [i for i in self.injections.values() if not i.done.is_set()]
        for injection in waiting:
            self._apply_to_injection(injection, method, params)
        return False

    def _apply_to_injection(self, injection: Injection, method: Optional[str],
                            params: Dict[str, Any]) -> None:
        turn = params.get('turn') if isinstance(params.get('turn'), dict) else {}

        if method == 'turn/started' and injection.turn_id is None:
            if params.get('threadId') == injection.thread_id and isinstance(turn.get('id'), str):
                injection.turn_id = turn['id']
            return

        if not injection.turn_id:
            return

        if method == 'item/completed' and params.get('turnId') == injection.turn_id:
            item = params.get('item')
            if isinstance(item, dict) and item.get('type') == 'agentMessage' and item.get('text'):
                injection.messages.append(str(item['text']))
        elif method == 'turn/completed' and turn.get('id') == injection.turn_id:
            if turn.get('status') == 'failed' and turn.get('error'):
                injection.error = json.dumps(turn['error'], ensure_ascii=False)[:500]
            injection.done.set()

    # --------------------------------------------------------------- plumbing

    def _pump_client_to_server(self) -> None:
        """Extension -> app-server. Forwarding never depends on parsing succeeding."""
        try:
            for line in sys.stdin:
                try:
                    self._observe_from_client(json.loads(line))
                except Exception:
                    pass
                self.write_to_child(line)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                assert self.process and self.process.stdin
                self.process.stdin.close()

    def _pump_server_to_client(self) -> None:
        """app-server -> extension, minus the responses to our own injected requests."""
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                is_swallowed = False
                try:
                    is_swallowed = self._observe_from_server(json.loads(line))
                except Exception:
                    pass
                if not is_swallowed:
                    sys.stdout.write(line)
                    sys.stdout.flush()
        except Exception:
            pass

    # -------------------------------------------------------- side channel ops

    def status(self) -> Dict[str, Any]:
        with self.state_lock:
            threads = sorted(self.threads.values(),
                             key=lambda t: t.get('last_user_activity', t.get('last_seen', 0)),
                             reverse=True)
            for thread in threads:
                thread.setdefault('session_id', thread['thread_id'])
            activity = self.last_user_activity
        return {'ok': True, 'agent': self.agent, 'pid': os.getpid(), 'threads': threads,
                'sessions': threads, 'last_user_activity': activity,
                'argv': self.argv, 'real_binary': self.real_codex}

    def _pick_thread(self, thread_id: Optional[str]) -> Optional[str]:
        if thread_id:
            return thread_id
        with self.state_lock:
            if not self.threads:
                return None
            newest = max(self.threads.values(), key=lambda t: t.get('last_seen', 0))
        return newest['thread_id']

    def inject(self, text: str, session_id: Optional[str], timeout: int) -> Dict[str, Any]:
        target = self._pick_thread(session_id)
        if not target:
            return {'ok': False, 'error': 'no live thread seen on this app-server yet'}

        request_id = INJECT_ID_PREFIX + uuid.uuid4().hex[:12]
        injection = Injection(target, request_id)
        with self.state_lock:
            self.injections[request_id] = injection

        payload = json.dumps({
            'id': request_id,
            'method': 'turn/start',
            'params': {
                'threadId': target,
                'input': [{'type': 'text', 'text': text}],
                'clientUserMessageId': request_id,
            },
        }, ensure_ascii=False) + '\n'

        try:
            self.write_to_child(payload)
        except Exception as e:
            with self.state_lock:
                self.injections.pop(request_id, None)
            return {'ok': False, 'error': f'failed to write to app-server: {e}'}

        is_finished = injection.done.wait(timeout=timeout)
        with self.state_lock:
            self.injections.pop(request_id, None)

        if injection.error:
            return {'ok': False, 'error': injection.error, 'sessionId': target}
        if not is_finished:
            return {'ok': False, 'error': f'turn did not complete within {timeout}s',
                    'sessionId': target, 'turnId': injection.turn_id,
                    'partial': '\n'.join(injection.messages)}

        return {
            'ok': True,
            'sessionId': target,
            'threadId': target,
            'turnId': injection.turn_id,
            'reply': injection.messages[-1] if injection.messages else '',
        }

    # -------------------------------------------------------------------- run

    def run(self) -> int:
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            text=True, bufsize=1,
        )
        self.start_side_channel()

        reader = threading.Thread(target=self._pump_server_to_client, daemon=True)
        reader.start()
        self._pump_client_to_server()

        code = self.process.wait()
        reader.join(timeout=2)
        self.unregister()
        return code


def main() -> int:
    argv = sys.argv[1:]
    real_codex = find_real_codex()

    if not real_codex:
        print('cross-agent shim: could not locate the real codex binary; '
              'set CROSS_AGENT_REAL_CODEX', file=sys.stderr)
        return 127

    if not is_app_server_invocation(argv):
        os.execv(real_codex, [real_codex] + argv)

    try:
        return CodexAppServerShim(argv, real_codex).run()
    except Exception as e:
        # last-resort fail-open: behave exactly like the real binary
        print(f'cross-agent shim: falling back to direct exec ({e})', file=sys.stderr)
        os.execv(real_codex, [real_codex] + argv)
        return 1


if __name__ == '__main__':
    sys.exit(main())
