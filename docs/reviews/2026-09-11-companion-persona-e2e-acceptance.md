# Companion / Persona 自动化联调验收

日期：2026-09-10 至 2026-09-11。按用户要求，优先用真实服务、真实模型、真实语音供应商和可重复的用户操作验证体验；真机只承担硬件与 OS 行为验证。所有新增写入使用临时 SQLite、合成 Owner 和 Companion，没有修改用户数据库。未部署。

本轮提交：Admin `bd1709e`、Mobile `d67e11e`、Channel `7499a28`；Agent 的上下文默认值、测试、脚本和本报告同次提交。没有推送。其他任务的架构/设备路由/Channel 基准与场景改动均保留。

## 本轮发现与修复

1. **本机管理请求被系统代理截走。** Local API → Admin 默认 HTTPX 客户端读取系统代理，实际 socket 联调出现 502/503；同一地址绕过代理正常。内部客户端改用 `trust_env=False`。联网回归故意配置不可用代理，验证真实鉴权和创建/编辑仍可完成。
2. **两轮之后快速遗忘。** 默认 `history_context_window=4` 是四条消息，不是四轮。三个起点各十轮对话在第九轮都无法回忆第二轮的猫名。默认窗口调整为 20 条消息；降级窗口至少同样大小，仍受现有 6000 token 估算预算裁剪，不引入第二套记忆或摘要服务。显式窗口配置仍受尊重。新增回归同时验证近期事实保留和超出窗口后不再重放。
3. **大块文本绕过 TTS 单批上限。** SentenceAggregator 原来只在达到 60 字时触发发送，单次传入完整长回复时依然发送整段。真实供应商首轮只生成约 33 秒音频，尾部识别匹配失败。现按标点优先切分，确保实际每批不超过配置上限，全部内容按顺序发送。新增 1/17/1000 字输入块、有/无标点六个用例，验证不丢、不重、不过限。

第三项修复的是传输批次上限，**没有证明任意 one-shot 输入都能完整合成**。同一 703 字样本在修复后的 one-shot 路径仍只有约 123 秒，尾部匹配 0.056；正式 SDK Markdown 过滤＋流式输入路径为约 142 秒，末尾完整。两条路径输入形态和过滤均不同，本轮未进一步拆分因果，不能把差异全部归因于过滤或分段。

## 自动化证据与边界

| 层级 | 本轮结果 | 实际覆盖 / 替身边界 |
|---|---|---|
| 跨项目 HTTP | 10 项通过 | Local API、Admin 管理路由、Data Companion/Workspace authority、Agent preview 使用生产实现和真实 loopback socket；多个 ASGI app 在同一 Python 进程。Controller 凭据来源和服务发现为测试替身；模型默认确定性替身 |
| Flutter 实际客户端和页面 | 2 项真实 HTTP 场景通过 | 一项执行 CRUD、改名、冲突、恢复、重放；另一项在实时 Widget binding 中实际填写画像、选择详略、点击试聊、两步创建、编辑语气，再读 Data 核对。生产页面、ManagementClient、生成 DTO；由上述 Python 测试启动 Flutter 子进程，属于上一行十项中的一项 |
| Mobile 既有页面与通话流程 | 118 项通过 | 两步创建、字段保留、试聊迟到结果丢弃、失败重试、连接恢复、模式切换等；这些组件测试的后端/RTC 使用替身 |
| Agent Compiler / Harness / Persona | 88 项通过 | 窗口、预算、偏好、人格和 Memory 分支回归 |
| Agent gRPC / 产品流程 | 8 项通过 | 包含生产 bootstrap、真实 gRPC、SQLite 运行记录、流生命周期及清理；确定性模型，不连接真实 Channel 房间 |
| Admin 管理边界 | 35 项通过 | 创建、人格、改名、组合与拒绝边界 |
| Channel 语音场景 | 228 passed，6 xfailed，27 deselected | 语速、停顿、自我纠正、PTT 连按、半/全双工、错误打断恢复、分段播放。真实 pipeline/SDK 与合成 PCM，部分 EOT 走本地模型；STT/LLM/TTS 使用场景替身 |
| TTS 修复定向回归 | 47 项通过 | 大块文本上限、取消、连接恢复、流错误、清理；与上一行有重叠，不能相加当作独立场景数 |
| Memory 进程 E2E | 6 项通过 | 独立 Memory 进程、NATS、MCP、HistoryFanout→投影→召回→Compiler、隐私生命周期；语义抽取使用确定性 fixture |
| 真实模型连续对话 | 3 起点 × 10 轮，30/30 自动断言通过 | 实际配置的 `openai/deepseek-v4-flash`、生产 TurnEngine、HTTP 人格读取、本地历史；Memory 空命中/KG/断连/超时在端口注入；不执行工具、不写 Memory |

Flutter 最终静态分析无问题。Agent 和 Channel 本轮 Python 文件 Ruff 通过，四仓库 diff 空白检查通过。Admin `local_api/app.py` 有 15 项既有未使用 import，已对照 HEAD 复核数量与位置一致，本轮未扩大清理范围。

模型实测见 [修复前](./persona-multiturn-before.jsonl) 和 [最终输出](./persona-multiturn-journeys.jsonl)。最终无输出截断，三个猫名均召回且未混淆其他伙伴事实；普通场景字符中位数 11，明确详细回答为 703、310、352 字符。首个文本 delta 中位数 1104.5 ms，范围 675–1464 ms，**不是 speech-stop 到首音的端到端延迟，也不是受控性能 A/B**。初轮脚本没有传入生产 bootstrap 的预算配置，最终脚本已按当前设置构造 Harness 和 Compiler。

