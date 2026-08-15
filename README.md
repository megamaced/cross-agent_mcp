# cross-agent MCP

VS Code에서 **이미 대화 중인 Claude Code 세션**과 **Codex 스레드**를 서로 교신시키는 중계 MCP 서버.

새 에이전트를 매번 띄우는 것이 아니라, 각 제품이 디스크에 남기는 세션 트랜스크립트에서
**현재 활성 세션 ID를 찾아 그 세션을 resume** 하므로 양쪽 모두 기존 문맥을 그대로 유지한다.

```
        VS Code
           │
   ┌───────┴────────┐
   │                │
Claude Code       Codex
session A        thread B
   │                │
   └──── cross-agent MCP ────┘
              │
       session registry
   (~/.cross-agent/registry.json)
```

| 방향 | 도구 | 패널 셰임이 있을 때 | 없을 때 (폴백) |
|---|---|---|---|
| Claude → Codex | `send_to_codex` | 패널 app-server에 `turn/start` 주입 | `codex exec resume <thread-id> --json` |
| Codex → Claude | `send_to_claude` | 패널 프로세스에 stream-json 사용자 메시지 주입 | `claude -p --resume <session-id> --output-format json` |

셰임을 붙이면 교신이 **실제 VS Code 패널에 그대로 렌더링**된다(3장 참고).

---

## 1. 요구사항

- macOS / Linux, Python 3.10+
- `claude` CLI (Claude Code 2.x), `codex` CLI (0.146+)
- 두 CLI 모두 로그인 완료 상태

## 2. 설치

```bash
cd /Users/dexter/project/cross-agent_mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x run-server.sh
```

## 3. 등록

양쪽 모두 **user-level(전역)** 로 등록한다. 브리지가 새로 만드는 세션은
샌드박스/승인 없이 동작하도록 환경변수를 함께 준다.

### Claude Code

```bash
claude mcp add cross-agent -s user \
  -e CROSS_AGENT_CODEX_SANDBOX=danger-full-access \
  -e CROSS_AGENT_CLAUDE_PERMISSION_MODE=bypassPermissions \
  -- /Users/dexter/project/cross-agent_mcp/run-server.sh
```

`~/.claude.json`의 최상위 `mcpServers`에 기록되어 모든 프로젝트에서 쓸 수 있다.
특정 프로젝트에만 붙이려면 `-s local`, 리포지터리로 공유하려면 프로젝트 루트 `.mcp.json`을 쓴다.

```json
{
  "mcpServers": {
    "cross-agent": {
      "command": "/Users/dexter/project/cross-agent_mcp/run-server.sh",
      "env": {
        "CROSS_AGENT_CODEX_SANDBOX": "danger-full-access",
        "CROSS_AGENT_CLAUDE_PERMISSION_MODE": "bypassPermissions"
      }
    }
  }
}
```

### Codex

```bash
codex mcp add cross-agent \
  --env CROSS_AGENT_CODEX_SANDBOX=danger-full-access \
  --env CROSS_AGENT_CLAUDE_PERMISSION_MODE=bypassPermissions \
  -- /Users/dexter/project/cross-agent_mcp/run-server.sh
```

`~/.codex/config.toml`에 아래가 추가된다(Codex는 전역 설정만 지원).

```toml
[mcp_servers.cross-agent]
command = "/Users/dexter/project/cross-agent_mcp/run-server.sh"
default_tools_approval_mode = "approve"   # UI에서 매번 승인 프롬프트가 뜨지 않도록 (수동 추가)

[mcp_servers.cross-agent.env]
CROSS_AGENT_CLAUDE_PERMISSION_MODE = "bypassPermissions"
CROSS_AGENT_CODEX_SANDBOX = "danger-full-access"
```

`default_tools_approval_mode`는 `codex mcp add`에 해당 플래그가 없어 config.toml에 직접 넣는다.
유효값은 `auto` / `prompt` / `writes` / `approve`이며, 매번 뜨는 승인 프롬프트를 없애려면
`approve`를 쓴다(`auto`로는 계속 물어봤다). 헤드리스 `codex exec`의 취소 문제는 이것으로
해결되지 않는다(9장 참고). UI 프롬프트에서 **"Always allow"** 를 한 번 눌러도 같은 효과다.

