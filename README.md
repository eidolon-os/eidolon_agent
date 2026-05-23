# eidolon-agent

> 桌面陪伴中控大脑。被 LiveKit 语音 pipeline 通过 gRPC 当作 LLM 调用；对接外部
> `eidolon-memory` 服务（MCP+NATS）；通过 NATS 与"情感进化""工作站智能体"协作。

实时陪伴中控的 **快速应答内核**：热路径只做 prompt 拼装 + LLM 流式回复 + safety check，
其它一切（持久化、记忆写入、人格演化、工作站任务）都通过 event bus 异步派发。

## 1. 这是什么

- **不是** LiveKit client。它**是**被 LiveKit 调用的 LLM 服务。
- **数据面**：gRPC bidi（同机自动 UDS），暴露 `Chat / ChatOnce / PushSignal / SubscribeProactive / ExchangePairingCode`。
- **控制面**：FastAPI Admin HTTP（`:8081`），给 admin web 与运维使用。
- **核心 HTTP**：FastAPI（`:8080`），健康探针 `/readyz`。
- **持久化**：SQLite3（WAL，aiosqlite + SQLAlchemy 2.0 async）。5 张表：`conversations / turns / chat_messages / devices / evolution_history`。
- **总线**：NATS（pub/sub + JetStream + KV），完全取代 Redis。
- **人格**：YAML 模板（基因，只读）+ 每用户实例（runtime 状态 + 异步演化 worker）。

## 2. 架构总览（4 层 + 共享 config）

```
eidolon_agent/
├── core/              # L0 — 纯类型 & 协议，对兄弟模块零依赖
│   ├── types/         #   ChatMessage / TurnEvent / LLMDelta / MemoryHit / Event / …
│   ├── ports/         #   LLMPort / MemoryPort / EventBus / KVStore / ToolPort / 4 个 Repo
│   └── errors.py
│
├── domain/            # L1 — 业务逻辑，只依赖 core/
│   ├── agent/         #   TurnEngine + Registry + Companion + Triage + Workstation
│   ├── personas/      #   人格模板 / 实例 / 异步演化 worker / prompt 编译
│   ├── context/       #   ContextCompiler — 直接拼装 persona + memory + history
│   ├── guardrails/    #   input / output 过滤 + 危机响应
│   ├── tools/         #   ToolRegistry + 顺序 ToolDispatcher + 内置工具
│   ├── history/       #   HistoryManager + Fanout（通过 port 解耦 NATS）
│   └── signals/       #   实时信号 ring buffer（被 gRPC PushSignal 灌入）
│
├── infra/             # L2 — 外部系统适配器，实现 core/ports/
│   ├── llm/           #   LiteLLM provider + Router + FakeLLM
│   ├── persistence/   #   SQLAlchemy 2.0 async + 4 个 Repo + Alembic
│   ├── events/        #   NatsEventBus + JetStream KV + InMemory 替身
│   ├── memory/        #   eidolon-memory MCP 客户端 + NATS 发布 + 路由发现
│   └── observability/ #   loguru 配置（含 stdlib 桥接 + 三方静默）
│
├── app/               # L3 — 进程编排 + I/O 入口
│   ├── runtime/       #   bootstrap / container / cli / lifecycle
│   ├── transport/     #   gRPC + HTTP 健康探针 + pairing JWT
│   └── admin/         #   独立 FastAPI admin app + 路由（devices / personas / pairing / chat_test）
│
└── config/            # 共享 Pydantic settings（不在 app/ 下，所有层都读）
```

**严格分层契约**（由 [`import-linter`](https://import-linter.readthedocs.io/) 在 CI 强制）：
- `core ← domain ← infra ← app`
- core 对兄弟模块零依赖
- domain 不允许 import infra/app
- infra 只能 import core/types 中的类型，不可 import domain 业务
- 测试文件豁免（可自由 import 任何层来构造 fixture）

## 3. 热路径

```
gRPC Chat
  ↓
TurnEngine.run(input):
  1. input_guardrail.check          [<2ms]
  2. triage.classify                [<1ms]
  3. if COMPLEX_LONG: NATS publish + 返回 "I'll handle that"
  4. ContextCompiler.compile:
       persona prompt + memory recall (200ms timeout) + recent history (N=20) + realtime digest
  5. yield STATE("speaking")
  6. async for chunk in llm.stream(...):
       yield DELTA(chunk)
       if tool_call: ToolDispatcher.dispatch_serial(call)
  7. output_guardrail.check         [<5ms]
  8. yield DONE                     ← 用户在这里看到完整回复
  9. asyncio.create_task(_post_turn) ← 后台异步：SQLite 写 + NATS fanout + persona 交互事件
```

**目标延迟**：除 LLM 外热路径 < 250ms（其中 200ms 为 memory recall 预算）。

## 4. 5 个 Port —— hex-arch 边界

| Port | 抽象 | infra 实现 |
|---|---|---|
| `LLMPort` | 流式 LLM 调用 | `LLMRouter` + `LiteLLMProvider` / `FakeLLM` |
| `MemoryPort` | 外部 eidolon-memory（读 MCP / 写 NATS） | `EidolonMemoryPort` |
| `EventBus` / `KVStore` | 发布订阅 + 键值 | `NatsEventBus` / `NatsKVStore` / `InMemoryEventBus` |
| `ToolPort` | LLM 可调用工具 | 内置 `GetTime` / `EmitEvent` |
| `*Repository` | 4 个 SQLite 数据访问 | `Sql{ChatMessage,Conversation,Device,EvolutionHistory}Repository` |

domain 代码只 import `core/ports`，**不知道也不关心**对面是 LiteLLM 还是 Fake、NATS 还是内存。bootstrap 是唯一一处装配具体适配器的地方。

## 5. 快速开始

```bash
# 安装依赖
uv sync --extra dev

# 一次性初始化（SQLite 建表、proto 重生成、运行时目录创建、config.yaml copy）
./deploy/dev/init.sh

# 启动本地 NATS（JetStream）
./deploy/dev/run_nats.sh start

# 启动 eidolon-agent（gRPC :50051 + 健康 HTTP :8080 + admin HTTP :8081）+ admin web Vite (:5281)
./deploy/dev/run_all.sh start

# 端到端模拟 LiveKit 调用
python scripts/livekit_sim.py "你好"

# Admin Web 聊天测试
open http://127.0.0.1:5281/chat-test
```

## 6. 测试 & CI 卡点

```bash
# 全量测试（unit + functional + integration + architecture）
.venv/bin/pytest --strict-markers

# 按 marker 切片
.venv/bin/pytest -m unit         # 纯单元，最快
.venv/bin/pytest -m functional   # 模块级，可用 in-mem adapter
.venv/bin/pytest -m integration  # 跨模块，FakeLLM + InMemoryEventBus
.venv/bin/pytest -m "not smoke"  # CI 默认（排除真实外部依赖）

# 覆盖率（CI 卡 77%）
.venv/bin/pytest --cov

# 分层契约（4 个 import-linter rule，CI 强制）
.venv/bin/lint-imports

# 风格
.venv/bin/ruff check .
```

**当前数字**：222 tests pass · 77.2% project coverage · 4 import-linter contracts kept · ruff clean

测试与生产代码**同位**：每个模块下有 `tests/{unit,functional}/`。跨模块集成测试在根 `tests/integration/`，架构契约测试在 `tests/test_architecture.py`。

## 7. License

MIT.
