# eidolon-agent 作为 Eidolon OS 大脑：对标评审与优化方案

日期：2026-09-10；复核：2026-09-14 于 `eidolon_agent@bfe685d`。原基线 `7247e5d`。
复核改动见文中 **[09-14]** 标注；未标注的结论在 bfe685d 上重新核对仍成立。
性质：**代码评审与方案，不是已实施变更**。对标对象为同类顶级实现（实时语音助理、
陪伴类产品、记忆框架、Agent 框架、OS 级助理），用于定位能力坐标，不逐条复制它们的形态。

计划见 [`docs/plan/brain-optimization-plan.md`](../plan/brain-optimization-plan.md)——
回路归属与不变量、MCP 工具面目标形状、等待话术机制都已并入该计划，不再单独成篇
（长版见 `b07fb0b`）。

本文与 `2026-09-09-companion-persona-review.md` 互补：那篇聚焦 Companion/Genome/Persona
编辑链路的确定性缺陷（F1–F8），本篇聚焦**大脑本身的认知回路、能力面、顶层架构与工程底座**。
persona 编辑正确性问题不在此重复，只在依赖处引用。

---

## 0. 结论摘要

三个判断，按重要性排序：

1. **这不是一个「大脑」，是一个做得很好的「实时应答内核」。** 所有认知都发生在用户触发的
   一个 turn 内：拼 prompt → 流式生成 → 少量工具 → 落库。没有任何在 turn 之外运行的认知
   （巩固、反思、预期、主动）。顶级同类产品在 2025–2026 已普遍把「后台认知」当作一等公民
   （记忆巩固、sleep-time compute、异步任务、主动提示）。**这是与顶级产品最大的结构差距，
   也是「陪伴」产品叙事的根。**
2. **中期记忆有洞。** **[09-14 修正]** 评审时热路径 prompt 只有 4 条消息；`68f0f46` 后
   窗口已放宽到 20 条，降级窗口同为 20（`config/settings.py:314,317`）。
   **急性程度下降，结构性缺口不变**：滚动摘要的接缝在 compiler 里做好了，但
   `bootstrap.py:472` 仍未注入 `summary_provider`（已复核：bootstrap 全文 0 次出现）——
   生产中滚动摘要**不存在**，也没有压缩。于是分界线从「两轮以前」移到「十轮以前」，
   再往前的一切仍完全依赖外部 memory 服务在 ≤0.5s 内碰巧召回；召回失败时 harness 又禁止
   模型播报记忆机制（`domain/harness/realtime.py` 策略行），结果仍是**自信的失忆**。
3. **能力面几乎是空的：真实工具 3 个。** `get_time` / `get_weather` / `delegate_to_coworker`
   （`bootstrap.py:310-316`，`emit_event` 对模型隐藏）。`body_control` 默认 fail-closed
   （`bootstrap.py:97`），`RuntimeCapabilityToolProvider(None)` 是空的（`:323`）。
   作为「OS 的大脑」，它现在**不能定提醒、不能设闹钟、不能看屏幕、不能开应用、不能查资料**。

同时必须说清楚：**工程底座的质量明显高于同规模项目的平均线**，不建议推翻重做。
分层契约由 CI 强制、context 按易变性排序并有 ledger、committed-turn 落库边界、
打断后按「用户真正听到的字符」截断入库、记忆降级时显式告知模型、privacy 三态、
durable outbox、回放基准 —— 这些都是正确且罕见的工程决策。**方案是补回路，不是换骨架。**

---

## 1. 证据强度

- 阅读了 `core/` `domain/` `infra/` `app/` 全部非测试源文件的结构，逐行读了热路径
  （`domain/agent/turn.py`、`domain/context/compiler.py`、`app/runtime/bootstrap.py`、
  `app/transport/grpc/chat_servicer.py`）、guardrails、signals、harness、memory port、
  LLM router/provider、settings、topics、benchmark suites 索引。
- 用 grep 验证了「某能力是否真的接线」这一类判断（下文标注为**已验证**）。
- **未做**：未运行测试、未启真实服务栈、未看真实会话 transcript、未评测真实模型输出质量。
  因此本文中「延迟/质量」类结论是**机制层判断**，不是线上实测结论。