### 승인 프롬프트 끄기 (Claude Code)

Claude Code는 MCP 도구 호출마다 승인을 묻는다. `~/.claude/settings.json`에 서버 단위
규칙을 추가하면 된다(도구별 나열이나 `*` 와일드카드가 아니라 **서버 이름 하나**).

```json
{
  "permissions": {
    "allow": ["mcp__cross-agent"]
  }
}
```

> ⚠️ 위 두 환경변수는 **브리지를 통해 도달한 에이전트의 안전장치를 끈다.**
> Claude는 권한 확인 없이 파일을 수정·명령을 실행하고, 새로 생성되는 Codex 세션은
> 샌드박스 없이 동작한다. 신뢰하는 로컬 작업에서만 쓸 것.
> 되돌리려면 두 `-e/--env` 인자를 빼고 다시 등록하면 기본값(`read-only` / 에이전트 기본 권한)으로 돌아간다.

> 등록 직후에는 **VS Code 창을 새로고침**하거나 새 세션을 시작해야 도구가 잡힌다.
> MCP 서버는 세션 시작 시점에만 연결된다.

### IDE 패널 연동 (양방향)

CLI resume 경로(`codex exec resume` / `claude -p --resume`)는 세션 기록에 턴을 덧붙이므로
문맥은 유지되지만 **VS Code 패널에는 나타나지 않는다.** 패널의 세션은 확장이 stdio로 직결해
띄운 자식 프로세스 안에만 살아 있고, 밖에서 들어갈 통로가 없기 때문이다.

두 셰임을 그 파이프 가운데 끼우면 해결된다.

```
VS Code 확장 ──stdio──▶ codex-shim.sh  ──stdio──▶ 진짜 codex app-server
VS Code 확장 ──stdio──▶ claude-shim.sh ──stdio──▶ 진짜 claude (stream-json)
                            ▲
                            │ 유닉스 소켓
                      cross-agent MCP  ──▶ 메시지 주입 ──▶ 패널에 렌더링
```

VS Code 사용자 설정에 추가하고 **창을 새로고침**한다.

```json
"chatgpt.cliExecutable": "/Users/dexter/project/cross-agent_mcp/codex-shim.sh",
"claudeCode.claudeProcessWrapper": "/Users/dexter/project/cross-agent_mcp/claude-shim.sh"
```

| | Codex | Claude Code |
|---|---|---|
| 설정 키 | `chatgpt.cliExecutable` (바이너리 **교체**) | `claudeCode.claudeProcessWrapper` (`<wrapper> <진짜경로> <args>`) |
| 가로채는 호출 | plain `app-server` | `--input-format stream-json` 세션 |
| 주입 방식 | JSON-RPC `turn/start` (id는 `xagent-` 네임스페이스) | stream-json `{"type":"user",...}` |
| 세션 id 출처 | `thread/started` · 요청 params | argv `--resume=` · `system/init` |
| 사람 입력 관측 | 확장이 보낸 `turn/start` · `turn/steer` | 확장이 보낸 `{"type":"user"}` |
| 패널 표시 | 사용자 메시지 + 응답 | 사용자 메시지 + 응답 |
| 설정 성격 | "DEVELOPMENT ONLY" 표시 | 정식 설정 |

공통 규칙:

- 모든 바이트를 그대로 통과시키고, **패널 세션 호출만** 가로챈다
  (`--version`, `login`, `app-server daemon`, `claude -p` 등은 진짜 바이너리로 exec)
- 진짜 바이너리는 확장 디렉터리에서 자동 탐색한다
  (`CROSS_AGENT_REAL_CODEX` / `CROSS_AGENT_REAL_CLAUDE`로 지정 가능)
- 어떤 이유로든 실패하면 진짜 바이너리를 그대로 exec 한다 (fail-open)
- 셰임은 자기 pid 조상 목록을 `~/.cross-agent/panels/<agent>-<pid>.json`에 기록한다.
  브리지는 **자신과 조상을 공유하는 셰임**을 고르므로, 창이 여러 개여도
  "지금 이 IDE 인스턴스"를 정확히 겨냥한다
