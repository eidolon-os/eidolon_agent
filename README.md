# eidolon-agent

> 桌面陪伴中控大脑。被 LiveKit 语音 pipeline 通过 gRPC 当作 LLM 调用；对接外部
> `eidolon-memory` 服务（MCP+NATS）；通过本地 long-task worker 调用 mementos coworker。

实时陪伴中控的 **快速应答内核**：热路径只做 prompt 拼装 + LLM 流式回复 + safety check，
其它一切（持久化、记忆写入、人格演化、长任务执行）都走异步链路。

---

## 目录

1. [产品定位](#1-产品定位)
2. [系统拓扑](#2-系统拓扑)
3. [代码分层](#3-代码分层)
4. [核心抽象 —— 5 个 Port](#4-核心抽象--5-个-port)
5. [Turn 热路径](#5-turn-热路径)
6. [异步事件总线](#6-异步事件总线)
7. [人格子系统](#7-人格子系统)
8. [对外接口](#8-对外接口)
9. [配置与持久化](#9-配置与持久化)
10. [快速开始](#10-快速开始)
11. [测试 & CI](#11-测试--ci)
12. [仓库地图](#12-仓库地图)

---

## 1. 产品定位

- **不是** LiveKit client。它**是**被 LiveKit 调用的 **LLM 替代品 / Brain 服务**。
- 业务边界：**桌面陪伴 AI** —— 同一个 user 长期对话，关心情绪、记得过去说过什么、能根据交互演化人格。
- 设计原则：**实时响应优先**。LLM 本身 1–30s，其他流程必须毫秒级；非阻塞工作全部异步。
- 单进程多用户：同一个 eidolon-agent 实例可以服务多个 (tenant, user) 对，每对自动 lazy 创建一个 CompanionAgent。

### 关键决策一览

| 决策 | 取舍 |
|---|---|
| Hexagonal Architecture（4 层 + Port/Adapter） | 多一层抽象，换底层不动业务 |
| NATS 一统总线（pub/sub + JetStream + KV） | 不引入 Redis；in-process 仍可用 `InMemoryEventBus` 同 Protocol |
| LiteLLM 作为唯一 LLM 出口 | 100+ provider 切换零成本；坏处：cost map 噪声 |
| SQLite WAL + SQLAlchemy 2.0 async | 单机部署足够；多机时迁 Postgres 改 URL 即可 |
| Persona = YAML 模板 + 实例（运行时状态 + 异步演化） | Git-friendly 模板；运行时 mood/energy 不写回 YAML |
| gRPC 数据面 + 两个 HTTP（健康 / Admin） | 与 LiveKit 同机走 UDS；admin 独立端口免污染 |

---

## 2. 系统拓扑

```
                ┌──────────────────────────────┐
                │       LiveKit voice agent     │
                │   (caller of this service)    │
                └──────────────┬───────────────┘
                               │ gRPC Chat bidi  (UDS or :45051)
                               ▼
        ┌──────────────────────────────────────────────┐
        │              eidolon-agent                    │
        │  ┌───────────────────────────────────────┐    │
        │  │  app/transport  (gRPC / HTTP /admin)  │    │
        │  └────────────────┬──────────────────────┘    │
        │                   │                            │
        │  ┌────────────────▼──────────────────────┐    │
        │  │  domain  (TurnEngine / Personas / …)  │    │
        │  └────────┬──────────────────────┬───────┘    │
        │           │                      │             │
        │  ┌────────▼──────┐    ┌──────────▼──────┐     │
        │  │  infra/llm    │    │  infra/persistence│   │
        │  │  LiteLLM      │    │   SQLite WAL     │     │
        │  └───────────────┘    └──────────────────┘     │
        │           ▲                      ▲             │
        └───────────┼──────────────────────┼─────────────┘
                    │ HTTP                  │ pub/sub + KV
        ┌───────────▼────────────┐   ┌─────▼──────────┐
        │   LLM endpoint          │   │   NATS server   │
        │   (DeepSeek / OpenAI    │   │   (JetStream)   │
        │    via LiteLLM)         │   └──┬─────┬───────┘
        └─────────────────────────┘      │     │
                                         │     │
                ┌────────────────────────┘
                ▼                                              ▼
   ┌─────────────────────────┐               ┌────────────────────────┐
   │  eidolon-memory          │               │  mementos sidecar       │
   │  (external service)      │               │  (local HTTP service)   │
   │   ┌──────────────────┐   │               │  long-task worker calls │
   │   │ MCP HTTP (read)  │◀──┤ MCP recall    │  /api/v1/chat/...       │
   │   │ NATS (write)     │◀──┤ NATS turn pub │                            │
   │   └──────────────────┘   │               └────────────────────────────┘
   └─────────────────────────┘
```

**对外接口（5 个）**：
1. **进来的**：LiveKit / 设备 →（gRPC Chat） → eidolon-agent
2. **进来的**：运维 / 脚本 →（HTTP）→ eidolon-agent admin :8081
3. **出去的**：eidolon-agent →（HTTPS）→ LLM endpoint
4. **出去的**：eidolon-agent →（MCP HTTP / NATS publish）→ eidolon-memory
5. **出去的**：eidolon-agent →（HTTP worker）→ mementos sidecar

> ⚠️ **LLM 出口与代理**：代理策略由**部署层**统一管理，agent 代码本身不感知。在
> admin/supervisord 启动的栈里，`NO_PROXY=127.0.0.1,localhost,::1,*.local` 由 supervisord
> 注入到所有子进程，环回流量直连；外网 LLM 调用按 operator shell 里的
> `HTTP(S)_PROXY` 自然走（国内用户用 Clash/VPN 访问 openai.com 就是这条路径）。
> 单独跑 agent（脱离 supervisord）时请确保 shell 里有 `NO_PROXY=127.0.0.1,localhost,::1`，
> 否则环回 endpoint（memory MCP / discovery）可能被系统代理劫持。启动时仍会对 default
> model 做一次 warmup 把 DNS/TCP/TLS + 上游模型 cold start 提前到 boot（`startup_warm_*`）。

---

## 3. 代码分层

四层严格依赖方向 `core ← domain ← infra ← app`，由 [`import-linter`](https://import-linter.readthedocs.io/) 在 CI 强制：

```
eidolon_agent/
├── core/              # L0 — 纯类型 & 协议，对兄弟模块零依赖
│   ├── types/         #   ChatMessage / TurnEvent / LLMDelta / MemoryHit /
│   │                  #   Event / Topics / Identity / Signal / Tool / Turn / Messages
│   ├── ports/         #   LLMPort / MemoryPort / EventBus / KVStore / ToolPort /
│   │                  #   ChatMessageRepository / ConversationRepository /
│   │                  #   DeviceRepository / UnitOfWork
│   └── errors.py      #   异常类型层级
│
├── domain/            # L1 — 业务逻辑，只允许 import core/
│   ├── agent/         #   TurnEngine（热路径） + Registry + Companion
│   │                  #   + Triage + LLM tool-call loop
│   ├── personas/      #   PersonasService + 模板/实例存储 + 异步演化 worker
│   │                  #   + memory adapter + signal adapter
│   ├── context/       #   ContextCompiler — 直接拼装 persona + memory + history
│   ├── guardrails/    #   InputGuardrail / OutputGuardrail / CrisisHandler
│   ├── tools/         #   ToolRegistry + 顺序 ToolDispatcher + builtin tools
│   ├── history/       #   HistoryManager（in-mem 窗口） + Fanout（NATS publish）
│   │                  #   + ports.py (MemoryTurnSubjectResolver Protocol)
│   └── signals/       #   SignalBus —— 实时信号 ring buffer
│
├── infra/             # L2 — 外部系统适配器，实现 core/ports/
│   ├── llm/           #   LLMRouter + LiteLLMProvider + FakeLLM
│   ├── persistence/   #   SQLAlchemy 2.0 async + 4 个 Sql*Repository
│   │                  #   + Alembic migrations + UoW
│   ├── events/        #   NatsEventBus + NatsKVStore (生产)
│   │                  #   InMemoryEventBus + InMemoryKVStore (测试)
│   ├── memory/        #   McpClientPool + MemoryNatsPublisher
│   │                  #   + MemoryRoutingTable + EidolonMemoryPort
│   └── observability/ #   loguru 配置（stdlib 桥接 + 三方静默）
│
├── app/               # L3 — 进程编排 + I/O 入口
│   ├── runtime/       #   bootstrap.py（10 步连线）/ container / cli / lifecycle
│   ├── transport/     #   grpc/ (Chat servicer + interceptors + codec)
│   │                  #   http/ (健康探针 /readyz)
│   │                  #   pairing/ (JWT device token + 配对码)
│   └── admin/         #   独立 FastAPI app + 路由
│                      #     devices / personas / pairing / chat_test
│
└── config/            # 共享 Pydantic settings（所有层都可读）
```

**分层契约（4 条 import-linter rule，CI 阻塞）**：
- `core` 对所有兄弟模块零依赖
- `domain` 不允许 import `infra/` 或 `app/`
- `infra` 不允许 import `app/`
- `infra` 引用 `domain` 限于 `*.types`（实现 domain-owned port 时允许）
- `tests/**` 全部豁免（测试可自由 import 任意层构造夹具）

---

## 4. 核心抽象 —— 5 个 Port

Port 是 Hexagonal Architecture 里的"业务对外的插口"。domain 代码只 import Protocol，**不知道也不关心**对面是哪个 adapter。

| Port | 定义在 | 抽象 | infra 实现 |
|---|---|---|---|
| `LLMPort` | `core/ports/llm.py` | 流式 LLM (`stream / count_tokens`) | `LLMRouter` (按名分发) + `LiteLLMProvider` + `FakeLLM` |
| `MemoryPort` | `core/ports/memory.py` | 外部 eidolon-memory（读 MCP / 写 NATS） | `EidolonMemoryPort` (composes pool + publisher) |
| `EventBus` | `core/ports/events.py` | 异步 publish/subscribe + request/reply | `NatsEventBus` (生产) / `InMemoryEventBus` (测试) |
| `KVStore` | `core/ports/events.py` | get/put/cas/watch/keys | `NatsKVStore` / `InMemoryKVStore` |
| `ToolPort` | `core/ports/tool.py` | LLM 可调用工具（schema + invoke） | `GetTimeTool` / `EmitEventTool` / 自定义 |
| `ChatMessageRepository` 等 4 个 | `core/ports/persistence.py` | SQLite 数据访问 | `Sql{ChatMessage,Conversation,Device,EvolutionHistory}Repository` |

**装配点**：`app/runtime/bootstrap.py` 是唯一知道"哪个 Port 用哪个 Adapter"的地方。例如 `LLMRouter(providers={"openai/deepseek-v4-flash": LiteLLMProvider(...), "fake": FakeLLM()})` 在这里组装好后注入到 `TurnEngine`。

**好处**：
- 换 LLM provider（OpenAI → Anthropic → Ollama）不需要动 domain 代码一行
- 测试用 `FakeLLM` + `InMemoryEventBus` 不用任何 mock 框架
- 重构 NATS（升级 SDK / 切换 broker）也不影响 turn 逻辑

---

## 5. Turn 热路径

```
gRPC Chat (bidi stream)
   │
   ▼
EidolonAgentServicer.Chat   ←  AuthInterceptor 校验 device_token
   │
   ▼
AgentRegistry.resolve_for_caller(tenant_id, user_id)   ← lazy 创建 CompanionAgent
   │
   ▼
CompanionAgent.run_turn(turn_input)
   │
   ▼
TurnEngine.run(ti)   ← 以下为热路径，逐步 yield TurnEvent
   │
   ├─ 1. input_guardrail.check(ti.text)         [< 2ms]
   │     ├─ ESCALATE → CrisisHandler.handle → 返回安抚 + 应急资源 + DONE
   │     ├─ REFUSE   → 返回拒绝消息 + DONE
   │     └─ FORGET   → 确认遗忘 + DONE
   │
   ├─ 2. triage.classify(ti.text)               [< 1ms]
   │     ├─ TOOL_DIRECT  → 简化路径（未来）
   │     └─ COMPLEX_LONG → 仅作为 trace；复杂任务由 LLM 调用 delegate_to_coworker
   │
   ├─ 3. yield STATE(thinking)
   │
   ├─ 4. ContextCompiler.compile(ti)            [≤ 250ms 含 memory recall]
   │     ├─ PersonasService.compile_prompt(dry_run_memory=[])   ← 跳过 personas 内部 recall
   │     ├─ MemoryPort.recall_context(timeout=200ms)            ← best-effort，失败降级
   │     ├─ HistoryManager.recent_window(N=20)                  ← in-mem
   │     └─ realtime digest from ti.realtime
   │   → list[ChatMessage]  =  [system] + [history…] + [user]
   │
   ├─ 5. yield STATE(speaking)
   │
   ├─ 6. LLMPort.stream(messages, tools=…) 多轮工具循环（≤4 iters）:
   │     ├─ delta.text_delta  → yield DELTA(chunk)         ← 用户看到打字
   │     ├─ delta.tool_call   → yield TOOL_CALL + 入队
   │     ├─ delta.usage       → yield USAGE
   │     └─ finish=TOOL_CALLS → ToolDispatcher.dispatch_serial → 把结果喂回 messages
   │
   ├─ 7. output_guardrail.check                 [< 5ms]
   │     └─ SOFTEN → 前置免责
   │
   ├─ 8. yield DONE  ←━━━━━━━ 用户在此看到完整回复，HTTP/gRPC 流可关闭
   │
   └─ 9. asyncio.create_task(_post_turn(...))   ←━ 后台异步，不阻塞返回
                            │
                            ├─ HistoryManager.append (SQLite 写)
                            ├─ HistoryFanout.publish_turn (NATS → memory + emotion)
                            └─ PersonasService.submit_interaction (内部队列 → evolution worker)
```

**延迟预算（除 LLM）**：< 250ms，其中 200ms 是 memory recall 的上限。LLM 本身 1–30s 取决于 token 数。

**latency benchmark**：`tests/integration/test_hot_path_latency.py` 用 `FakeLLM`（per_token_delay_s=0）+ 无 memory 测试纯框架开销，断言 first DELTA < 100ms、DONE < 200ms。

---

## 6. 异步事件总线

NATS 是核心总线。**进程内** fire-and-forget 直接 `asyncio.create_task`；**跨进程** 走 NATS subject。两者共享同一个 `EventBus` Protocol。

### NATS Subject 约定（全部在 `core/types/topics.py`）

| 类别 | Subject | 用途 | JetStream? |
|---|---|---|---|
| 内部生命周期 | `agent.turn.completed.<conv_id>` | turn 完成通知 | 否 |
| 内部生命周期 | `agent.fsm.changed.<session_id>` | FSM 状态变化 | 否 |
| 人格 | `agent.persona.template.reloaded.<tpl>` | 模板热重载 | 否 |
| 人格 | `agent.persona.overlay.updated.<inst>` | 实例 overlay 更新 | 否 |
| 人格 | `agent.evolution.proposed.<inst>` | 演化提案 | **是** |
| 人格 | `agent.evolution.applied.<inst>` | 演化已应用 | **是** |
| 人格 | `agent.evolution.rolled_back.<inst>` | 演化回滚 | **是** |
| 信号 | `agent.signal.<modality>.<session>` | 实时信号入队 | 否 |
| 系统 | `agent.system.config.updated` | 配置变更广播 | 否 |
| 系统 | `agent.pairing.revoked` | 设备 token 吊销 | 否 |
| **外部出站** | `agent.memory.conversation.turn.<user>` | turn 完成 → memory 服务 | **是** |
| **外部出站** | `agent.memory.cmd.<user>` | KG 写命令 → memory 服务 | **是** |
| **外部出站** | `agent.emotion.turn.<user>` | turn 完成 → emotion 服务 | **是** |
| **外部入站** | `agent.memory.event.*` | memory 服务推送（promise_due 等） | **是** |
| **外部入站** | `agent.persona.evolution.proposed.*` | emotion 服务提出的演化 | **是** |
长任务不再使用 NATS subject；`delegate_to_coworker` 写入 SQLite receipt 后进入本地内存队列，由 mementos worker 通过 HTTP 执行。`submit_long_task` 仅保留为兼容旧调用的隐藏别名。

JetStream 持久化前缀：`agent.memory.*` / `agent.emotion.*` / `agent.evolution.*`。`is_persistent(subject)` 自动判定。

### KV Buckets

通过 `NatsKVStore` 暴露，bootstrap 阶段 `ensure_buckets` 预创建：

| Bucket | 用途 |
|---|---|
| `EIDOLON_CACHE` | 通用缓存（如 memory 短期 cache，已弃用） |
| `EIDOLON_SESSION` | 会话上下文 |
| `EIDOLON_HISTORY_WINDOW` | 历史窗口缓存（预留） |
| `EIDOLON_RATELIMIT` | 限流计数 |
| `EIDOLON_CONFIG` | 动态配置 |
| `EIDOLON_FLAGS` | 功能开关 |
| `EIDOLON_EXP` | A/B 实验 |
| `DEVICE_REVOCATIONS` | 设备吊销名单（RuntimeTokenVerifier 查询） |
| `EIDOLON_TOOL_IDEMP` | 工具幂等性 cache（ToolDispatcher 用） |

---

## 7. 人格子系统

详见 [`eidolon_agent/domain/personas/README.md`](eidolon_agent/domain/personas/README.md)。要点：

- **模板** 是只读 YAML（`domain/personas/templates/*.yaml`），定义 identity_core + 行为旋钮 (knobs)。
- **实例** 是 companion 的运行时 persona/genome 快照；生产环境写入 `eidolon_data.persona_genomes`。
- **演化** 异步：TurnEngine yield DONE 后 `submit_interaction` 入队，`PersonaEvolutionWorker` 后台消费、按规则微调 knob、持久化到 SQLite + YAML。
- **运行时状态**（mood/energy/attention）不写 YAML，长在内存里 + 周期 snapshot。
- **演化护栏**：identity_core 不可演化；knob 有 min/max + step_limit + cooldown。

热路径只用 3 个公开方法：`compile_prompt / submit_signal / submit_interaction`。其余是 admin 路由调用。

---

## 8. 对外接口

### gRPC（数据面，`:45051` 或 UDS）

`app/transport/grpc/proto/eidolon.proto`：

```proto
service EidolonAgent {
  rpc Chat(stream ChatRequest) returns (stream TurnEvent);
  rpc PushSignal(SignalRequest) returns (Ack);
  rpc SubscribeProactive(SubscribeRequest) returns (stream ProactiveEvent);
}
```

`AuthInterceptor` 在每个 RPC 上校验 `Bearer <runtime_token>`（JWT，HS256）。Token 必须携带
`RuntimeIdentity(owner_id, companion_id, device_id, memory_realm_id, genome_id)`。

Agent 是 companion runtime，不负责 owner 注册、device pairing、或 companion 选择。外部调用方
必须在进入 Agent 前完成 device -> companion 绑定，并重新签发包含具体 companion/genome/realm 的
runtime token。对话热路径不会再按 owner 查询 active companion。

### HTTP（`:8180`）—— 健康探针

仅一个端点 `/readyz`，给 systemd / k8s liveness 用。

### Admin HTTP（`:8081`）—— 控制面

- `GET  /api/admin/devices` —— 设备列表
- `DELETE /api/admin/devices/{id}` —— 吊销设备
- `POST /api/admin/owners/{owner_id}/revoke-sessions` —— 吊销 owner 下 runtime token
- `GET  /api/admin/personas/templates` —— 模板列表
- `POST /api/admin/chat/test` —— owner/companion runtime chat dogfood 端点
- `GET  /api/admin/conversations/turns` —— 对话/消息/turn 检查入口

---

## 9. 配置与持久化

### 配置：`config/settings.yaml` + `config/.env`

非密钥项在 `config/settings.yaml`（模板 `config/settings.example.yaml`），密钥在 `config/.env`（模板 `config/.env.example`）。首次运行按 §10 从模板复制即可。

Pydantic settings 顶层段：

| Section | 内容 |
|---|---|
| `grpc` | host / port / UDS path / TLS / keepalive |
| `http` | 健康 HTTP + admin port + CORS |
| `nats` | URL + creds + kv bucket 列表 |
| `memory` | endpoints / discovery_url / recall_timeout_s |
| `sqlite` | path / WAL / busy_timeout / synchronous |
| `llm` | models 列表 + default_model（LiteLLM 命名） |
| `long_task` | mementos_base_url / 队列 / worker 超时 |
| `persona` | templates_dir / instances_dir |
| `observability` | log_level / log_dir |
| `pairing` | jwt_secret / 算法 / TTL |
| `runtime` | log_dir / run_dir / debug_dir / 关停超时 |
| `turn` | 各阶段 SLO（compile/recall/first_delta）+ tool 最大轮数 + token 预算 |

### SQLite 表（5 张，定义在 `infra/persistence/models.py`）

| 表 | 用途 |
|---|---|
| `conversations` | 会话 (tenant/user/instance) |
| `turns` | 对话轮（含 latency、token、status） |
| `chat_messages` | 消息（FK 到 turn） |
| `devices` | 配对设备 + token hash + 吊销时间戳 |
| `evolution_history` | 演化审计（rationale + delta） |

Alembic 在 `infra/persistence/migrations/versions/`：
- `0001_initial` —— 初始 5 表（原 11 表）
- `0002_drop_unused_tables` —— Phase 4 drop 掉 tenants/users/agent_instances/pairing_codes/audit_log/publish_outbox

---

## 10. 快速开始

### 一次性初始化

```bash
uv sync --extra dev

# 配置（若尚不存在）
cp -n config/settings.example.yaml config/settings.yaml
cp -n config/.env.example config/.env
# 编辑 config/.env：EIDOLON_AGENT_LLM_API_KEY、PAIRING_JWT_SECRET

# gRPC stub 重生成
.venv/bin/python -m grpc_tools.protoc \
  -I eidolon_agent/app/transport/grpc/proto \
  --python_out=eidolon_agent/app/transport/grpc/proto \
  --grpc_python_out=eidolon_agent/app/transport/grpc/proto \
  --pyi_out=eidolon_agent/app/transport/grpc/proto \
  eidolon_agent/app/transport/grpc/proto/eidolon.proto
sed -i.bak 's/^import eidolon_pb2 as eidolon__pb2/from . import eidolon_pb2 as eidolon__pb2/' \
  eidolon_agent/app/transport/grpc/proto/eidolon_pb2_grpc.py
rm -f eidolon_agent/app/transport/grpc/proto/eidolon_pb2_grpc.py.bak

.venv/bin/alembic upgrade head
mkdir -p ~/eidolon/{run,logs,debug,history}
```

### 启动

```bash
# NATS（JetStream，另开终端或 systemd）
nats-server -js -sd ~/eidolon/nats-jetstream --port 4222 --http_port 8222

# agent（前台）
.venv/bin/eidolon-agent
```

启动后端口：
- `:45051` gRPC（LiveKit / 设备；勿用 50000–60000，与 LiveKit RTC 端口段冲突）
- `:8180`  HTTP 健康探针 `/readyz`
- `:8081`  Admin HTTP `/api/admin/*`、OpenAPI `/api/docs`

### 验证

```bash
python scripts/livekit_sim.py "你好"
curl -s http://127.0.0.1:8180/readyz
curl -s http://127.0.0.1:8081/api/docs
```

---

## 11. 测试 & CI

### 测试同位 + 三档 marker

```
eidolon_agent/<layer>/<module>/tests/
    unit/         pytest -m unit         # 纯单元，全 mock
    functional/   pytest -m functional   # 模块级，可用 in-mem adapter
tests/
    integration/  pytest -m integration  # 跨模块端到端（FakeLLM + InMemoryEventBus）
    test_architecture.py                 # 跨层 deep-import 契约
```

### 命令

```bash
.venv/bin/pytest --strict-markers          # 全量 222 个
.venv/bin/pytest -m unit                    # 最快
.venv/bin/pytest -m "not smoke"             # CI 默认
.venv/bin/pytest --cov                      # 覆盖率（卡 77%）
.venv/bin/lint-imports                      # 4 条分层契约
.venv/bin/ruff check .                      # 风格
```

### 当前数字

| 指标 | 值 |
|---|---|
| 测试总数 | **222** (151 unit + 65 functional + 5 integration + 1 architecture) |
| 项目覆盖率 | **77.2%** |
| infra 层覆盖率 | **80.0%** |
| import-linter 契约 | **4 / 4 KEPT** |
| ruff | 0 错 |

CI 卡点：
1. `pytest --strict-markers --cov`（cov ≥ 77%）
2. `lint-imports` 4 contracts
3. `ruff check`

---

## 12. 仓库地图

```
eidolon_agent/             ← Python 包（见 §3）
config/
├── settings.example.yaml  ← 非密钥模板（git-tracked）
├── settings.yaml          ← 本地配置（git-ignored）
├── .env.example           ← 密钥模板
└── .env                   ← 本地密钥（git-ignored）
scripts/
├── livekit_sim.py         ← 端到端模拟脚本
├── replay_conversation.py ← 回放 Eidolon Data 中的会话
└── benchmark/             ← 性能基准（预留）
tests/
├── conftest.py            ← 顶层 fixtures（event_bus / personas_service / turn_engine_factory）
├── helpers.py             ← 工具函数（make_turn_input 等）
├── test_architecture.py   ← 跨层 import 检查
└── integration/           ← 跨模块端到端测试
pyproject.toml             ← uv 项目 + ruff / pytest / coverage / import-linter 配置
conftest.py                ← pytest 入口（位于项目根，自动发现）
README.md                  ← 本文件
```

---

## License

MIT.
