#!/usr/bin/env bash
# Wrapper for the Claude process the VS Code extension launches.
#
#   VS Code setting:  "claudeCode.claudeProcessWrapper": "/Users/dexter/project/cross-agent_mcp/claude-shim.sh"
#
# The extension invokes a wrapper as `<wrapper> <real-claude-binary> <args...>`. Every
# invocation is forwarded; only the panel's stream-json session is intercepted so the bridge
# can hand messages to the conversation the user has open. On any failure the real binary is
# exec'd directly and Claude behaves as usual.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "cross-agent shim: venv missing at ${PYTHON_BIN}, running Claude directly" >&2
    exec "$@"
fi

export PYTHONPATH="${ROOT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONUNBUFFERED=1

exec "${PYTHON_BIN}" -m cross_agent_mcp.claude_shim "$@"