- Claude 셰임은 사용자가 대화 중이면 그 턴이 끝날 때까지 기다렸다가 주입한다
- Codex 셰임은 **서브에이전트 스레드를 대상에서 제외**한다. 멀티에이전트 실행이 만드는
  스레드는 app-server가 직접 입력을 거부하며(`direct app-server input is not allowed for
  multi-agent v2 sub-agents`), `parentThreadId`·`agentNickname`·`agentRole`·
  `canAcceptDirectInput`로 판별한다. 알림에 실려 오는 thread id만으로는 대상을 새로 만들지
  않고, 확장이 직접 보낸 `thread/start`·`thread/resume`·`turn/start`·`turn/steer`만 신뢰한다.
  그래도 거부당하면 그 스레드를 버리고 새 대화를 열어 한 번 재시도한다

#### 패널에 열린 대화가 없을 때

패널이 대화 목록만 띄우고 있어도 그 뒤에는 살아 있는 프로세스가 있다. 이때 CLI로
폴백하면 요청자는 답을 받지만 **패널은 빈 채로 남아** 브리지가 아무 일도 안 한 것처럼 보인다.
그래서 셰임이 **패널에 새 대화를 연 뒤** 거기에 메시지를 넣는다.

- Codex : `thread/start`로 스레드를 만든다. app-server가 `thread/started` 알림을
  브로드캐스트하므로 확장이 그 스레드를 인지하고 렌더링한다
- Claude : 세션 없이 떠 있는 패널 프로세스에 그냥 사용자 메시지를 쓴다.
  CLI가 새 대화를 시작하고 `system/init`으로 세션 id가 잡힌다

응답의 `was_session_created`가 `true`면 이렇게 새로 연 대화다.
Codex는 `thread/name/set`으로 **"발신자: 메시지 앞부분"** 형태의 제목까지 달아 준다.
그렇지 않으면 목록에 "New chat"으로만 남아 어떤 대화인지 알 수 없다.

새 대화는 목록에 뜨고 unread 표시가 붙지만 **패널이 자동으로 그 대화를 열지는 않는다.**
app-server 프로토콜에는 클라이언트를 특정 대화로 이동시키는 알림이 없고,
확장의 `vscode://` 딥링크(`/local/<thread-id>` 라우트)는 **창을 지정할 수 없어**
열려 있는 모든 VS Code 인스턴스의 Codex 패널이 함께 이동한다. 그래서 채택하지 않았다.

#### 어느 대화 탭으로 가는가

확장은 **대화 탭마다 프로세스를 따로 띄우므로** 한 창에 셰임이 여러 개 뜬다. 어느 탭이
포커스인지는 어디에도 기록되지 않으므로, 다음 순서의 증거로 고른다.

```
1. send_to_*(session_id=...) 로 명시한 세션
2. pin_agent_session 으로 고정한 세션
3. 사람이 마지막으로 입력한 탭 (셰임이 확장→에이전트 방향에서 직접 관측)
4. (셰임 기동 후 아무도 입력하지 않은 경우 - 예: 창 새로고침 직후)
   트랜스크립트가 가장 최근에 갱신된 탭
5. 가장 나중에 열린 탭
6. 열린 대화가 하나도 없으면 → 패널에 새 대화를 연다 (위 절 참고)
```

관측된 사람 입력이 트랜스크립트 시각보다 **항상 우선**한다. 브리지가 주입한 턴도
트랜스크립트를 건드리므로, 그렇지 않으면 브리지가 자기가 마지막에 쓴 탭을 계속
다시 고르게 된다. 주입 턴은 관측 대상이 아니라 이 오염이 애초에 생기지 않는다.

`bridge_status`의 `ide_panels`가 열린 탭 목록과 선택 결과를 그대로 보여준다.
원하는 탭이 아니면 `pin_agent_session`으로 고정하면 된다.

`CROSS_AGENT_UI_HOOK`으로 동작을 고른다 — `auto`(기본, 있으면 쓰고 없으면 CLI로 폴백),
`off`(항상 CLI), `require`(패널을 못 찾으면 조용히 폴백하지 않고 실패).