- 兄弟项目只做定向查证（`eidolon_sdk` 的 `biz/sense`、`biz/dialogue_control`；
  `eidolon_channel` 是否调用 `PushSignal`；各仓 README 边界），不是对它们的审计。

---

## 2. 大脑现在实际拥有什么（诚实清单）

| 能力 | 状态 | 证据 |
|---|---|---|
| 单轮实时应答（流式 + 工具循环 ≤4） | ✅ 完整 | `domain/agent/turn.py:146-560` |
| 上下文分段 + 易变性排序 + 预算裁剪 + ledger | ✅ 完整且优秀 | `domain/context/compiler.py`、`context/types.py` |
| 外部长期记忆读（≤2 次串行 query，共享一个 deadline） | ✅ 有，弱 | `compiler.py:736-870` |
| 承诺（commitment）只读 | ✅ 有 | `compiler.py:873-902` |
| 打断/barge-in 截断入库 | ✅ 完整 | `turn.py:596-607`、`chat_servicer.py:93-125` |
| 投机（speculative）预热轮 | ✅ 有，无策略 | `chat_servicer.py:168-172`、`turn.py:178-182` |
| privacy 三态（normal/private/temporary） | ✅ 有 | `domain/runtime_policy.py` |
| 每轮 TurnTrace + 回放 diff + live turn board | ✅ 有 | `core/types/trace.py`、`infra/observability/` |
| 长任务委派给 mementos coworker | ✅ 有 | `domain/tools/builtin/submit_long_task.py`、`infra/long_tasks/mementos.py` |
| 滚动摘要 | ❌ **接缝在，生产未接线**（09-14 复核仍然如此） | `bootstrap.py:472` 无 `summary_provider` |
| 实时多模态信号（PushSignal/SignalBus/Fuser） | ❌ **无生产者，等于死代码** | `eidolon_channel` 全仓无 `PushSignal` 调用 |
| `sense.*`（视觉：注意力/疲劳/在场） | ❌ **未消费** | agent 全仓无 `eidolon_sdk.biz.sense` 引用 |
| 主动对话（TurnTrigger.PROACTIVE） | ❌ **无任何生产者** | 全仓仅类型定义，`chat_servicer.py:216` 恒为 USER_UTTERANCE |
| memory 入站事件（promise_due 等） | ❌ **无订阅者** | `core/types/topics.py:68` 定义，全仓无订阅 |
| 人格演化写入 | ❌ 生产不可用（设计上待契约） | README §7、`bootstrap.py:220-233` |
| OS/设备动作 | ❌ fail-closed + 空 provider | `bootstrap.py:97,323` |
| 禁忌词/taboos | ❌ 恒为空 | `bootstrap.py:500` `taboos_provider=lambda: tuple()` |
| metrics / OTel | ❌ **只有配置项，无实现** | `config/settings.py:212-217`，全仓无 exporter |
| 限流 / 准入控制 | ❌ 无 | `EIDOLON_RATELIMIT` bucket 建了但无人用 |
| 成本核算 | ❌ 无 | 只有 usage token 计数 |
| 输出长度上限 | ❌ **从不传 `max_tokens`** | `turn.py:304` 调用未带该参数 |

---

## 3. 对标坐标系

对标对象（按能力维度取其最强代表，不是逐个产品比较）：实时语音（OpenAI Realtime /
Gemini Live）、陪伴与对话策略（Pi、Character.AI / Replika 类）、语音临场感（Sesame 类）、
记忆架构（Letta/MemGPT、Mem0、Zep 类时序知识图谱）、Agent 框架与工具生态
（Claude Agent SDK / OpenAI Agents SDK + MCP）、OS 级助理（Apple Intelligence + App Intents）。

