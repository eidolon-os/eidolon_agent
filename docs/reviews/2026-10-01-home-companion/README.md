# 家居交互终端与 Companion 统一（2026-10-01）

## 产品决定

用户确认：Korvo 等直接与用户交互的终端显式选择 Companion。家居交互仍交给独立 SmartHome Agent，不必加载伙伴聊天的完整 prompt 或 memory。此决定取代 2026-09-25/26 Korvo 方案中“家居交互不挂伙伴”的决定；无 TTS、面板反馈及智能家居独立验收范围保持不变。

Owner 归属继续决定设备控制权限；Companion Assignment 决定默认由谁承接交互。配网、认领、管理通道及面板状态同步不以 Companion 为前置条件；启动语音交互需要已有默认绑定，或现有机制允许的显式同 Owner 会话目标。没有隐藏 Owner 默认伙伴，也不创建系统伙伴。

## 实现边界

- SDK：`HomeSessionScope` 统一 owner / companion / device / session，`HomeCommandRequest` 追加 turn / utterance。Channel 与 Agent 共用同一严格契约；匿名、无 session 的旧入口删除。
- Channel：家居会话复用 `ChannelRuntimeServices`、Kernel BodyAssignment 与 System Data Runtime Authority。解析和匹配身份后才构建 STT；身份不成立时走既有启动失败和 session_end 流程。家居 dispatch 不再禁止 Companion 目标，仍不能成为多角色团队会话。
- Agent：复用 `RuntimeSessionAuthorizer` 每轮核对有效 Companion；只读取身份和配置事实，不进入伙伴 `TurnEngine`、prompt 编译、记忆检索或记忆写入。家居上下文固定在 Owner/device/session 对应的 Companion 上，同一 session 中换 Companion 或结束另一 Companion 的上下文会被拒绝。
- 家居 domain、Models、Hub/Provider：Laya 适用条件、续接、一次 LLM 兜底、提案校验、执行与回执路径不变，没有新增模型路由或训练补丁。
- Kernel / Admin / Mobile：继续使用已有 BodyAssignment 与“由谁应答”配置。Kernel 派生 Body 的 optional 表示管理和绑定事实可以独立存在，交互入口仍要求明确目标；本次没有扩展为所有传感器/执行器必须绑定人格。

`home.command.v1` 保留为独立处理器选择，和 Companion 身份正交。为保留快速家居路径，不把命令强行送入当前总会编译聊天上下文、调用 LLM 的伙伴回合。没有新服务、第二套身份/绑定库或通用插件框架。手机及 Korvo 的 wire 声明无需修改。

会话内身份固定，绑定变更在新会话解析；沿用现有 Companion 会话规则，不让换绑定直接把旧上下文迁给另一伙伴。Companion 停用则下一轮由 Runtime Authority 拒绝。此改动没有新增绑定变更主动终止在途会话的机制。

## 验证

- SDK 家居契约：63 项通过，包含无效身份、匿名请求、不可变作用域及额外 memory 字段拒绝。
- Channel 家居会话、共用身份解析与 LiveKit adapter：199 项通过，包含无绑定、跨 Owner/device 拒绝，授权解析、显式目标传递、启动失败、闲置结束及无 LLM/TTS 构造。
- Agent 家居回归、Admin 认证及架构约束：128 项通过，包含每轮身份核对、伙伴停用、跨 Owner 拒绝、旧匿名入口拒绝、上下文固定及 HTTP → 既有家居 domain → 虚拟执行器两轮开关窗帘。另验证身份核对等待期间新输入立即作废旧提案，不因新增网络读而恢复过时指令执行。
- Ruff 与 diff 检查通过。

以上为软件边界和虚拟执行验证，没有宣称真实 Laya/LLM 准确率、NPU 长尾或麦克风端到端性能已通过。

## 发布与真机验收

SDK、Agent、Channel 必须作为同一固定版本发布；旧 Channel 不会发送 companion_id，不能只更新 Agent。沿用 Ops 发布流程，检查统一 SDK 安装及运行 fingerprint，验证失败时整体回滚。

本次配套提交：SDK `c375645`、Channel `e68c152`；Agent 为包含本记录的提交。没有推送或部署。

当前 Ops 工作区存在并行的未提交改动，本次没有混入或修改这些文件，也没有重启产品服务。只读确认 opi5max 产品服务正在运行；旧 Korvo 实例的 Mount 已 inactive，当前另有四个 active Mount 均有绑定，不能把旧实例当作当前 Korvo，也未擅自改动任何 Assignment。待发布工作区稳定后，核对当前 Korvo 在手机中显式选择的 Companion，再验证：未绑定拒绝 → 绑定后单句/续接 → 正常结束重进 → 更换 Companion 后新会话上下文隔离；分别记录身份解析、Laya/LLM 与设备回执耗时。Agent 已在现有逐轮日志追加 authority_ms，便于区分本次身份核对开销。

客户 demo 的语义正确性及性能门槛保持原验收要求，已有训练/语义失败不因身份统一而视为解决。