> ⚠️ 셰임은 확장과 에이전트 사이에 끼는 프로세스다. 확장이 업데이트되면 깨질 수 있고,
> `chatgpt.cliExecutable`은 확장이 "DEVELOPMENT ONLY"로 표시한 application 스코프 설정이다.
> 되돌리려면 해당 설정 줄을 지우고 창을 새로고침하면 된다.

### 상태 점검

```bash
./run-server.sh --check
```

실행 주체, 두 CLI 경로, 현재 해석되는 활성 세션, 적용 중인 설정을 출력하고 종료한다.
인자 없이 실행하면 MCP stdio 서버로 떠서 stdin을 기다린다(정상 동작이며, Ctrl-C로 종료).

---

## 4. 도구

| 도구 | 설명 |
|---|---|
| `send_to_codex(message, ...)` | 활성 Codex 스레드에 메시지를 전달하고 응답을 받아온다 |
| `send_to_claude(message, ...)` | 활성 Claude 세션에 메시지를 전달하고 응답을 받아온다 |
| `list_agent_sessions(agent, scope, cwd, limit)` | 브리지가 찾을 수 있는 세션 목록 (최신순, 활성 여부 포함) |
| `bridge_status(cwd, scope)` | 실행 주체·해석된 세션·설정·잠금 상태 진단 |
| `pin_agent_session(agent, session_id, cwd)` | 특정 세션을 고정. `session_id`를 비우면 해제 |

`send_to_*` 공통 파라미터:

| 이름 | 기본값 | 의미 |
|---|---|---|
| `message` | (필수) | 상대 에이전트에게 보낼 내용. 상대는 이쪽 대화를 못 보므로 자기완결적으로 작성 |
| `session_id` | 자동 탐색 | 특정 세션을 직접 지정 |
| `new_session` | `false` | 활성 세션이 있어도 강제로 새 세션 생성 |
| `scope` | `cwd` | `cwd` = 같은 디렉터리 및 그 하위, `tree` = 상위 디렉터리까지, `any` = 전체 |
| `cwd` | 서버 실행 디렉터리 | 탐색 기준 및 신규 세션 생성 위치 |
| `timeout` | `600` | 상대 턴 대기 시간(초) |
| `conversation_id` | 자동 생성 | 기존 브리지 대화를 이어받아 hop 예산 공유 |
| `raw` | `false` | 브리지 헤더 없이 원문 그대로 전달 |

---

## 5. 세션 해석 규칙

`send_to_*` 호출 시 대상 세션은 다음 순서로 결정된다.

```
1. session_id 인자가 있으면 → 그 세션
2. 레지스트리에 pin이 있으면 → 그 세션
     - pin_agent_session으로 고정한 pin은 만료되지 않음
     - 브리지가 자동 생성한 pin은 활성 창(기본 240분) 안에서만 유효
3. 세션 저장소 스캔 → 조건을 만족하는 가장 적합한 트랜스크립트
     - Claude : ~/.claude/projects/<slug(cwd)>/*.jsonl
                사용자 메시지가 있고 sidechain 전용이 아닌 것
     - Codex  : ~/.codex/sessions/**/rollout-*.jsonl
                session_meta.thread_source == 'user' (subagent 스레드 제외)
                같은 session_id의 rollout이 여러 개면 가장 최신 것
     - 마지막 기록이 활성 창(기본 240분) 이내인 것만 "활성"으로 인정
     - 정렬 우선순위: ① 디렉터리 일치도(정확 > 하위 > 상위) ② 최근 기록순
       상위 디렉터리는 scope='tree'에서만 후보가 된다. 홈 디렉터리가 모든
       프로젝트의 상위이므로, ~에서 연 세션이 아무 프로젝트나 가로채면 곤란하기 때문
4. 위 조건을 만족하는 세션이 하나도 없으면 → 새 세션을 생성한다
     - Claude : claude -p --session-id <새 uuid> ...
     - Codex  : codex exec --json -C <cwd> ... (thread.started에서 id 회수)
     - 생성된 세션은 레지스트리에 pin되어 다음 호출부터 resume 대상이 된다
```

