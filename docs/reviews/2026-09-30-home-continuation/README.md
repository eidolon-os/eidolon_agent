# c4 家居续接接入与影子验收

## 结论

Agent 接入完成，默认关闭，**未部署、未修改产品8771或OPi服务**。
173项相关回归通过，4项import-linter架构契约通过。真实 c4 MPS + 产品
`openai/deepseek-v4-flash` + 虚拟Provider，14组合成场景通过13组。
**转述开门仍被LLM解释为解锁并提交给虚拟执行器，整体产品验收未通过。**
不能以续接准确、测试全绿或提示词已修改宣称该风险已解决。

## 架构与调用

- 复用HomeContext；30秒有效期、会话版本、目录复查和执行回执仍由Agent维护。
- pending保存真正展示的可执行候选和顺序、结构化动作及实际问题；成功操作/查询保存实际答复。
- LayaHomeContinuation实现已有SmartHomeFallbackPort的提案形态，借用LayaInterpreter的HTTP客户端。
  单句与续接共用有总超时和256KiB响应上限的传输；不新增连接池、SDK协议或模型内会话状态。
- pick只开放有结构化动作和2–8个候选的已发出选择题；follow只开放1–3台有效焦点。
  门锁、传感器、场景焦点不进入续接；选项为空、失效焦点、问题顺序不符转交LLM。
  当前显示不能区分的重名候选、被截短的问题也转交LLM，不猜“第一个”。
- 按冻结契约使用所选选项的probabilities值：执行≥0.95、pick取消≥0.5。
  follow没有取消；重新理解、低置信度、模型revision不符、截断、超时均交回LLM一次。
- 完成/取消经过同一当前版本检查；超时后到达的结果或已过期焦点不能直接执行。
  数值和动作仍由既有lexicon解析、SDK校验、HomeActuator/Hub执行链路验证。
- 单句0.8及严格规则一致性门禁保持原逻辑。

配置 `EIDOLON_SMARTHOME_LAYA_CONTINUATION_REVISION=7b695ba8` 显式启用；必须同时
配置Laya模式和真实LLM。未设置即关闭；不根据服务自报版本自动开放能力。
旧 `interpretation/LayaInterpretationAdapter` 已有revision参数，显式传7b695ba8可用，
本次新增回归证明；r14默认保留，避免将现有产品影子调用全部拒收。

## 真实模型结果与局限

`cooperation-v2.json` 包含全部输入、返回、虚拟命令、上下文和计时。金标仅供离线评分，
不传模型。14组以命令目标/动作/参数及非失败结果评分：

- 窗帘打开→关闭→打开它：三次正确执行，续接没有调用LLM。
- 打开空调→主卧那个→再高两度：LLM产生结构化选择题，Laya接管选择与+2°C。
- 打开空调→算了：LLM澄清后Laya取消，未提交设备命令。
- 主灯打开→再调暗一点→调到50%：Laya续接，参数-10、50正确。
- 换设备、引用、假设、讲述过去、明确请求和否定对照：除下项外符合预期。
- 转述送水者要求开门：Laya正确交还LLM，但LLM仍提交unlock。修改前后均复现。

7次成功续接在本机MPS为22.12–51.62ms；这是合成目录、虚拟回执的小样本，
**不是OPi NPU数据、真实Hub执行耗时、语音端到端或p95/p99保证**。
初始6组探针用于发现问题；后续14组包含已见反例，不称为完全独立的盲测。

LLM提示增加了通用的“当前用户请求与转述/引用/假设的区别”，未增加关键词拒绝表。
该改动仍没修好反例。产品基线文档记录第一期不加开锁二次确认，本次没有擅自添加确认流程。
进一步开放前须解决/明确该意图边界并通过独立对照，不以Laya阈值调整替代LLM问题。

## 对Models交付的复核与后续

Models 5d3cc2b的“v2-dev 4条误执行”列表包含设备出口；“27条无关误触发”缺少设备/动作
分数，且未包含Agent严格规则一致性与LLM路由。它们不能直接解释为实际误执行次数，
也不足以单独证明生产0.8阈值适用。需要按decision ID提供完整输出再对齐统计口径。

Claude Code后续：交付c4 RKNN制品和同契约的NPU逐点比较、与p4并发时800ms预算内的
延迟/超时/拒答数据；当前7b695ba8/npu目录仅看到ONNX等导出文件，未验证RKNN服务。
不要调整参与8773或Agent路由；共享服务重启前协调。
Codex后续：解决LLM意图误判、补独立产品语义验收，再考虑配置发布与真机链路验收。
本轮没有发现需要新增Laya训练样本才能解释的续接失败，不把LLM问题转交给Laya训练。

复现（仅虚拟执行，使用现有产品凭据；先独立启动18771）：

```sh
.venv/bin/python scripts/eval_home_cooperation.py \
  --endpoint http://127.0.0.1:18771 \
  --settings ../.eidolon/mac-product/config/settings/agent.yaml \
  --env-file ../.eidolon/mac-product/config/env/agent.env \
  --output /tmp/c4-home-cooperation.json
```
