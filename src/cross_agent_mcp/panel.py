"""Shared plumbing for the editor-panel shims.

Both agents run their live session inside a process the VS Code extension spawns and talks to
over stdio. A shim sits in that pipe, forwards every byte, and opens a side socket so the
bridge can hand a message to the session the user is actually looking at.

This module holds the parts that do not depend on which agent is being wrapped: the registry
that lets the bridge find the shim belonging to its own editor window, and the socket server.
"""

import contextlib
import json
import logging
import os
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from . import config


logger = logging.getLogger('cross_agent_mcp.panel')

REGISTRY_DIR: str = config.HOME_DIR + 'panels/'

SOCKET_BACKLOG = 4
MAX_REQUEST_BYTES = 4_000_000
DEFAULT_INJECT_TIMEOUT = 600


def process_ancestry(pid: int, depth: int = 12) -> List[int]:
    """Parent pid chain, used to tell which editor window a process belongs to."""
    chain: List[int] = []
    current = pid
    for _ in range(depth):
        if current <= 1:
            break
        try:
            completed = subprocess.run(['ps', '-o', 'ppid=', '-p', str(current)],
                                       capture_output=True, text=True, timeout=5)
            parent = int(completed.stdout.strip())
        except Exception:
            break
        chain.append(parent)
        current = parent
    return chain


def split_wrapper_argv(argv: List[str]) -> tuple:
    """Separate the wrapped executable from the arguments meant for it.

    A wrapper is invoked as `<wrapper> <real-binary> <args...>`, and sometimes as
    `<wrapper> <node> <cli.js> <args...>` when the extension falls back to the JS entry point.
    """
    if not argv:
        return [], []

    first = argv[0]
    if os.path.isfile(first) and os.access(first, os.X_OK):
        if len(argv) > 1 and argv[1].endswith('.js') and os.path.isfile(argv[1]):
            return [first, argv[1]], argv[2:]
        return [first], argv[1:]
    return [], argv


class PanelShim:
    """Base for a shim that wraps one live agent process."""

    agent: str = ''

    def __init__(self, command: List[str], argv: List[str]) -> None:
        self.command = command
        self.argv = argv
        self.process: Optional[subprocess.Popen] = None
        self.stdin_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.socket_path = REGISTRY_DIR + f'{self.agent}-{os.getpid()}.sock'
        self.registry_path = REGISTRY_DIR + f'{self.agent}-{os.getpid()}.json'

    # ------------------------------------------------------------- subclasses

    def status(self) -> Dict[str, Any]:
        raise NotImplementedError

    def inject(self, text: str, session_id: Optional[str], timeout: int) -> Dict[str, Any]:
        raise NotImplementedError

    # --------------------------------------------------------------- registry

    def register(self) -> None:
        os.makedirs(REGISTRY_DIR, exist_ok=True)
        record = {
            'agent': self.agent,
            'pid': os.getpid(),
            'socket': self.socket_path,
            'ancestors': process_ancestry(os.getpid()),
            'started_at': time.time(),
            'argv': self.argv,
        }
        with open(self.registry_path, 'w', encoding='utf-8') as f:
            json.dump(record, f)

    def unregister(self) -> None:
        for path in (self.registry_path, self.socket_path):
            with contextlib.suppress(OSError):
                os.remove(path)

    # ----------------------------------------------------------- side channel

    def write_to_child(self, payload: str) -> None:
        with self.stdin_lock:
            assert self.process and self.process.stdin
            self.process.stdin.write(payload)
            self.process.stdin.flush()

    def _handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        operation = request.get('op')
        if operation == 'status':
            return self.status()
        if operation == 'send':
            return self.inject(
                str(request.get('text') or ''),
                request.get('sessionId') or request.get('threadId'),
                int(request.get('timeout') or DEFAULT_INJECT_TIMEOUT),
            )
        return {'ok': False, 'error': f'unknown op: {operation}'}

    def _serve_client(self, connection: socket.socket) -> None:
        try:
            buffer = b''
            while b'\n' not in buffer:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_REQUEST_BYTES:
                    return
            response = self._handle_request(json.loads(buffer.split(b'\n', 1)[0].decode('utf-8')))
        except Exception as e:
            response = {'ok': False, 'error': f'{type(e).__name__}: {e}'}

        with contextlib.suppress(Exception):
            connection.sendall((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        with contextlib.suppress(Exception):
            connection.close()

    def serve_socket(self) -> None:
        with contextlib.suppress(OSError):
            os.remove(self.socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        server.listen(SOCKET_BACKLOG)

        while True:
            try:
                connection, _ = server.accept()
            except Exception:
                return
            threading.Thread(target=self._serve_client, args=(connection,), daemon=True).start()

    def start_side_channel(self) -> None:
        """The side channel must never be able to take the passthrough down with it."""
        try:
            self.register()
            threading.Thread(target=self.serve_socket, daemon=True).start()
        except Exception as e:
            print(f'cross-agent shim: side channel disabled ({e})', file=__import__('sys').stderr)