즉 **활성 세션이 있으면 문맥을 유지한 채 이어 붙이고, 없으면 새로 만들어 그 이후로 재사용**한다.

## 6. 무한 호출 방지

세 겹으로 막는다.

1. **hop 예산** — 대화당 최대 `MAX_HOPS`(기본 4)회. `conversation_id`는 자식 프로세스에
   환경변수로 전파되므로 A→B→A→B 체인이 자동으로 같은 예산을 공유한다. 초과 시 거부.
2. **busy 잠금** — 답변을 기다리는 중인 세션으로 되돌아오는 relay를 거부한다.
   `~/.cross-agent/locks/`에 `O_EXCL`로 원자적으로 선점하므로 "확인 후 점유" 사이의
   경합이 없다. 즉 같은 세션을 노린 두 relay가 동시에 들어와도 하나만 통과한다.
   해제는 자기 토큰이 남아 있을 때만 하고, 죽은 프로세스의 잠금은 자동 회수된다.
   체인 정보는 `CROSS_AGENT_BUSY` 환경변수로 자식 프로세스에 전파된다.
   잠금 획득이 hop 차감보다 먼저라 경합에서 밀린 호출은 예산을 쓰지 않는다.
3. **자기호출 가드** — 부모 프로세스 체인으로 호출자를 판별해, Claude가 `send_to_claude`를
   부르는 것을 거부한다(`allow_same_agent=true` + `session_id` 명시 시에만 허용).

전달되는 메시지에는 발신자·대화 ID·남은 hop이 담긴 헤더가 붙고, 수신 에이전트에게
"최종 메시지가 그대로 상대에게 회신된다"는 규칙이 함께 전달된다.

## 7. 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CROSS_AGENT_HOME` | `~/.cross-agent` | 레지스트리·잠금·로그 위치 |
| `CROSS_AGENT_ACTIVE_WINDOW_MIN` | `240` | 활성 세션으로 인정할 최대 경과 시간(분) |
| `CROSS_AGENT_MAX_HOPS` | `4` | 대화당 최대 중계 횟수 |
| `CROSS_AGENT_TIMEOUT` | `600` | 상대 턴 대기 시간(초) |
| `CROSS_AGENT_SCOPE` | `cwd` | 기본 탐색 범위 (`cwd` / `tree` / `any`) |
| `CROSS_AGENT_UI_HOOK` | `auto` | Codex 패널 주입 (`auto` / `off` / `require`) |
| `CROSS_AGENT_REAL_CODEX` | (자동 탐색) | 셰임이 감쌀 진짜 codex 바이너리 |
| `CROSS_AGENT_REAL_CLAUDE` | (자동 탐색) | 셰임이 감쌀 진짜 claude 바이너리 |
| `CROSS_AGENT_CODEX_SANDBOX` | `read-only` | **신규 생성** Codex 세션의 샌드박스 (`read-only` / `workspace-write` / `danger-full-access`) |
| `CROSS_AGENT_CLAUDE_PERMISSION_MODE` | (미설정) | Claude 호출 시 `--permission-mode` (`acceptEdits` / `bypassPermissions` / `plan` 등) |
| `CROSS_AGENT_CODEX_MODEL` / `CROSS_AGENT_CLAUDE_MODEL` | (미설정) | 모델 강제 |
| `CROSS_AGENT_CLAUDE_BIN` / `CROSS_AGENT_CODEX_BIN` | `claude` / `codex` | CLI 경로 |
| `CROSS_AGENT_CODEX_SCAN_LIMIT` | `2000` | Codex rollout 스캔 안전판(scope=`any`에만 적용) |
| `CROSS_AGENT_SELF` | (자동 판별) | 호출자 에이전트 강제 지정 |
| `CROSS_AGENT_DEBUG` | (미설정) | 값이 있으면 DEBUG 로깅 |

Claude Code에서 값을 주려면 `claude mcp add cross-agent -s user -e KEY=VALUE -- <script>`,
Codex에서는 `codex mcp add cross-agent --env KEY=VALUE -- <script>`.