自动断言只检查完成、非空、截断、指定事实、单句请求长度和跨伙伴样本混淆，不等于所有回复质量达标。人工复核仍看到 playful 在“只想说说”场景追加问题；三条建议后也可能加祝福或额外收尾。人格可辨识性、知识事实正确性、追问是否自然仍不能用这些通过数代替质量评价。

## 真实语音供应商验证

使用本机配置的 Bailian STT/TTS。两条合成用户语音经实际 STT 后，作为连续对话的情绪和详细请求输入；最终用 Agent 实际回复驱动 SDK Markdown 过滤与流式 TTS。没有麦克风、真实 RTC 房间或设备播放 ACK，因此属于分段贯通的真实供应商链路。

| 样本 | 音频时长 | 首个 TTS frame | 验证结果 |
|---|---:|---:|---|
| 情绪输入 | 4.85 秒 | 370 ms | STT 归一化文本匹配 1.0 |
| 详细请求输入 | 6.13 秒 | 456 ms | STT 归一化文本匹配 1.0 |
| 情绪回复，18 字 | 4.64 秒 | 434 ms | 全文 ASR 匹配 1.0；尾部匹配 1.0 |
| 详细回复，703 字 | 141.71 秒 | 419 ms | 最后 15 秒 ASR，尾部匹配 1.0 |
| 短信回复，34 字 | 8.85 秒 | 494 ms | 全文 ASR 匹配 0.967；尾部匹配 1.0 |

长回复末尾明确识别出“定位决定你讲什么……练习决定你讲得顺不顺”。长音频整段 ASR 与 20 秒分段尝试出现超时，其中一次底层 WebSocket 报 keepalive ping timeout；最终采用尾部检查，**没有验证长音频中间每一句**。短信 ASR 多出“啊，没”，本轮没有区分合成多读与识别误差，不能宣称语音逐字准确。

证据：[语音输入](./persona-audio-journeys/input.json)、[流式回复](./persona-audio-journeys/output.json)、[初轮 one-shot 失败记录](./persona-audio-journeys/output-before.json)。JSON 中 WAV 路径是本地最新一次运行的文件位置；初轮音频已被后续运行覆盖，只保留当时的识别结果。

## 尚未达标的软件场景

Channel 的六项 strict xfail 没有被放宽或改成通过，属于两组已复现的软件缺口：

- 三种重叠语音意图：“好的”误取消、“不要停止，请继续刚才的解释”误取消、“不是，我说的是明天”未及时打断。EOT 完句概率不能当作打断意图；见 Channel 的 [FD-INTENT 记录](../../../eidolon_channel/docs/analysis/全双工级联优化执行记录-2026-09-05.md)。
- “如果预算不够的话”后停顿 2.2 秒，条件句还没结束就提前回复；在全双工无意图、全双工意图和半双工三种配置各失败一次。

下一步先修这两组话轮决策，再跑原冻结用例及真实重叠语音。它们无需等待真机；也不适合通过新增案例热词表或统一延长等待时间来掩盖。此次没有扩展人格架构去承担 Channel 的话轮决策。

此外，长文本 one-shot 合成未通过末尾验收，需要继续核查供应商输入、任务结束和音频消费；保留脚本默认 one-shot 模式用于复现，不能因正式流式样本通过就将它关闭。

当前 Channel 生产源码中找到的 `synthesize_all` 调用用于短等待语；正式回答经 SDK 流式 TTS。因此该问题已作为独立输入边界保留，不能直接推断为当前长回答播放路径仍被截断。

## 真机与部署的剩余边界

本轮没有将所有服务接入一个真实 RTC 房间并在手机连续使用；分层联调不能声称已覆盖完整部署。后续顺序是隔离环境的 RTC 房间联调，再补手机的麦克风权限、AEC/扬声器回声、蓝牙路由切换、锁屏/后台、音频焦点抢占及实际弱网重连。硬件拾音和主观韵律最后在真机听测，软件已知失败先在自动化中修复。

## 复跑

需要并列的 Agent/Admin/Data/SDK/Mobile/Channel/Memory 工作区及各自环境。Agent 环境须可导入 Admin 依赖（本机补装了 `psutil`）；网络套件显式 opt-in，普通测试默认跳过。命令中的工作目录不能混用。

Agent：

```sh
EIDOLON_PERSONA_NETWORK_E2E=1 PYTHONPATH=.:../eidolon_admin/server .venv/bin/pytest tests/e2e/test_persona_network_journeys.py -q
PYTHONPATH=.:../eidolon_admin/server .venv/bin/python scripts/validate_persona_journeys.py --execute-model --speech-input docs/reviews/persona-audio-journeys/input.json
.venv/bin/pytest tests/e2e/test_product_acceptance_profile.py eidolon_agent/app/transport/grpc/tests -q
```

Channel（先 input，再运行 Agent 连续对话，再 output）：

```sh
PYTHONPATH=. .venv/bin/python ../eidolon_agent/scripts/validate_persona_audio.py --phase input --output ../eidolon_agent/docs/reviews/persona-audio-journeys
PYTHONPATH=. .venv/bin/python ../eidolon_agent/scripts/validate_persona_audio.py --phase output --streaming --output ../eidolon_agent/docs/reviews/persona-audio-journeys --journeys ../eidolon_agent/docs/reviews/persona-multiturn-journeys.jsonl
.venv/bin/pytest eidolon/livekit/tests/tts/test_sentence_aggregator.py eidolon/livekit/tests/tts/bailian eidolon/livekit/tests/pipeline/test_tts_stage.py -q
```

音频是合成测试内容。WAV 保留在本地供听测，目录内忽略提交；JSON 报告和脚本纳入版本库。
