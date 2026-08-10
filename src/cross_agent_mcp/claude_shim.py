"""Transparent stdio shim between the Claude Code VS Code extension and the Claude CLI.

The extension runs the panel session as

    claude --input-format stream-json --output-format stream-json --resume=<session-id> ...

and feeds user turns in as newline-delimited stream-json on stdin. That is exactly the shape
a bridged message needs, so this shim forwards every byte and writes an extra user message
into the same pipe when the bridge asks it to. The CLI answers on the shared stdout, so the
reply lands in the panel.

Point the extension at this shim with the `claudeCode.claudeProcessWrapper` setting. The
extension invokes a wrapper as `<wrapper> <real-claude-binary> <args...>`.

Failure is always fail-open: unparsable traffic is still forwarded, and any setup problem
degrades to exec'ing the real binary.
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
from typing import Any, Dict, List, Optional

from . import config
from .panel import PanelShim, split_wrapper_argv


# how long an injection waits for a user-initiated turn to finish before giving up
IDLE_WAIT_SECONDS = 120
IDLE_POLL_SECONDS = 0.2


def find_real_claude() -> Optional[str]:
    override = os.environ.get('CROSS_AGENT_REAL_CLAUDE')
    if override and os.path.exists(override):
        return override

    candidates = glob.glob(os.path.expanduser(
        '~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude'))
    candidates += glob.glob(os.path.expanduser(
        '~/.vscode-insiders/extensions/anthropic.claude-code-*/resources/native-binary/claude'))
    if candidates:
        return sorted(candidates)[-1]

    found = shutil.which('claude')
    return found if found and os.path.realpath(found) != os.path.realpath(sys.argv[0]) else None


def is_panel_invocation(args: List[str]) -> bool:
    """True only for the stream-json session the extension drives the panel with."""
    joined = ' '.join(args)
    return '--input-format' in joined and 'stream-json' in joined


def session_id_from_args(args: List[str]) -> Optional[str]:
    for index, token in enumerate(args):
        if token.startswith('--resume='):
            return token.split('=', 1)[1] or None
        if token == '--resume' and index + 1 < len(args):
            return args[index + 1]
    return None


class Injection:
    """One bridged message waiting for the CLI to finish its turn."""

    def __init__(self) -> None:
        self.messages: List[str] = []
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.done = threading.Event()


class ClaudeStreamShim(PanelShim):

    agent = config.AGENT_CLAUDE

    def __init__(self, command: List[str], args: List[str]) -> None:
        super().__init__(command + args, args)
        self.real_binary = command[0] if command else ''
        self.session_id: Optional[str] = session_id_from_args(args)
        self.cwd: str = os.getcwd()
        self.is_turn_active = False
        self.injection: Optional[Injection] = None
        self.last_seen = time.time()
        # only the human typing in the panel updates this; injected turns must not, or the
        # bridge would keep reinforcing whichever tab it last wrote to
        self.last_user_activity = 0.0

    # ------------------------------------------------------------ observation

    def _observe_from_client(self, message: Dict[str, Any]) -> None:
        """A user turn from the panel: nothing may be injected until it finishes."""
        if message.get('type') == 'user':
            with self.state_lock:
                self.is_turn_active = True
                self.last_seen = time.time()
                self.last_user_activity = self.last_seen

    def _observe_from_agent(self, message: Dict[str, Any]) -> None:
        kind = message.get('type')

        if isinstance(message.get('session_id'), str):
            with self.state_lock:
                self.session_id = message['session_id']
                self.last_seen = time.time()
        if isinstance(message.get('cwd'), str):
            self.cwd = message['cwd']

        with self.state_lock:
            injection = self.injection

        if kind == 'assistant' and injection and not injection.done.is_set():
            text = _text_of(message.get('message'))
            if text:
                injection.messages.append(text)
        elif kind == 'result':
            with self.state_lock:
                self.is_turn_active = False
            if injection and not injection.done.is_set():
                if message.get('is_error'):
                    injection.error = str(message.get('result') or 'claude reported an error')[:500]
                injection.result = str(message.get('result') or '')
                injection.done.set()

    # --------------------------------------------------------------- plumbing

    def _pump_client_to_agent(self) -> None:
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

    def _pump_agent_to_client(self) -> None:
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                try:
                    self._observe_from_agent(json.loads(line))
                except Exception:
                    pass
                sys.stdout.write(line)
                sys.stdout.flush()
        except Exception:
            pass

    # -------------------------------------------------------- side channel ops

    def status(self) -> Dict[str, Any]:
        with self.state_lock:
            sessions = ([{'session_id': self.session_id, 'thread_id': self.session_id,
                          'cwd': self.cwd, 'last_seen': self.last_seen,
                          'last_user_activity': self.last_user_activity,
                          'is_turn_active': self.is_turn_active}]
                        if self.session_id else [])
            activity = self.last_user_activity
        return {'ok': True, 'agent': self.agent, 'pid': os.getpid(), 'sessions': sessions,
                'threads': sessions, 'last_user_activity': activity,
                'argv': self.argv, 'real_binary': self.real_binary}

    def _wait_for_idle(self, deadline: float) -> bool:
        while time.time() < deadline:
            with self.state_lock:
                if not self.is_turn_active and self.injection is None:
                    return True
            time.sleep(IDLE_POLL_SECONDS)
        return False

    def inject(self, text: str, session_id: Optional[str], timeout: int,
               cwd: Optional[str] = None, title: Optional[str] = None) -> Dict[str, Any]:
        with self.state_lock:
            current = self.session_id
        if session_id and session_id != current:
            return {'ok': False,
                    'error': f'this panel drives session {current}, not {session_id}'}

        # A panel sitting on its conversation list has a process but no conversation yet.
        # Writing the message anyway makes the CLI open one, and the panel renders it.
        is_created = current is None

        # the CLI serialises turns; injecting mid-turn would make us collect the wrong reply
        if not self._wait_for_idle(time.time() + min(IDLE_WAIT_SECONDS, timeout)):
            return {'ok': False, 'error': 'the panel session is busy with another turn'}

        injection = Injection()
        with self.state_lock:
            if self.injection is not None:
                return {'ok': False, 'error': 'another bridged message is already in flight'}
            self.injection = injection
            self.is_turn_active = True

        payload = json.dumps({
            'type': 'user',
            'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]},
        }, ensure_ascii=False) + '\n'

        try:
            self.write_to_child(payload)
        except Exception as e:
            with self.state_lock:
                self.injection = None
                self.is_turn_active = False
            return {'ok': False, 'error': f'failed to write to the claude process: {e}'}

        is_finished = injection.done.wait(timeout=timeout)
        with self.state_lock:
            self.injection = None
            session = self.session_id

        if injection.error:
            return {'ok': False, 'error': injection.error,
                    'sessionId': session, 'wasCreated': is_created}
        if not is_finished:
            return {'ok': False, 'error': f'turn did not complete within {timeout}s',
                    'sessionId': session, 'wasCreated': is_created,
                    'partial': '\n'.join(injection.messages)}

        reply = injection.result or (injection.messages[-1] if injection.messages else '')
        return {'ok': True, 'sessionId': session, 'threadId': session,
                'wasCreated': is_created, 'reply': reply}

    # -------------------------------------------------------------------- run

    def run(self) -> int:
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            text=True, bufsize=1,
        )
        self.start_side_channel()

        reader = threading.Thread(target=self._pump_agent_to_client, daemon=True)
        reader.start()
        self._pump_client_to_agent()

        code = self.process.wait()
        reader.join(timeout=2)
        self.unregister()
        return code


def _text_of(message: Any) -> str:
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get('text', '') for b in content
                 if isinstance(b, dict) and b.get('type') == 'text']
        return ' '.join(p for p in parts if p).strip()
    return ''


def main() -> int:
    argv = sys.argv[1:]
    command, args = split_wrapper_argv(argv)

    if not command:
        found = find_real_claude()
        if not found:
            print('cross-agent shim: could not locate the real claude binary; '
                  'set CROSS_AGENT_REAL_CLAUDE', file=sys.stderr)
            return 127
        command = [found]

    if not is_panel_invocation(args):
        os.execv(command[0], command + args)

    try:
        return ClaudeStreamShim(command, args).run()
    except Exception as e:
        print(f'cross-agent shim: falling back to direct exec ({e})', file=sys.stderr)
        os.execv(command[0], command + args)
        return 1


if __name__ == '__main__':
    sys.exit(main())
