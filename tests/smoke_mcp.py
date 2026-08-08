"""Smoke test: speak MCP to the bridge over stdio without touching any agent CLI.

    PYTHONPATH=src .venv/bin/python tests/smoke_mcp.py

Verifies the handshake, the advertised tool list, and the read-only tools.
`send_to_*` is deliberately not exercised here: it would spend real agent turns.
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/src')

from cross_agent_mcp import config, discovery, registry, uihook  # noqa: E402


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _text_of(result) -> str:
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            return block.text
    return ''


async def main() -> int:
    params = StdioServerParameters(
        command=ROOT_DIR + '/.venv/bin/python',
        args=['-m', 'cross_agent_mcp'],
        env={**os.environ, 'PYTHONPATH': ROOT_DIR + '/src'},
        cwd=ROOT_DIR,
    )

    async with stdio_client(params) as (read, write):
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

            refused = json.loads(_text_of(await session.call_tool(
                'send_to_claude', {'message': 'self-call guard check'})))
            if refused.get('ok') or 'refusing to relay' not in refused.get('error', ''):
                print(f'[FAIL] self-call guard did not trigger: {refused}')
                return 1
            print('[ok] self-call guard -> refused as expected')

            # exhaust the hop budget up front so no agent turn is ever spent
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

            # resolve the target the same way the bridge does: the panel session wins over
            # anything inferred from transcripts, and the lock has to land on that one
            target = (uihook.find_live_session(config.AGENT_CODEX) if uihook.is_enabled() else None)
            if not target:
                target = discovery.find_active_session(
                    config.AGENT_CODEX, config.DEFAULT_SCOPE, ROOT_DIR)
            if target:
                with registry.busy_lock(config.AGENT_CODEX, target['session_id'], 'conv_smoke_busy'):
                    blocked = json.loads(_text_of(await session.call_tool(
                        'send_to_codex', {'message': 'busy guard check', 'cwd': ROOT_DIR})))
                if blocked.get('ok') or 'already waiting' not in blocked.get('error', ''):
                    print(f'[FAIL] busy guard did not trigger: {blocked}')
                    return 1
                print('[ok] busy guard -> refused while the peer session is awaiting a reply')
            else:
                print('[skip] busy guard -> no reachable Codex session in this directory')

    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
