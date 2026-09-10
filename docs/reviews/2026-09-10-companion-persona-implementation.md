# Companion / Persona 执行记录

日期：2026-09-10。范围：SDK、Data、Admin、Agent、Mobile 的本地工作区，以及已配置模型的合成场景验证。本轮已按精简范围完成并分别提交；未推送、未部署、未修改用户数据库。Mobile 原有设备路由评审文档改动保留。

## 提交记录

| 仓库 | 提交 |
|---|---|
| SDK | `2239fd3`（此前 `23ad317`） |
| Data | `3bb4ed2`（此前 `40bf867`） |
| Admin | `3e9d2b2`（此前 `470cdb8`） |
| Mobile | `c3581ae`（此前 `a832cac`） |
| Agent | 本执行记录与 Agent 实现同次提交 |

其他任务的架构分析文档、Mobile 设备路由评审改动未纳入这些提交。

## 已交付

- **编辑正确性**：`PersonaAuthoring` 作为 patch，在完整 base genome 上合并；省略字段保留，显式空列表/空字符串清空。隐藏的关系阶段、trait metadata、signature phrases、memory/evolution policy 不被重置。
- **并发与重试**：人格版本和偏好版本同时校验；操作 ID、请求指纹、不可变结果引用及偏好快照在同一事务保存。重复请求返回原结果，即使之后又发生修改；相同 ID 不同请求返回冲突。无内容变化不增加 genome，但保留回执。
- **回复偏好独立存储**：`conversation_preferences` / `preference_revision` 位于 Companion 的 `runtime_config_json`，不是 genome。可选简短/适中/详细、主动建议、主动延伸话题。创建时与 Companion、初始 genome 同事务保存。
- **每轮策略**：首次人格实现传入正确媒介；每轮固定一次偏好快照；新增 `response_policy.v2` 易变段，稳定人格和 Harness 前缀不变。明确要求详细时完整回答；不默认附加建议和追问。
- **输出预算**：支持可选 `max_output_tokens`，默认不限制。`finish_reason=length` 标记 `output_truncated`，保留已有输出；未设置全局低 token 限制，也未增加二次 LLM 压缩或 TTS 截断。
- **改名和恢复**：改名在同一事务更新 Companion 名字并追加同名 genome；restore、rollback、reset 都使用追加版本语义。恢复保留当前名字和独立存储的回复偏好；改名和恢复通过同一个双版本、操作 ID 写入协议；历史显示被恢复版本，lineage 指向恢复前当前版本。
- **Data 命令边界**：写入由 `PersonaService` 承担；Repository 保留读取；旧读写 API 由服务层兼容门面组合，未引入 Repository → Service 反向依赖。
- **演化保护**：SDK 提供共享校验，Data 在提议与批准时再次执行。检查 enabled、证据、身份/关系保护、策略不被反思改写、trait 集合与变化幅度、关系阶段变化、当前 base。批准不能通过省略 expected base 绕过陈旧检查。
- **Mobile**：名字/起点/一句话画像 → 确认与回复偏好，两步创建；详细字段折叠。Data 提供三个带 revision 的起点和静态对话示例，明确示例不反映自定义修改。创建提交实际起点内容；创建与编辑支持真实模型独立试聊，旧预览随草稿修改失效；编辑冲突保留草稿，读取最新内容后显示冲突，用户检查后再保存。

## 当前字段归属和生效点

| 内容 | 权威存储 / 写入者 | 读取者 | 生效点 |
|---|---|---|---|
| 名字 | Companion + 新 genome，Data 同事务 | 管理界面 / 新运行快照 | 界面保存后；人格身份下次对话 |
| 性格、语气、价值观 | 不可变 genome / Data persona commands | Agent Realizer | 下次对话；旧会话继续 pin |
| 回复详略、建议、追问 | Companion runtime config / Data edit command | 每轮 pinned genome 读取时一并取当前偏好 | 下一轮开始；不改变正在流式输出的一轮 |
| 情绪、精力等短期状态 | Agent 进程运行态 | Realizer volatile context | 按现有衰减和更新机制 |
| 用户事实、具体承诺 | 现有 Memory / ActiveCommitment | Memory adapter / 执行链路 | genome 与表单已删除 commitments、pinned_facts、owner_preferences；不迁移旧数据 |
| trait 数值 | genome | 校验、诊断；不新增数值→prompt 映射 | 不宣称数值变化自动产生人格成长 |
| 工具权限 | 既有 Harness / runtime policy | TurnEngine | 原有权限边界；persona prose 不授予权限 |

