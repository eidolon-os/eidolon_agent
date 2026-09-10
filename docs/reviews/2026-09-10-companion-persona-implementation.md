# Companion / Persona 执行记录

日期：2026-09-10。范围：SDK、Data、Admin、Agent、Mobile 的本地工作区，以及已配置模型的合成场景验证。已按仓库分别提交；未推送、未部署、未修改用户数据库。Mobile 原有设备路由评审文档改动保留。

## 提交记录

| 仓库 | 提交 |
|---|---|
| SDK | `23ad317` |
| Data | `40bf867` |
| Admin | `470cdb8` |
| Mobile | `a832cac` |
| Agent | 本执行记录与 Agent 实现同次提交 |

其他任务的架构分析文档、Mobile 设备路由评审改动未纳入这些提交。

## 已交付

- **编辑正确性**：`PersonaAuthoring` 作为 patch，在完整 base genome 上合并；省略字段保留，显式空列表/空字符串清空。隐藏的关系阶段、trait metadata、signature phrases、memory/evolution policy 不被重置。
- **并发与重试**：人格版本和偏好版本同时校验；操作 ID、请求指纹、不可变结果引用及偏好快照在同一事务保存。重复请求返回原结果，即使之后又发生修改；相同 ID 不同请求返回冲突。无内容变化不增加 genome，但保留回执。
- **回复偏好独立存储**：`conversation_preferences` / `preference_revision` 位于 Companion 的 `runtime_config_json`，不是 genome。可选简短/适中/详细、主动建议、主动延伸话题。创建时与 Companion、初始 genome 同事务保存。
- **每轮策略**：首次人格实现传入正确媒介；每轮固定一次偏好快照；新增 `response_policy.v2` 易变段，稳定人格和 Harness 前缀不变。明确要求详细时完整回答；不默认附加建议和追问。
- **输出预算**：支持可选 `max_output_tokens`，默认不限制。`finish_reason=length` 标记 `output_truncated`，保留已有输出；未设置全局低 token 限制，也未增加二次 LLM 压缩或 TTS 截断。
- **改名和恢复**：改名在同一事务更新 Companion 名字并追加同名 genome；restore、rollback、reset 都使用追加版本语义。恢复保留当前名字、pinned facts 和 owner preferences；历史显示被恢复版本，lineage 指向恢复前当前版本。
- **Data 命令边界**：写入由 `PersonaService` 承担；Repository 保留读取；旧读写 API 由服务层兼容门面组合，未引入 Repository → Service 反向依赖。
- **演化保护**：SDK 提供共享校验，Data 在提议与批准时再次执行。检查 enabled、证据、身份/关系保护、策略不被反思改写、trait 集合与变化幅度、关系阶段变化、当前 base。批准不能通过省略 expected base 绕过陈旧检查。
- **Mobile**：名字/起点/一句话画像 → 确认与回复偏好，两步创建；详细字段折叠。Data 提供三个带 revision 的起点和静态对话示例，明确示例不反映自定义修改。创建提交实际起点内容；编辑冲突保留草稿，读取最新内容后显示冲突，用户检查后再保存。

## 当前字段归属和生效点

| 内容 | 权威存储 / 写入者 | 读取者 | 生效点 |
|---|---|---|---|
| 名字 | Companion + 新 genome，Data 同事务 | 管理界面 / 新运行快照 | 界面保存后；人格身份下次对话 |
| 性格、语气、价值观 | 不可变 genome / Data persona commands | Agent Realizer | 下次对话；旧会话继续 pin |
| 回复详略、建议、追问 | Companion runtime config / Data edit command | 每轮 pinned genome 读取时一并取当前偏好 | 下一轮开始；不改变正在流式输出的一轮 |
| 情绪、精力等短期状态 | Agent 进程运行态 | Realizer volatile context | 按现有衰减和更新机制 |
| 旧 pinned facts、owner preferences、关系约定 | 当前仍在 genome | 原 Realizer / Memory adapter | 尚未迁移；恢复人格不会复活旧 facts/preferences |
| trait 数值 | genome | 校验、诊断；不新增数值→prompt 映射 | 不宣称数值变化自动产生人格成长 |
| 工具权限 | 既有 Harness / runtime policy | TurnEngine | 原有权限边界；persona prose 不授予权限 |

## 管理 API 变化

`GET /api/management/v1/companions/{id}/persona` 返回：

```json
{
  "genome_id": "g_current",
  "persona": {"voice_portrait": "温暖、清晰"},
  "preferences": {"response_length": "brief", "advice": "when_asked", "follow_up": "when_needed"},
  "preference_revision": 1
}
```

`PUT` 提交请求：

```json
{
  "expected_base_genome_id": "g_current",
  "expected_preference_revision": 1,
  "operation_id": "client-retained-random-id",
  "persona": {"voice_portrait": "短句、直接"},
  "preferences": {"response_length": "brief", "advice": "when_asked", "follow_up": "when_needed"}
}
```

`preferences` 可以省略，`persona` 可以为空对象。请求必须携带两个读取到的版本，不允许服务端替陈旧客户端猜测。Admin 各层转发时保留字段是否省略的语义。新增 `GET /persona-presets`；原 template 接口保留。

