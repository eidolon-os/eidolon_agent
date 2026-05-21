# eidolon-agent

> 桌面陪伴中控大脑。被 LiveKit 语音 pipeline 通过 gRPC 当作 LLM 调用；对接外部
> `eidolon-memory` 服务（MCP+NATS）；通过 NATS 与"情感进化""工作站智能体"协作。

工业级第一梯队的实时陪伴中控。完整架构见
[`.claude/plans/1-users-manson-ai-eidolon-eidolon-daemo-hidden-prism.md`](.claude/plans/1-users-manson-ai-eidolon-eidolon-daemo-hidden-prism.md)。

## 1. 这是什么

- **不是** LiveKit client。它**是**被 LiveKit 调用的 LLM 服务。
- **数据面**：gRPC bidi（同机自动 UDS），暴露 `Chat / PushSignal / SubscribeProactive / ExchangePairingCode`。
- **控制面**：FastAPI HTTP，给 Admin Web 与运维使用。
- **持久化**：SQLite3（WAL，aiosqlite + SQLAlchemy 2.0 async），可零成本切 Postgres。
- **总线**：NATS（pub/sub + JetStream + KV），完全取代 Redis。
- **人格**：YAML 模板（基因，Git 只读）+ 实例 Overlay（演化叠加，Git+DB 双写）。

## 2. 关键模块（顶层）

```
eidolon_agent/
├── core/             # Ports + 共享类型（零三方依赖）
├── transport/        # gRPC + HTTP + Pairing
├── agent/            # Turn 管线 / Triage / FSM / Registry
├── context/          # ContextCompiler + Providers
├── brain/            # LLM Provider 适配 + Router + 流
├── memory/           # MCP read + NATS write
├── history/          # 中心化对话历史 + fanout
├── mind/             # 心智状态（Mood/Energy/Attention/Bond）
├── persona/          # Template + Overlay + Resolver + Evolution
├── tools/            # Tool 注册 + 并行调度
├── dispatch/         # 复杂任务转发到工作站智能体
├── hooks/            # 生命周期钩子
├── events/           # NATS EventBus + KV
├── proactive/        # 主动行为
├── guardrails/       # 安全边界
├── signals/          # 多模态信号融合
├── session/          # 会话/对话/轮次
├── persistence/      # SQLAlchemy ORM + Alembic
├── runtime/          # Bootstrap / DI / Lifecycle
├── plugins/          # 第三方插件入口
├── observability/    # logging / metrics / tracing
└── config/           # Settings
```

## 3. 快速开始

```bash
# 安装依赖
uv sync --extra dev

# 一次性初始化（SQLite 建表、admin token、git init personas）
./deploy/dev/init.sh

# 启动本地 NATS（JetStream）
./deploy/dev/run_nats.sh start

# 启动 eidolon-agent（gRPC:50051 + HTTP:8080）+ admin web（Vite dev :5281）
./deploy/dev/run_all.sh start

# 端到端模拟 LiveKit 调用
python scripts/livekit_sim.py
```

## 4. License

MIT.