| 维度 | 顶级实现的现状（≈2026 中） | eidolon-agent | 差距 |
|---|---|---|---|
| 单轮首响与打断 | 语音到语音端到端；打断即停并保留「已播出」语义 | 打断语义做得比多数产品干净；首响预算已成契约 | **持平/领先** |
| 上下文工程 | 分段、缓存前缀、预算裁剪 | 分段 + 易变性排序 + ledger + 预算 | **领先** |
| 中期上下文（10–100 轮） | 滚动摘要 / 自动压缩 / 分层摘要 | 20 条消息，无摘要、无压缩 | **落后 1–2 档** |
| 长期记忆 | 时序知识图谱 + 后台巩固 + 模型可主动检索 | 外部服务读（一次性、≤0.5s、模型无法主动检索） | **落后 1–2 档** |
| 后台认知 | 记忆巩固 / sleep-time compute / 异步任务 | 无 | **缺失** |
| 主动性 | 定时、事件、在场变化触发的主动交互 | 无（仅长任务完成回流） | **缺失** |
| 多模态感知 | 屏幕/摄像头持续流 + 事件化 | 通道就绪但无生产者；视觉服务未接入 | **缺失** |
| 工具/行动面 | MCP 生态 / App Intents / 数百个连接器 | 3 个真实工具 | **落后 3 档** |
| 对话策略（详略、追问、沉默） | 产品级差异化核心 | 已有 RESPONSE_POLICY 分档（`68f0f46`）；静默/恢复策略与可测指标仍缺 | **落后 1 档** |
| 人格一致 + 演化 | 版本化人格 + 反馈学习 | 不可变 genome（正确）+ 演化生产不可用 | **落后 1 档** |
| 安全 | 分类器 + 分级处置 + 危机流程 + 未成年人保护 | 8 个关键词 + 前缀软化 | **落后 2 档，且是合规风险** |
| 评测闭环 | 离线套件 + LLM 评审 + 线上 A/B | 离线确定性套件（质量高）+ 无线上闭环 | **落后 1 档** |
| 本地/隐私 | 端侧模型 + 私有云推理分层 | 单一云模型；standalone 档位强制 FakeLLM | **落后 1 档（与定位冲突）** |

---

## 4. 逻辑欠缺（认知回路层）

**L1. 记忆只有一次机会，且模型不能自救。**
`compiler._memory_recall`（`compiler.py:736`）在生成前串行跑最多 2 个 query，共享一个
`recall_timeout_s` deadline（默认 0.5s，`settings.py:119`）。query 是原始用户文本 +
最近 4 条历史拼接（`compiler.py:932-967`），没有查询改写、没有实体归一、没有多路并发。
命中失败后：模型没有 `recall_memory` 工具可调（**已验证**：工具表里没有），harness 又要求
「不询问、不播报内部记忆流程」。**失败模式是自信地失忆**，而不是「我去找一下」。
顶级做法：预取 + 模型可主动检索（工具）+ 生成中/生成后二次检索。

**L2. 中期记忆洞（最高优先级逻辑缺陷）。** **[09-14 修正]**
`history_window` 已由 4 放宽到 20（消息数，不是轮数，`settings.py:314`、`history/manager.py:44`），
降级窗口同为 20；**滚动摘要仍未接线**。一次 30 分钟的桌面陪伴对话，超出 20 条之前的内容
既不在 prompt、也不一定在长期记忆（memory 服务只吃已提交 turn，抽取策略在它那边），
中间层仍是真空 —— 只是真空的起点从第 3 轮推后到第 10 轮左右。

**L3. 输出长度没有硬约束。** ~~已关闭（`68f0f46`）~~
评审时 `turn.py:304` 从不传 `max_tokens`。**[09-14] 已提交 `68f0f46`：下发
`cfg.max_output_tokens` 并记 `output_truncated`（`turn.py:309`、`companion_runtime.py:25`）。**
剩余未闭合：`output_reserve_tokens`（`settings.py:321`）仍只进 trace 不生效；
`enable_filler_phrases` 仍是死配置（除 settings 无引用）。

**L4. 安全判定是子串匹配。**
`guardrails/input_filter.py:27-40`：8 个自伤关键词 + 4 个越权短语。既有漏检
（变体、拼音、英文长尾、跨轮累积风险），也有误判（"这剧好看到我想死"→ 直接走危机分支，
整轮被替换成危机文案）。输出侧只是**加前缀软化**（`turn.py:527-529`），对语音是明显的体验伤害。

**L5. 每一轮都走全量重路径。**
`939d468` 删掉了词法 triage（决策正确），但**没有替代物**：「几点了」「嗯」「停」与
「帮我梳理这周的工作」走同一条 compile + 同一个模型 + 同一套工具 schema（预算 800 token）。
这同时是延迟成本和金钱成本。