## 管理 API 变化

`GET /api/management/v1/companions/{id}/persona` 返回：

```json
{
  "genome_id": "g_current",
  "display_name": "小南",
  "companion_revision": 2,
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

同一 PUT 请求支持 `action=edit|rename|restore`。改名提交 `display_name`，恢复提交 `restore_genome_id`，两者 persona 为 `{}`，不能夹带其他编辑。旧的裸改名、恢复 HTTP 入口返回 410，停止写入。恢复历史只追加新版本，当前名字和回复偏好不回退。

`POST /persona-preview` 接收当前 name/persona/preferences/text。编辑已有伙伴时携带 companion_id/base_genome_id；Agent 按已认证 Owner 读取完整 base 并检查版本，叠加草稿。创建草稿使用默认完整 genome。两者都使用生产 Compiler/Realizer/Harness，不创建 Companion、运行状态、历史或记忆，不执行工具；返回 draft_digest、回复和截断标记。每次独立单轮，30 秒生成超时，试聊不可用仍可保存。

OpenAPI、Dart、TypeScript 契约已重新生成并验证。**这是编辑接口的破坏性升级，SDK / Data / Admin / Mobile 必须协调发布**；旧客户端不能继续按旧的裸 authoring 请求保存。按用户最新指示，不做旧数据迁移；直接删除旧字段与旧读取，移除迁移审查脚本。含旧字段的 genome 不再符合新合同，没有兼容转换；本轮没有清理实际数据库。

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

- Data 本地虚拟环境全量：199 passed。
- Agent Persona / Compiler / Admin / E2E 定向回归：141 passed，包含版本化改名/恢复、旧操作延迟重试、双版本冲突和预览鉴权。
- Admin 最终全量：859 passed、5 skipped；管理接口定向回归 99 passed。
- SDK persona：11 passed；相关 Dart 合同检查重跑 21 passed。完整 SDK 初轮有 411 项通过，另 3 项因沙箱阻止 Flutter 缓存更新失败；三项均在权限允许的重跑中通过。
- Mobile 全量：996 passed、5 skipped；新增草稿预览 2 项通过，验证草稿变更后丢弃迟到回答、输入修改后清空结果、失败允许重试。最终静态分析无问题。
- Admin Web 类型检查与生产构建通过；OpenAPI、Dart、TypeScript 生成一致性检查通过。
- 修改文件的 Ruff 与 git diff whitespace 检查通过。Agent 已有的 import-linter `domain.agent.companion → infra.observability` 违规不在本次修改范围。

新增真实模型试聊使用三个起点 × 两个合成问题：全部 6 次正常结束，无 length 截断。普通情绪回应分别为 24、12、17 字符；明确请求详细解释时分别为 718、704、660 字符。这里只验证表达长度和链路可用性，不作为知识事实准确性或音频性能评分。结果见 [试聊记录](./persona-preview-smoke.jsonl)。

## 本轮范围收尾

用户明确不迁移旧数据、直接修改、不要过度设计。据此调整原计划：

- 取消旧数据迁移与兼容层，直接收敛字段归属，删除审查脚本。
- 完成对外编辑、改名、恢复的统一 CAS 与持久回执；Mobile 重试保留原操作 ID，冲突不自动覆盖。
- 完成真实模型草稿试聊；不引入独立预览存储或第二套 prompt。
- 自动演化默认 `auto_apply_low_risk=false`，保留现有提议/审批校验。本轮不增加自动成长消费者，不将其列为当前上线所需功能。未来若启用，仍须独立验证证据、长期一致性和撤回能力。

本轮代码范围已收敛。部署和真机语音验收仍未执行；三种通话模式、Memory 健康/降级、连续 10 轮与 TTS 播放的实际表现不能用单轮文本模型试聊替代。既有首音 6 秒、Channel 等待 30 秒、Local TTS 单批 60 字符指标没有因本轮而改变。
