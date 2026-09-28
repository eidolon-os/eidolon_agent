# 家居命令理解与澄清（2026-09-28）

## 本次定位

21:11–21:12 的真机测试中，Laya 首两轮超过 800ms；原因可能与 Mac
训练资源竞争有关，本次不修改推理运行时或放宽置信度阈值。

使用同一设备候选与 DeepSeek 对修改前的适配器做仅提案复现：
“打开客厅灯”返回了正确的 `living.main_light / on_off.on`，但同时返回
`mention="打开客厅灯"`。SDK 的 Proposal 明确禁止在 resolved/ambiguous
时携带 mention，旧适配器吞掉 ValidationError 并返回 None，最终错误地
提示用户换个说法。“关闭主灯”的候选提案也复现了同类错误。

修复保持 SDK 校验严格，不丢弃非法字段来强行执行：工具 Schema 从正式
Pydantic 契约生成，提示词说明跨字段限制；格式错误、截断、多工具输出、
服务错误均有明确诊断，与模型主动弃权区分。协议/服务错误显示理解服务
暂不可用，不伪装成用户表达不清楚。

## 边界

- Agent/domain/smarthome：命令决策、置信度门控、短期上下文、执行前校验。
- Agent/app/smarthome：按 Owner + device + voice session 管理、串行化会话。
- Agent/infra/smarthome：共享 LLMPort 的家居适配器，三个互斥结果工具：
  propose_home_action、ask_home_clarification、cancel_home_command。
  工具只提出结果，不直接操作设备。
- Models：仍仅负责 Laya 推理运行时；不保存家居会话或调用设备。
- Channel：复用已有 runtime session identity，转发文本、结果与结束事件；
  设备执行继续由 Capability Runtime/Provider 承担。
- SDK：在 VoiceResult 新增 clarification，无执行模板；ambiguous 继续复用
  原有候选设备和命令模板。客户端必须支持新增结果类型后再启用该流程。
- Korvo：复用 HomePanelView 结果卡片展示问题；既有 RGB 直接 DMA 不变。
  当前家居通道不合成语音，问题显示在屏幕上。

## 会话规则

首轮使用 Laya；超时、弃权、低置信度由 LLM 兜底。近期上下文存在时，
直接交给支持上下文的 LLM，避免单轮训练的 Laya 将“客厅那个”判为无关。
每个提案都重新校验当前设备目录和命令能力，LLM 不持有执行权限。

只保留最近一次成功操作/查询，或待澄清的话语、动作和问题；不接入陪伴
记忆。上下文 30 秒过期。取消、无关话语、失败、会话结束清除上下文；新
voice session 不继承旧数据。传输未正常关闭时由 TTL 兜底。会话存储有
容量上限，旧调用者未传 session_id 时仍为单轮，不按 device 猜会话。

已知房间来自注册表的 placement；未配置则保持未知，不擅自指定客厅。
缺少目标/动作时提问，用户明确房间优先；context 中的历史内容不能覆盖
当前明确的新指令。多轮澄清是语义理解，技术故障不会触发伪澄清。

## 验证与实屏验收

真实 Laya + DeepSeek，执行端用测试替身隔离副作用，已验证：
- 关闭主灯 → 候选 → 客厅那个 → 关闭客厅主灯。
- 打开它 → 再暗一点 → 相同对象的开灯和亮度调整。
- 打开灯 → 候选 → 算了 → 清除，之后“客厅那个”不复用旧动作。
- 客厅灯 → 询问动作 → 打开 → 正确提案。

自动化覆盖非法提案、原始 mention 错误、未完成输出、候选越界、会话
隔离/关闭/过期、取消及无关话语、Channel 生命周期、SDK/固件消息互通。

真机验收：进入家居 voice room，依次测试上述序列；退出重进后说“客厅
那个”不得执行旧操作；等待超过 30 秒后再说代词不得使用过期对象。
确认结果卡片、候选和设备状态变化一致，同时观察无闪点/上下位移。

逐轮追踪使用 turn_id：Agent 日志包含理解路由、提案或错误、最终
VoiceResult 和耗时；Channel 保留 ASR 最终文本、turn_id 和发布结果。