**L6. 人格运行时状态不学习。**
mood/energy/attention 只在内存（`personas/types.py:78`），进程重启即失；不回写 genome
（设计正确），但也**没有任何慢速通道**把「用户偏好更短的回答」「不喜欢被追问」变成持久事实。
`taboos_provider` 恒空（`bootstrap.py:500`）说明连最简单的持久偏好都没接。

**L7. 没有反馈信号回路。**
打断率、重复提问、会话时长、显式点赞 —— 一个都没有被采集为质量信号。TurnTrace 采了
大量**机制**指标（延迟、token、降级原因），没有采**效果**指标。

**L8. speculative 有机制无策略。**
是否投机、何时取消、如何合并，全由客户端决定（`chat_servicer.py:175`）。大脑对
「双倍 LLM 花费」没有任何预算或收益判断。

**L9. 延迟契约自相矛盾。**
`first_delta_slo_p95_ms=300`（`settings.py:311`）与 `recall_timeout_s=0.5`（`:119`）
并存，而 recall 在 compile 的关键路径上（`compiler.py:112-175` 的 gather）。
两个串行 query 共享该 deadline，最坏 compile 就 500ms+。README 又写「≤250ms 含 200ms recall」。
**三处数字互相打架**，这类不一致会让所有后续性能讨论失去基准。

---

## 5. 功能欠缺（产品能力层）

**F1. 工具面几乎为零** —— 见 §0.3。一个「桌面陪伴 OS 大脑」不能：定提醒/闹钟/计时器、
记一条笔记、查网页、看屏幕、控播放、读日历、开应用、发消息。

**F2. 没有 MCP 工具平面（最高性价比的功能补齐）。**
项目已经把 MCP 用于 memory 读（`infra/memory/mcp_client.py`），但**没有把 MCP 当作 agent
的工具平面**。顶级 Agent 产品已把 MCP 作为工具生态事实标准。做成 MCP host 后，工具
不再需要一个个在 `bootstrap.py` 里硬编码注册。

**F3. 主动性完全缺失。**
`TurnTrigger.PROACTIVE` 无生产者；`SubscribeProactive`（`chat_servicer.py:340`）只中继长任务
完成报告；`eidolon.memory.event.*`（promise_due）无订阅者；没有调度器、没有静默检测、
没有「早安/回访/答应过你的事」。陪伴类竞品的留存曲线主要由主动性驱动。

**F4. 承诺只能读不能写。**
memory 有 `PROMISE`/commitment 概念，大脑能读活跃承诺，但**没有创建/取消承诺的工具**，
也没有到期唤醒。"你明天提醒我" 在端到端上不成立。

**F5. 多模态感知未接入。**
`eidolon_vision` 产出 `sense.attention/session/fatigue/event`，契约在
`eidolon_sdk/biz/sense/protocol.py`；agent 侧零引用（**已验证**）。同时 `PushSignal`
在 Channel 无调用者（**已验证**）。桌面共处的核心卖点（知道你在不在、累不累、在忙）
目前没有数据通路进入大脑。模型本身也只吃文本，无图像入口。

**F6. 跨会话/跨设备没有「接续」。**
history 窗口按 conversation_id 存在内存；换设备/换房间= 冷启动。没有「我们上次聊到…」
的交接摘要（这与 L2 是同一根因）。

**F7. 没有大脑自己拥有的用户模型。**
偏好、称呼、作息、禁忌散落在 genome（Data 权威、不可变、改动要版本）和 memory（外部服务）
之间。缺一个**慢变、可编辑、热生效**的 owner 偏好层 —— 这正是「少说一点」这类诉求应该落的地方。

**F8. 对话策略层缺失。** ~~已部分关闭（`68f0f46`）~~
评审时详略/追问/建议全在 harness 的一段散文里。**[09-14] 已提交 `68f0f46`：新增
`domain/context/response_policy.py` + `RESPONSE_POLICY` volatile 段，按 modality 分档，
偏好来自 SDK `ConversationPreferences`，并带 `expected_preference_revision` 乐观并发
（同时覆盖上一篇评审的 F2 陈旧编辑覆盖）。** 剩余未闭合：静默策略、打断后恢复策略、
工具等待话术（见计划 T4）、以及把详略合规做成可测指标进基准套件。

**F9. 群体/多人场景无语义。** 一个房间里两个人、家庭共享伴侣：无说话人区分、无多 owner
上下文。可以是明确的 non-goal，但应写成 non-goal。

