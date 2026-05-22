# Personas 模块架构与执行计划

## Summary

`personas` 是独立人格模块，负责模板、实例副本、当前状态、prompt 编译、记忆消费、信号解释、异步进化、主动行为策略和审计。模板提供基础性格底色；用户绑定模板时生成一份完整实例 copy；后续长期变化都发生在实例 copy 上，不回写模板。短期 mood/energy/attention 属于 runtime state，不写入 instance YAML。

代码、模板和模块文档都放在 `eidolon_agent/personas/`。项目根目录不再保留 `personas/`。

## Directory Layout

```text
eidolon_agent/personas/
  __init__.py
  README.md
  types.py
  ports.py
  registry.py
  instance_store.py
  runtime_state.py
  signal_adapter.py
  memory_adapter.py
  compiler.py
  evolution.py
  worker.py
  proactive_policy.py
  service.py
  providers.py
  templates/
    caretaker_jiezhi.yaml

~/eidolon/personas/
  instances/
    <tenant>/<user>/<instance_id>.yaml
```

## Architecture

- `PersonaTemplate`：Git 管理的基础性格模板，不包含用户记忆，也不随用户进化。
- `PersonaInstance`：模板绑定到用户后的完整 copy，包含当前滑块、风格编译器、记忆适配策略和进化状态。
- `PersonaRuntimeState`：短期 mood、energy、attention，可衰减，不写入 instance YAML。
- `PersonaCompiler`：把实例状态、runtime state、召回记忆、实时信号编译为 LLM 可消费的 prompt。
- `PersonaMemoryAdapter`：把 memory recall 文本与 `MemoryHit.metadata` 转换为 persona-specific memory policy。
- `PersonaSignalAdapter`：把外部实时信号摘要转换为 persona runtime state 更新。
- `PersonaEvolutionEngine`：根据反馈、turn 事件、mock memory trigger 或 memory relation 自动进化实例 copy。
- `PersonaEvolutionWorker`：异步消费 interaction events，更新 runtime state 或长期人格滑块。
- `PersonaProactivePolicy`：只负责人格层面的主动行为意图；调度、节流、发布仍在 personas 外。
- `PersonasService`：唯一对外接口。其他模块不直接访问 registry、store、compiler、adapter 或 evolution engine。

## Public Interface

`PersonasService` 对外暴露：

- `list_templates()`
- `get_template(template_id)`
- `create_instance(tenant_id, user_id, instance_id, template_id)`
- `get_instance(tenant_id, user_id, instance_id)`
- `get_snapshot(tenant_id, user_id, instance_id, template_id=None)`
- `compile_prompt(tenant_id, user_id, instance_id, user_text, realtime=None, dry_run_memory=None)`
- `submit_interaction(event)`
- `submit_signal(signal)`
- `evolve_now(tenant_id, user_id, instance_id, events, dry_run=False)`
- `mock_memory_trigger(tenant_id, user_id, instance_id, user_text, memory_hits, apply=False)`
- `propose_proactive(tenant_id, user_id, instance_id, template_id=None)`

Admin HTTP 只通过 `PersonasService`：

- `GET /personas/templates`
- `GET /personas/templates/{template_id}`
- `POST /personas/instances`
- `GET /personas/instances/{tenant_id}/{user_id}/{instance_id}`
- `POST /personas/instances/{tenant_id}/{user_id}/{instance_id}/compile-preview`
- `POST /personas/instances/{tenant_id}/{user_id}/{instance_id}/evolve`
- `POST /personas/instances/{tenant_id}/{user_id}/{instance_id}/mock-memory-trigger`

## Ports

personas 可以使用 memory、LLM、events 和 audit，但不直接依赖具体基础设施。不提供 LLM 可调用的人格状态修改工具；状态只能由代码逻辑提交事件或信号来更新。

- `PersonaMemoryPort`：读取 memory recall。
- `PersonaLLMPort`：为未来反思、摘要、复杂进化判断预留。
- `PersonaEventPort`：发布 instance updated / evolution applied。
- `PersonaAuditPort`：记录 evolution history。

运行时由 `eidolon_agent` 现有 `MemoryPort`、`LLMRouter`、event bus、SQLite repository 适配注入。测试中使用 mock ports。

## Evolution Rules

长期进化异步落盘，但必须经过 guard：

- `identity_core` 不可被进化修改。
- knob 必须 clamp 在 `min/max` 内。
- 单次变化不得超过 `step_limit`。
- 高频 rule 使用 cooldown。
- 所有 apply 都记录 delta，可审计和回滚。

短期 runtime state 更新规则：

- mood / energy / attention 是当前状态，可衰减。
- 长期关系只使用 `behavioral_knobs.intimacy`，不再保留独立 Bond。
- turn 完成、实时信号、memory trigger、Admin/debug API 都转成 personas event。
- agent turn 不等待 worker 完成，保持响应速度。

## Testing Plan

`tests/personas/` 覆盖：

- template registry 加载与校验。
- instance store create/load/save roundtrip。
- template copy 后模板变化不影响已有 instance。
- compiler 将 knob range 编译为 deterministic prompt。
- memory adapter 消费 raw `MemoryHit.metadata`，包括 graph relation policy 和降级路径。
- evolution engine 自动应用事件、dry-run、不越界、cooldown、identity lock。
- runtime state 更新与 decay。
- async evolution worker 非阻塞提交、串行落盘和异常隔离。
- signal adapter 低置信度过滤和 runtime state 映射。
- proactive policy 只返回 persona decision，不测试 scheduler/throttler。
- `PersonasService` 所有对外接口。
- Admin endpoints 通过 service 工作。

集成测试覆盖：

- context provider 调用 `PersonasService.compile_prompt`。
- turn pipeline 使用 compiled persona prompt。
- turn pipeline 完成后提交 `PersonaInteractionEvent`。
- mock memory trigger 影响当前 prompt，并可选择 apply 触发自动进化。

## Verification

```bash
pytest tests/personas tests/context tests/agent
ruff check eidolon_agent tests
```

## Assumptions

- 不使用 schema 版本字段或版本化类型命名。
- `eidolon_agent/personas` 是代码模块。
- 设计文档随模块放在 `eidolon_agent/personas/docs`。
- 用户实例数据默认写入可配置运行期路径，不放进 package 代码目录。
- personas 对外只暴露 `PersonasService`。
- 不保留独立 `mind` 模块或 `set_mood` 工具。
- 基础设施仍在原模块：memory、LLM、events、transport、history、tools 框架和 scheduler 不迁入 personas。
