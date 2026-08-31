"""Live end-to-end check: relay real messages through the MCP server.

    PYTHONPATH=src .venv/bin/python tests/live_roundtrip.py

This spends real agent turns. Five scenarios are covered:

  1. resume        - an active Codex thread answers with its earlier context intact
  2. create        - a directory with no Codex thread gets a fresh one created automatically
  3. reuse         - the freshly created thread is resumed on the next call, not recreated
  4. claude create - a directory with no Claude session gets a fresh one created
  5. claude reuse  - that Claude session is resumed and still remembers its own context

Scenarios 4 and 5 target a scratch directory on purpose: relaying into the Claude
session that is running this test would make it talk to itself.

Sending is asynchronous, so a scenario is two steps: the tool call only queues the message,
and the answer is read afterwards from the delivery record in `bridge_status`. A real agent
gets the answer delivered into its session instead - this harness has no session of its own,
which is exactly the case `reply_preview` exists for.
"""

import asyncio
import json
import os
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# a peer turn can legitimately run for many minutes; this only bounds the harness
AWAIT_BUDGET_SECONDS = 600
POLL_SECONDS = 3


def _text_of(result) -> str:
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            return block.text
    return ''


async def _call(session: ClientSession, tool: str, args: dict) -> dict:
    return json.loads(_text_of(await session.call_tool(tool, args)))


def _report(label: str, payload: dict) -> bool:
    """Print the receipt the send returned. It never carries the answer."""
    if not payload.get('ok'):
        print(f'[FAIL] {label}: {payload.get("error")}')
        return False
    print(f'[ok] {label} queued')
    print(f'       delivery : {payload["delivery_id"]}')
    print(f'       target   : {payload["target_session_id"] or "(new session)"}')
    print(f'       origin   : {payload["session_origin"]} '
          f'(will_create={payload["will_create_session"]})')
    print(f'       hop      : {payload["hop"]}/{payload["hop"] + payload["hops_remaining"]}'
          f'  conv={payload["conversation_id"]}')
    return True


async def _await_delivery(session: ClientSession, delivery_id: str, cwd: str) -> dict:
    """Poll until the outbox finishes carrying this message."""
    deadline = time.time() + AWAIT_BUDGET_SECONDS
    while time.time() < deadline:
        status = await _call(session, 'bridge_status', {'cwd': cwd})
        deliveries = status.get('deliveries') or {}
        for record in deliveries.get('recent') or []:
            if record.get('delivery_id') == delivery_id:
                return record
        await asyncio.sleep(POLL_SECONDS)
    return {'delivery_id': delivery_id, 'state': 'harness-timeout', 'reply_preview': ''}


def _settled(label: str, record: dict) -> bool:
    print(f'       state    : {record["state"]} after {record.get("elapsed_seconds")}s')
    print(f'       reply    : {(record.get("reply_preview") or "")[:400]}')
    if record['state'] != 'delivered':
        print(f'[FAIL] {label} was not delivered: {record.get("error") or record["state"]}')
        return False
    return True


async def _round_trip(session: ClientSession, label: str, tool: str, args: dict) -> dict:
    """Queue one message and return its finished delivery record ({} when the send failed)."""
    receipt = await _call(session, tool, args)
    if not _report(label, receipt):
        return {}
    record = await _await_delivery(session, receipt['delivery_id'], args['cwd'])
    return record if _settled(label, record) else {}


async def main() -> int:
    params = StdioServerParameters(
        command=ROOT_DIR + '/.venv/bin/python',
        args=['-m', 'cross_agent_mcp'],
        env={**os.environ, 'PYTHONPATH': ROOT_DIR + '/src'},
        cwd=ROOT_DIR,
    )
    failures = 0

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print('--- 1. resume the active Codex thread in this project ---')
            # seed first so the check works whether or not a live thread already exists here
            seeded = await _round_trip(session, 'seed', 'send_to_codex', {
                'message': 'Remember this token for later: PROBE_OK. Reply with exactly: SEEDED',
                'cwd': ROOT_DIR, 'scope': 'cwd', 'timeout': 300,
            })
            if not seeded:
                failures += 1

            resumed = await _round_trip(session, 'resume', 'send_to_codex', {
                'message': ('Two questions. (a) Earlier in this thread I gave you a token to '
                            'remember - repeat it. (b) Repeat the conversation id shown in the '
                            'bridge header of this message. Answer in two short lines.'),
                'cwd': ROOT_DIR, 'scope': 'cwd', 'timeout': 300,
            })
            if not resumed:
                failures += 1
            elif resumed['target_session_id'] != seeded.get('target_session_id'):
                print('[FAIL] follow-up did not land on the seeded thread')
                failures += 1
            elif 'PROBE_OK' not in (resumed.get('reply_preview') or ''):
                print('[FAIL] resumed thread lost its earlier context')
                failures += 1

            with tempfile.TemporaryDirectory(prefix='cross-agent-live-') as tmp_dir:
                print(f'\n--- 2. no active Codex thread in {tmp_dir} -> create one ---')
                created = await _round_trip(session, 'create', 'send_to_codex', {
                    'message': 'Reply with exactly: FRESH_THREAD_OK',
                    'cwd': tmp_dir, 'scope': 'cwd', 'timeout': 300,
                })
                if not created:
                    failures += 1

                print('\n--- 3. call again in the same directory -> reuse that thread ---')
                reused = await _round_trip(session, 'reuse', 'send_to_codex', {
                    'message': ('What exact word did I ask you to reply with in my previous '
                                'message? Answer with that word only.'),
                    'cwd': tmp_dir, 'scope': 'cwd', 'timeout': 300,
                })
                if not reused:
                    failures += 1
                elif reused['target_session_id'] != created.get('target_session_id'):
                    print('[FAIL] second call did not reuse the created thread')
                    failures += 1
                elif 'FRESH_THREAD_OK' not in (reused.get('reply_preview') or ''):
                    print('[FAIL] reused thread lost its earlier context')
                    failures += 1

            with tempfile.TemporaryDirectory(prefix='cross-agent-claude-') as tmp_dir:
                print(f'\n--- 4. no active Claude session in {tmp_dir} -> create one ---')
                born = await _round_trip(session, 'claude create', 'send_to_claude', {
                    'message': 'Reply with exactly: FRESH_CLAUDE_OK',
                    'cwd': tmp_dir, 'scope': 'cwd', 'timeout': 300,
                    'allow_same_agent': True,
                })
                if not born:
                    failures += 1

                print('\n--- 5. call again in the same directory -> reuse that session ---')
                kept = await _round_trip(session, 'claude reuse', 'send_to_claude', {
                    'message': ('What exact word did I ask you to reply with in my previous '
                                'message? Answer with that word only.'),
                    'cwd': tmp_dir, 'scope': 'cwd', 'timeout': 300,
                    'allow_same_agent': True,
                })
                if not kept:
                    failures += 1
                elif kept['target_session_id'] != born.get('target_session_id'):
                    print('[FAIL] second call did not reuse the created session')
                    failures += 1
                elif 'FRESH_CLAUDE_OK' not in (kept.get('reply_preview') or ''):
                    print('[FAIL] resumed Claude session lost its earlier context')
                    failures += 1

    print('\nALL LIVE CHECKS PASSED' if not failures else f'\n{failures} CHECK(S) FAILED')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
