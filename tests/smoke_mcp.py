"""Smoke test: speak MCP to the bridge over stdio without touching any agent CLI.

    PYTHONPATH=src .venv/bin/python tests/smoke_mcp.py

Verifies the handshake, the advertised tool list, and the read-only tools. Runs against a
throwaway CROSS_AGENT_HOME and forces the agent identity with CROSS_AGENT_SELF, so no
`send_to_*` call here can resolve a real session whoever runs it.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src')

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# set before the import: config reads CROSS_AGENT_HOME at import time
STATE_DIR = tempfile.mkdtemp(prefix='cross-agent-smoke-')
os.environ['CROSS_AGENT_HOME'] = STATE_DIR

from cross_agent_mcp import config, registry  # noqa: E402


def _text_of(result) -> str:
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            return block.text
    return ''


def _server(agent: str) -> StdioServerParameters:
    """A bridge server told which agent it is, rather than inferring it from its parent."""
    return StdioServerParameters(
        command=ROOT_DIR + '/.venv/bin/python',
        args=['-m', 'cross_agent_mcp'],
        env={**os.environ, 'PYTHONPATH': ROOT_DIR + '/src',
             'CROSS_AGENT_HOME': STATE_DIR, 'CROSS_AGENT_SELF': agent},
        cwd=ROOT_DIR,
    )


def _delivery_records() -> list:
    """Every delivery record in the throwaway state, finished or in flight."""
    found = []
    for current, _, files in os.walk(config.DELIVERY_DIR):
        found += [os.path.join(current, name) for name in files if name.endswith('.json')]
    return found


async def _refuses_its_own_agent(agent: str) -> int:
    """A send aimed at the caller's own agent is refused before anything is resolved."""
    tool = f'send_to_{agent}'
    async with stdio_client(_server(agent)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            refused = json.loads(_text_of(await session.call_tool(
                tool, {'message': f'self-call guard check ({agent})'})))

    if refused.get('ok') or 'refusing to relay' not in refused.get('error', ''):
        print(f'[FAIL] self-call guard did not trigger for {agent}: {refused}')
        return 1
    print(f'[ok] self-call guard ({agent}) -> refused as expected')
    return 0


async def main() -> int:
    async with stdio_client(_server('claude')) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f'[ok] initialize -> {init.server_info.name} v{init.server_info.version}')

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f'[ok] tools/list -> {names}')

            expected = {'send_to_codex', 'send_to_claude', 'list_agent_sessions',
                        'bridge_status', 'pin_agent_session'}
            missing = expected - set(names)
            if missing:
                print(f'[FAIL] missing tools: {missing}')
                return 1

            status = json.loads(_text_of(await session.call_tool('bridge_status', {})))
            print(f'[ok] bridge_status -> running_under={status["running_under"]} '
                  f'max_hops={status["settings"]["max_hops"]}')
            for agent, resolved in status['resolved_sessions'].items():
                label = resolved.get('session_id') if resolved else None
                print(f'       resolved {agent}: {label}')

            listing = json.loads(_text_of(await session.call_tool(
                'list_agent_sessions', {'agent': 'both', 'scope': 'cwd'})))
            print(f'[ok] list_agent_sessions -> claude={len(listing["claude"])} '
                  f'codex={len(listing["codex"])}')

            # the hop check runs before the target is resolved, so nothing is ever addressed
            conversation_id = 'conv_smoke_hop_guard'
            for _ in range(config.MAX_HOPS):
                registry.bump_conversation(conversation_id, 'claude', 'codex')
            capped = json.loads(_text_of(await session.call_tool(
                'send_to_codex', {'message': 'hop guard check',
                                  'conversation_id': conversation_id})))
            if capped.get('ok') or 'hop limit' not in capped.get('error', ''):
                print(f'[FAIL] hop guard did not trigger: {capped}')
                return 1
            print(f'[ok] hop guard -> refused after {config.MAX_HOPS} hops')

            status = json.loads(_text_of(await session.call_tool(
                'bridge_status', {'cwd': ROOT_DIR})))
            deliveries = status.get('deliveries') or {}
            if 'pending' not in deliveries or 'recent' not in deliveries:
                print(f'[FAIL] bridge_status does not report deliveries: {deliveries}')
                return 1
            print(f'[ok] bridge_status -> deliveries pending={len(deliveries["pending"])} '
                  f'recent={len(deliveries["recent"])}')

            # A busy target is no longer refused - the outbox waits for the lock and then
            # delivers - so exercising it here would spend a real agent turn. That behaviour
            # is covered without any turn by unit_guards.test_a_busy_session_is_waited_out.

    # both identities: under ambient detection exactly one of these was a real send
    for agent in ('claude', 'codex'):
        if await _refuses_its_own_agent(agent):
            return 1

    records = _delivery_records()
    if records:
        print(f'[FAIL] the run created delivery records: {records}')
        return 1
    print('[ok] no delivery record was created by any of it')

    if not os.path.realpath(config.HOME_DIR).startswith(os.path.realpath(STATE_DIR)):
        print(f'[FAIL] the run was not isolated: {config.HOME_DIR}')
        return 1
    print(f'[ok] state stayed in {STATE_DIR}')

    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
