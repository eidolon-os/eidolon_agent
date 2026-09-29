# 参与决策 LLM 兜底：实现与开发集评测

## 状态

Agent 实现完成，**默认关闭，未部署、未重启共享服务**。实际模型语义质量未达到
产品开放要求，不能把“LLM兜底”当作可靠的补全器直接上线。

配置在 `participation` 下：`llm_fallback_enabled: false`、`primary_timeout_ms: 1500`、
`fallback_timeout_ms: 3000`。两阶段仍在原请求总时限内；合法wait/finish不兜底，
拒答及白名单可恢复故障只兜底一次，取消、权限和快照错误停止。候选、动作、快照
经SDK校验；CoordinationSession仍拥有当前版本/取消epoch/实际发言许可的检查。

LLM只提议动作、单一候选ID、澄清原因或abstain。自由台词/自由指令不进入Proposal；
澄清采用p4固定有界任务（Models pin_profile.py 1fe1f4d），说明其来源而不运行时导入训练代码。
规则依据LABELING.md 6fef9cd；原始两版提示词保留在本目录。LLM复用现有LLMPort与模型路由。
Router转发实际provider model_id，并在关闭迭代器时同步关闭provider流；决策适配器在
输出无效、预算耗尽、取消时也关闭流，不新增HTTP客户端或另一套重试。

## 输入与方法

Claude Code交付的p4 NPU **临时部署形态实例** p-dev回放：623决策点，其中65条拒答；
并非已确认的正式新发布。以既有Models `service_check.requests_for(seed=7)`导出SDK请求，
按decision_id连接primary响应。文件哈希与导出来源见input-lineage.json。不复制Models数据集。
没有使用封存p-test。每次模型调用仅包含当前公开快照，不含金标、后续事件或其他决策点。
额外控制组为规范顺序中前10个已决定wait与前10个已决定finish，用于模拟主服务不可用的风险。

使用Mac产品配置的`openai/deepseek-v4-flash`，模型预算3秒，temperature=0；没有操作生产发言。
每组按顺序执行一次，不能据此判断运行方差或生产p99；模型端到端之外的Laya、TTS及播放耗时不包含。

## 结果（全部错误与无效输出保留）

| 版本 / 样本 | 动作/端到端正确 | 应停却发言 | 错误响应 | p50 / p95 / max ms |
| --- | --- | --- | --- | --- |
| v1 / 65拒答 | 53/65 | 12 | 0 | 829.5 / 1073.3 / 1731.1 |
| v1 / 20控制 | 17/20 | 3 | 0 | 829.15 / 1082.7 / 2565.8 |
| v2 / 65拒答 | 56/65 | 6 | 1 | 884.8 / 1157.3 / 1476.4 |
| v2 / 20控制 | 19/20 | 1 | 0 | 786.75 / 1204.2 / 1263.3 |

v1暴露大量已完成问答仍继续发言。v2澄清运行时语义：finish只结束本轮，不关闭会话，
无需等用户告别；不把潜在补充当作未完成请求。没有改金标、没有按具体名字/话语加规则。
v2仍有6次过度发言、1次finish判wait、1次clarify判respond、1次非法输出。
两组已用于诊断/提示修订，不是独立验收。v2输入token中位数1335（v1为1237），
不能沿用“每次3k token”的估计；缓存、输出用量在逐条结果里。

## 验证与后续

相关自动化回归133项通过：结构化输出、上下文只读、候选与allowed_actions检查、
多提案/未完成流拒绝、超时/取消关流、默认关闭和注入资源所有权、迟到兜底不能获得发言许可。
本次NPU回放已接受样本没有超过1500ms，但不能据此保证现场不会超时。

下一步：完成度语义先以未参与提示调整的独立合成场景验收，分别统计应停误发言与应说漏发言，
再评测实际正式部署版本的拒答集和服务故障路径。不能以提高覆盖率牺牲用户的停止意图。
不通过时保持关闭，不补固定轮换、关键词拒绝表或延长预算掩盖质量问题。
家居续接另行推进：交付报告确认当前还没有同时通过单句回归的候选模型或NPU服务，
不会将其误认为现网已经支持上下文。

## 复现入口

`scripts/eval_participation_fallback.py --cases <export.jsonl> --settings <agent.yaml>
--env-file <existing.env> --output <new-directory> [--abstained-only]`

输入每行{id,request,gold,primary}，request/primary遵循SDK；gold只在模型返回后评分。
出站请求不含gold，输入全量校验后才发起调用。运行时凭据不进入证据目录。