---

## 6. 顶层产品架构欠缺

**A1. 缺一个「慢回路」拥有者（最重要的架构决策）。**
现在只有 fast loop（turn）。需要一条独立的、有自己预算和 SLO 的 slow loop：
巩固记忆、写摘要、生成 persona 观察、评估是否值得主动、维护承诺到期。
零件都在（mementos coworker、memory 服务、NATS、long_task 队列），**缺的是拥有者和节律**。
建议放在 agent 内作为第二个 runtime loop（它需要 persona + memory + LLM 三者同时在手），
以 `agent.tick.*` 为节律，与 turn 共享 registry 但**不共享延迟预算**。

**A2. 对话策略无归属。** turn 提交/EOT/打断在 Channel + SDK 契约；详略/追问/沉默/主动
无归属。建议在 domain 下新增 `dialogue_policy`：输出目标长度、追问率、静默策略、
话题切换、打断后恢复 —— 声学归 Channel，**策略归大脑**。

**A3. genome 语义过载**（与前篇 F6/F7 同源，此处给架构切分）：拆成四层 ——
① 不可变身份 genome（Data 权威，版本化）；② owner 可编辑的对话偏好（大脑热生效，不版本化）；
③ 关系事实（memory realm）；④ 运行策略（agent 配置）。不做这个切分，"少说一点" 永远要走人格版本。

**A4. 缺面向用户的「许可 + 审计」平面。**
`Permission` 枚举和 `audit_outbox` 都是内部的。一个将来要控设备、动文件、可能花钱的 OS 级
agent，需要**一等公民**的：动作前确认契约、owner 可见的「伴侣替我做了什么」流水、
可撤销。顶级 OS 助理把这条做在系统层。现在补代价小，等工具面铺开后补代价极大。

**A5. 部署形态在两条路之间骑墙。**
单进程 + 内存 registry + SQLite + 强依赖 NATS（`bootstrap.py:188` 连不上直接
RuntimeError），但 README §1 又宣称「单实例服务多个 (tenant,user)」。更严重：
`standalone` 档位**强制 FakeLLM**（`bootstrap.py:423`）—— 于是「桌面用户没有 NATS/
memory/system-data 时也能和真模型说话」这个形态**不存在**。必须选一条：
桌面=每 owner 一进程、可完全本地降级；云端=集群 + 会话亲和。

**A6. 模型策略过于扁平。**
一个 default + 同名 fallback（`infra/llm/router.py`）。没有快/深分层、没有端侧路径
（`eidolon_models` 目前只有 ASR）、没有成本/延迟感知路由。对一个以隐私和临场感为卖点的
桌面 OS，端侧优先 + 云端兜底是参照架构级别的缺失。

**A7. 评测只有离线确定性一侧。**
`app/benchmark/suites.py` 覆盖话题切换、个人记忆、记忆更新与弃权、隐私边界、工具漂移、
打断恢复、多轮指代 —— **这套设计本身是加分项**。缺：LLM 评审的人格一致性/自然度/详略
合规打分、跨天多会话长程一致性、真实模型的 CI 基线、线上 A/B（`EIDOLON_EXP` bucket 空置）、
以及任何与留存相关的产品指标。

**A8. 安全与合规不足以支撑陪伴类产品上市。**
关键词危机分支没有：升级记录、后续跟进、owner/监护人可见性、未成年人模式、
依赖/情感依恋防护、越狱评测集。陪伴类 AI 在 2025–2026 已进入明确的监管视野
（美国加州针对 companion chatbot 的立法、联邦层面的问询、EU AI Act 透明度义务等；
**具体条款请以法务最新核对为准**）。这是**上市阻塞项**，不是 nice-to-have。

---

## 7. 技术欠缺（工程底座）