OpenAPI、Dart、TypeScript 契约已重新生成并验证。**这是编辑接口的破坏性升级，SDK / Data / Admin / Mobile 必须协调发布**；旧客户端不能继续按旧的裸 authoring 请求保存。无 schema migration，新字段使用现有 JSON 存储；旧 Companion 未配置偏好时使用明确的简短默认值，原 genome 不被批量改写。

## 实际模型验证

使用项目配置的 `openai/deepseek-v4-flash`，合成人格 `gentle`，语音表达媒介，32 场景 × 两组，temperature=0。实际通过生产 Compiler 和配置的 LLM router；不连接 Memory，不执行工具，不采集麦克风、不写用户资产。

对照组使用同一 genome/Harness，仅移除 response-policy 段；因此衡量的是新增策略效果，不是旧版线上会话与整套新实现的对照。请求交替组别顺序；模型端缓存状态没有受控，不能称为冷/热缓存性能实验。

| 指标 | 无新增策略 | response_policy.v2 |
|---|---:|---:|
| 正常结束 / 样本数 | 32 / 32 | 32 / 32 |
| 超时 / length 截断 | 0 / 0 | 0 / 0 |
| 普通场景字符中位数* | 21 | 15 |
| 普通场景含问句的回答数* | 10 | 5 |
| 首个可见内容耗时中位数 | 1180 ms | 1185.5 ms |

*普通场景口径为排除 detail、advice、task；包含必要澄清，因此“含问句”不等于“多余追问”，也不是质量评分。

小样本首次策略仍出现模板化追问，据此收紧收尾规则到 v2。复测问候为“你好，我在。”，情绪回应为“累了就歇会儿吧，不用撑着。”；明确要求详细的解释仍给出三个例子。

完整样本中，建议请求仍能得到建议，明确三条建议会列出三条，翻译请求直接给译文；没有记忆的请求未编造已记得的具体事实。但面试建议、短信草稿和缺失记忆等回答仍有主动邀请补充的尾句。**行为改善已有证据，追问规则尚非完全可靠。**“我可以记住”也不能当作实际记忆写入能力验收。这一轮没有检查知识回答的每个事实，更未验证端到端语音体验。

产物：

- [32 场景 / 64 请求清单](./persona-policy-manifest.jsonl)
- [完整 A/B 输出](./persona-policy-evaluation.jsonl)
- [v1 小样本](./persona-policy-smoke-v1.jsonl) / [v2 小样本](./persona-policy-smoke-v2.jsonl)
- [可重复运行的脚本](../../scripts/bench_persona_response_policy.py)；默认只生成清单，`--execute` 才调用已配置模型。

## 验证

- Data 自己的虚拟环境全量测试：199 passed。此前借用 Agent 环境全量跑时触发事件循环 ResourceWarning；Data 本地环境最终全量通过。
- Agent Persona / Compiler / Turn / E2E / benchmark 定向回归：97 passed。
- Admin 管理、创建、权限与 persona 定向回归：93 passed。全量首轮其余 853 条通过、5 跳过；3 条旧 persona fixture 已迁移并包含在最终定向回归中。
- SDK 既有 persona 构建/投影测试：10 passed。
- Mobile 创建、人格编辑、请求契约、对话入口及三模式：67 passed；`flutter analyze --no-pub` 无问题。
- Admin Web 类型检查与生产构建通过；OpenAPI、Dart、TypeScript 生成一致性检查通过。
- Data 依赖方向测试通过。Agent import-linter 仍有既存 `domain.agent.companion → infra.observability` 违规，已核对 HEAD 中存在，本次没有新增该依赖。
- 新增临时 SQLite 测试验证隐藏字段、旧快照不可变、并发 CAS、延迟重试、同 ID 不同请求、偏好冲突、改名/恢复、创建偏好同事务及四种 Memory 分支下的 voice 规则。

## 尚未关闭的计划条件

1. **真实 Memory 迁移与停止双读**：未把旧事实直接提升为 Owner 共享记忆，也未把关系约定自动变成任务。提供 [只读迁移审查工具](../../scripts/audit_persona_legacy_fields.py)，对导出的 genome 标记原始路径、内容摘要、Companion audience、待确认冲突；不执行写入。正式迁移必须先读取目标 Memory、审查范围与冲突并核对回读，再移除旧读取。
2. **全部命令统一并发/重试协议**：author/edit 已有完整双版本与持久回执。restore/rollback/reset 已统一追加语义和事务；恢复、改名的对外 API 尚未全部携带等价的 expected base / operation ID，不能把所有命令的晚到重试保证一并宣称完成。
3. **设备与长期质量**：还需真实 Companion、不同起点、10 轮连续对话、Memory 健康/降级、三种通话模式、取消恢复与 TTS 跨批实测。首音 6 秒、Channel 实验 30 秒、Local TTS 单批 60 字符保持原有定义，未以文字模型结果替代验收。
4. **动态隔离试聊**：首版采用计划允许的静态示例；未来动态预览仍需同一 Realizer/Harness、草稿 digest 和无工具/记忆写入保障。
5. **生产自动演化**：没有新增自动批准或跨进程成长消费者。权威保护已补，但长期一致性、用户审阅/撤回和真实证据闭环尚未完成，不向产品宣称自动成长已启用。

这些项目保留在原计划的后续退出条件中；本记录不等于四批全部验收完成。
