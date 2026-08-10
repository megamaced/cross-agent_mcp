"""Prove the app-server shim delivers into a live panel session.

    PYTHONPATH=src .venv/bin/python tests/shim_roundtrip.py

This impersonates the VS Code extension: it launches `codex-shim.sh ... app-server`, starts a
thread over stdio exactly like the panel does, then asks the shim's side socket to inject a
message. The checks that matter are that the injected turn's events reach the *extension*
stream - that is what makes the exchange visible in the real panel - and that the extension
never sees a response to a request it did not send.

Spends one real Codex turn. Touches no VS Code settings.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR + '/src')

from cross_agent_mcp import uihook  # noqa: E402


FAILURES = []


def check(label: str, condition: bool, detail: str = '') -> None:
    if condition:
        print(f'[ok] {label}')
    else:
        print(f'[FAIL] {label} {detail}')
        FAILURES.append(label)


class FakeExtension:
    """Drives the shim over stdio the way the Codex VS Code extension does."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [ROOT_DIR + '/codex-shim.sh', '-c', 'features.code_mode_host=true', 'app-server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=ROOT_DIR,
        )
        self.inbox = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except Exception:
                continue
            with self.lock:
                self.inbox.append(message)

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + '\n')
        self.proc.stdin.flush()

    def call(self, request_id, method, params, wait=30.0):
        self.send({'id': request_id, 'method': method, 'params': params})
        deadline = time.time() + wait
        while time.time() < deadline:
            with self.lock:
                for m in self.inbox:
                    if m.get('id') == request_id and ('result' in m or 'error' in m):
                        return m
            time.sleep(0.05)
        return None

    def notifications(self, method: str) -> list:
        with self.lock:
            return [m for m in self.inbox if m.get('method') == method]

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def socket_request(path: str, payload: dict, timeout: float) -> dict:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(path)
        connection.sendall((json.dumps(payload) + '\n').encode('utf-8'))
        buffer = b''
        while b'\n' not in buffer:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
        return json.loads(buffer.split(b'\n', 1)[0].decode('utf-8'))
    finally:
        connection.close()


def main() -> int:
    extension = FakeExtension()
    try:
        init = extension.call(1, 'initialize', {
            'clientInfo': {'name': 'fake-vscode-extension', 'version': '0.1.0'},
            'capabilities': {'experimentalApi': True},
        })
        check('shim forwards initialize', bool(init and 'result' in init), str(init)[:200])
        extension.send({'method': 'initialized', 'params': {}})

        started = extension.call(2, 'thread/start', {'cwd': ROOT_DIR, 'sandbox': 'read-only'})
        thread_id = ((started or {}).get('result') or {}).get('thread', {}).get('id')
        check('shim forwards thread/start', bool(thread_id), str(started)[:250])
        if not thread_id:
            return 1
        print(f'       panel thread: {thread_id}')

        time.sleep(0.5)
        shims = [s for s in uihook.list_shims('codex') if s['pid'] == extension.proc.pid]
        check('shim registered itself', bool(shims), str(uihook.list_shims('codex'))[:200])
        if not shims:
            return 1
        socket_path = shims[0]['socket']

        status = socket_request(socket_path, {'op': 'status'}, 5)
        seen = [t['thread_id'] for t in status.get('sessions', [])]
        check('shim observed the panel thread', thread_id in seen, str(seen))

        before = len(extension.notifications('item/completed'))
        response = socket_request(
            socket_path,
            {'op': 'send', 'text': 'Reply with exactly: PANEL_OK', 'timeout': 240},
            260)
        print(f'       inject -> {json.dumps(response, ensure_ascii=False)[:220]}')
        check('injected turn returned a reply',
              response.get('ok') and 'PANEL_OK' in (response.get('reply') or ''), str(response)[:250])
        check('reply came from the panel thread', response.get('threadId') == thread_id)

        completed = extension.notifications('item/completed')
        agent_items = [m for m in completed[before:]
                       if (m.get('params', {}).get('item') or {}).get('type') == 'agentMessage']
        check('extension stream received the assistant item (panel would render it)',
              bool(agent_items), f'{len(completed) - before} new item/completed')

        user_items = [m for m in extension.notifications('item/started')
                      if (m.get('params', {}).get('item') or {}).get('type') == 'userMessage']
        check('extension stream received the injected user message',
              bool(user_items), f'{len(user_items)} userMessage items')

        with extension.lock:
            leaked = [m for m in extension.inbox
                      if isinstance(m.get('id'), str) and m['id'].startswith('xagent-')]
        check('injected request id never leaks to the extension', not leaked, str(leaked)[:200])

        check('turn/completed reached the extension',
              bool(extension.notifications('turn/completed')))
    finally:
        extension.stop()

    check_idle_panel_opens_a_thread()

    print('\n' + ('ALL SHIM CHECKS PASSED' if not FAILURES else f'{len(FAILURES)} CHECK(S) FAILED'))
    return 1 if FAILURES else 0


def check_idle_panel_opens_a_thread() -> None:
    """A panel showing only its chat list must still get a visible conversation."""
    print('\n--- panel with no open thread ---')
    extension = FakeExtension()
    try:
        init = extension.call(1, 'initialize', {
            'clientInfo': {'name': 'fake-vscode-extension', 'version': '0.1.0'},
            'capabilities': {'experimentalApi': True},
        })
        check('idle panel: shim forwards initialize', bool(init and 'result' in init))
        extension.send({'method': 'initialized', 'params': {}})
        time.sleep(0.5)

        shims = [s for s in uihook.list_shims('codex') if s['pid'] == extension.proc.pid]
        if not shims:
            check('idle panel: shim registered itself', False)
            return
        socket_path = shims[0]['socket']

        status = socket_request(socket_path, {'op': 'status'}, 5)
        check('idle panel: no thread is open yet', not status.get('sessions'), str(status)[:150])

        response = socket_request(
            socket_path,
            {'op': 'send', 'text': 'Reply with exactly: OPENED_OK',
             'cwd': ROOT_DIR, 'title': 'Claude Code: OPENED_OK check', 'timeout': 240},
            260)
        print(f'       inject -> {json.dumps(response, ensure_ascii=False)[:220]}')
        check('idle panel: a thread was opened for the message',
              response.get('ok') and response.get('wasCreated') is True, str(response)[:250])
        check('idle panel: the reply came back',
              'OPENED_OK' in (response.get('reply') or ''), str(response.get('reply'))[:120])

        started = extension.notifications('thread/started')
        check('idle panel: extension was told about the new thread (panel renders it)',
              any((m.get('params', {}).get('thread') or {}).get('id') == response.get('threadId')
                  for m in started),
              f'{len(started)} thread/started notifications')

        named = extension.notifications('thread/name/updated')
        check('idle panel: the new thread got a recognisable title',
              any('OPENED_OK check' in json.dumps(m.get('params'), ensure_ascii=False)
                  for m in named),
              json.dumps([m.get('params') for m in named], ensure_ascii=False)[:200])
    finally:
        extension.stop()


if __name__ == '__main__':
    sys.exit(main())