| 编号 | 问题 | 证据 | 影响 |
|---|---|---|---|
| T1 | metrics/OTel 只有配置无实现；无 TTFT/recall/tool 直方图，无跨服务 span（trace_id 已串起来却没人收） | `settings.py:212-217`，全仓无 exporter | 线上问题只能靠日志考古 |
| T2 | 无准入控制/限流/背压；`BackgroundTaskRunner` 不设上限（`eidolon_sdk/core/runtime`）；只有 gRPC 每连接 64 流 | `EIDOLON_RATELIMIT` 无使用者 | 过载时非确定性劣化而非有序降级 |
| T3 | 无成本核算与预算 | 仅 usage 计数 | 多用户下不可控 |
| T4 | 无显式 prompt cache 管理与命中观测（`cached_tokens_in` 字段存在但未见填充） | `core/types/llm.py:41` | 已做的 stable/volatile 切分收益不可测 |
| T5 | `max_tokens` 从不下发；`output_reserve_tokens` 不生效 | `turn.py:304` | 见 L3 |
| T6 | 延迟契约三处数字冲突 | 见 L9 | 性能工作失去基准 |
| T7 | 无故障注入/负载/长稳测试；延迟基准用 FakeLLM 且无 memory（README 自述） | `tests/integration/test_hot_path_latency.py` | memory 挂、NATS 挂、LLM 中途 500、SQLite 锁竞争都没有回归保护 |
| T8 | 「当前开发基线直接校验 clean schema，不保留迁移」 | README §9 | 装到用户机器上前必须补前向迁移，属 GA 阻塞 |
| T9 | 死/半死子系统未清理：SignalBus/Fuser/PushSignal（无生产者）、`summary_provider`（未接线）、`body_control`（fail-closed）、`enable_filler_phrases`、多个 KV bucket（CACHE 已弃用/HISTORY_WINDOW 预留/FLAGS/EXP/CONFIG 空置）、`memory_event_pattern`（无订阅）、`SubmitLongTaskTool` 兼容别名 | 全部 grep 已验证 | 让架构**看起来**比实际能力强，是评审和排期失真的直接来源 |
| T10 | 覆盖率门槛 77%、222 测试，对「大脑」偏低；`app/benchmark` 1462 行的 `live_local_contract.py` 与 `infra/benchmark/reporting.py` 1144 行属基础设施级体量，占据了本可用于能力建设的复杂度预算 | `wc -l` | 维护成本集中在测量而非能力 |

---

## 8. 优化方案

**已迁出。** 排序、验收门与范围裁剪现在只存在于
[`docs/plan/brain-optimization-plan.md`](../plan/brain-optimization-plan.md)。

本文自此只保留**发现与证据**（带日期，会随代码过期）；计划不在这里维护，
避免出现第二套排序。原 P0–P3 共 22 条与 MCP 的 M0–M4 已合并为该计划的 T1–T4，
并按「不能变成门禁的条目不进计划」裁剪；被裁掉的部分见其 §5。

## 9. 明确不建议做的事

- **不要重做分层或换掉 hexagonal + 4 条 import 契约。** 这是本仓最值钱的资产之一。
- **不要把慢回路做成第二个 realtime harness。** 它是不同 SLO 的另一种东西；混在一起会把
  turn 的延迟预算污染掉（harness prompt 里已经有「cowork 是工具，不是另一套 harness」的正确表述）。
- **不要恢复词法 triage。** 分层路由要用结构化判据或小模型，不要回到 `939d468` 删掉的东西。
- **不要在人格里加更多字段来表达对话偏好。** 那是 A3 的 ② 层。
- **不要为了主动性去做「随机搭话」。** 主动性必须由事件（承诺到期、在场变化、长任务完成、
  日程）驱动 + 抑制策略，否则会直接伤害桌面场景的信任。

---

## 10. 风险与开放问题

1. **慢回路放在 agent 内还是独立进程？** 本文建议在 agent 内（需要 persona+memory+LLM 同时在手），
   但这会让 agent 从「无状态应答内核」变成有节律的常驻大脑，与 A5 的部署形态选择耦合，需要一起决策。
2. **摘要写在 agent 库还是 memory 服务？** 建议 agent 库（它是会话上下文的键所有者，
   `conversation_id` 归 agent），但需与 memory 的抽取职责划清，避免两套摘要语义。
3. **主动性的产品边界谁定？** 免打扰、频次上限、可关闭 —— 这是产品决策，不能由实现默认值决定。
4. **端侧模型的时间表** 取决于 `eidolon_models`；P3-3 只预留路径，不承诺端侧质量。
5. 本文所有延迟/质量判断是机制层结论；P0-3 落地后应立刻用真实指标复核本文的优先级排序。