`CROSS_AGENT_CODEX_SANDBOX`는 **새로 생성되는** Codex 세션에만 적용된다.
`codex exec resume`에는 샌드박스 인자가 없어, 기존 세션을 이어받을 때는 그 세션이
처음 시작된 설정을 그대로 따른다. 반면 `CROSS_AGENT_CLAUDE_PERMISSION_MODE`는
신규·resume 양쪽 모두에 적용된다.

## 8. 검증

```bash
# 잠금/pin/스코프/타임아웃 단위 검증 (에이전트 턴 소비 없음)
PYTHONPATH=src .venv/bin/python tests/unit_guards.py

# 셰임 통과·주입·패널 렌더링 검증 (VS Code 설정 불필요)
PYTHONPATH=src .venv/bin/python tests/shim_roundtrip.py          # Codex 턴 1회
PYTHONPATH=src .venv/bin/python tests/claude_shim_roundtrip.py   # Claude 턴 2회

# 프로토콜 핸드셰이크 + 탐색 + 3종 가드 (에이전트 턴 소비 없음)
PYTHONPATH=src .venv/bin/python tests/smoke_mcp.py

# 실제 왕복 5종 (Codex/Claude 턴을 실제로 소비)
PYTHONPATH=src .venv/bin/python tests/live_roundtrip.py
```

로그는 `~/.cross-agent/logs/bridge.log`.

## 9. 알려진 제약

- **Codex의 MCP 도구 승인 (upstream 제약)** — VS Code Codex UI에서는 승인 프롬프트가 뜨고
  사용자가 허용하면 정상 동작한다. 반면 `codex exec` 헤드리스 모드에서는 승인 주체가 없어
  모든 MCP 호출이 `user cancelled MCP tool call`로 자동 취소된다.
  Codex 0.146.0에서 아래를 **직접 시험했고 전부 실패**했다.

  | 시도 | 결과 |
  |---|---|
  | `approval_policy = "never"` | 취소됨 |
  | `mcp_servers.<name>.default_tools_approval_mode = "auto"` | 취소됨 |
  | `approval_policy = { granular = { …, mcp_elicitations = false } }` | 취소됨 |
  | 위 둘 조합 | 취소됨 |
  | `--dangerously-bypass-approvals-and-sandbox` | 통과 |

  `default_tools_approval_mode = "auto"`는 스키마상 유효한 정식 키라 UI 프롬프트를 줄이는
  용도로 config.toml에 넣어 뒀지만, exec 모드의 취소는 막지 못한다.
  참고로 `[permissions.<profile>]` / `default_permissions`는 실재하는 설정이지만
  샌드박스 파일시스템·네트워크 권한용이라 이 문제와 무관하다.
  즉 **헤드리스 Codex→Claude는 현재 upstream 한계**이며, 대화형 사용에는 영향이 없다.
- **UI 반영 시점** — 셰임을 붙이지 않으면 브리지는 세션 트랜스크립트에 턴을 덧붙일 뿐이라
  VS Code 채팅창이 실시간으로 갱신되지 않는다(해당 세션을 다시 열 때 반영).
  3장의 두 셰임을 설정하면 양방향 모두 패널에 그려진다.
- **셰임 경유 시 체인 상태 전파** — 패널 주입은 자식 프로세스를 새로 띄우지 않으므로
  `CROSS_AGENT_CONVERSATION_ID` 같은 환경변수가 상대에게 전달되지 않는다. 대신 발신자
  자신의 세션에도 busy 잠금을 걸어 되돌아오는 relay를 막고, 봉투 헤더에 conversation id를
  실어 상대가 같은 대화로 이어붙일 수 있게 한다.
- **동시 쓰기** — busy 잠금은 브리지가 보내는 relay끼리는 원자적으로 막아 주지만,
  **사람이 VS Code 채팅창에 직접 입력 중인 세션**은 보호하지 못한다(그쪽은 잠금을 모른다).
  상대가 지금 타이핑 중인 세션을 겨냥하지 않는 것이 안전하다.
- **세션 탐색은 mtime 기반**이다. 한 디렉터리에서 여러 세션을 동시에 열어 두었다면
  `pin_agent_session`으로 대상을 고정하는 편이 확실하다.
