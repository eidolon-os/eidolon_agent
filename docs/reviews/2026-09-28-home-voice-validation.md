# 家居兜底与澄清部署验证

## 部署版本

- SDK `a81d383`：clarification 结果及 golden。
- Agent `3594677`、`188daba`：统一兜底、独立家居会话上下文、明确已确认焦点。
- Channel `0970a95`：已有 voice session identity 与结束事件透传。
- ESP32 `478bb0fb`：共用家居面板支持 clarification；直接 DMA 显示配置保留。

均在各项目 main。Mac Agent、Channel Provider、Channel worker 已重启；
未重启 Laya 或训练。Korvo 写入及 buildstamp 回读通过。

## 自动化与实际服务

- Agent 70 项、Channel 66 项、SDK 42 项通过。
- C++ 家居消息/状态测试通过，Korvo IDF 编译通过。
- 真实 Laya + DeepSeek 的仅提案/测试执行端验证通过主要多轮场景。
- 实际 Mac Agent HTTP → 模型 → Channel Capability Runtime → SQLite 模拟设备：
  - 关闭主灯 → ambiguous，无执行。
  - 客厅那个 → executed，关闭 living.main_light。
  - 打开它 → executed，打开同一设备。
  - 再暗一点 → executed，实际状态降到 80%。
  - 算了 → 清除上下文，不执行设备。
  - 关闭会话后再次提交该 session → HTTP 409，无执行。
  - 新会话“客厅灯” → clarification；“打开” → executed。
- 同一实际服务验证产生的 clarification/executed 结果通过 Provider 发送，
  均 HTTP 200。串口采集窗口结束前未捕获这些结果到达，不能把发送成功
  称为实屏渲染已验收。ASR、用户操作和实屏一致性由下一轮真机测试验证。

日志：`/tmp/home-deployed-verification.log`、
`/tmp/korvo-clarification-flash.log`、`/tmp/korvo-home-clarification-runtime.log`。

## 单独发现的触控启动问题

本次 90 秒串口采集的首次启动在约 1 秒处发生一次重启：
GT1151 `esp_lcd_touch_gt1151_read_data` 返回 ESP_ERR_INVALID_CRC；
esp_lvgl_port 的 lvgl9/esp_lvgl_port_touch.c:127 使用 ESP_ERROR_CHECK
读取触控数据，因此触发 abort。第二次启动进入 READY，收到 18 个设备
快照，此后采集窗口内未再重启，没有 LCD underrun。

这是触控采样错误被依赖组件作为致命错误处理的直接证据，不能归因于
Laya/LLM 或已修复的 RGB 位移，也尚未证明 CRC 本身的电气/时序触发原因。
本次没有修改触控驱动或托管依赖以绕过它，保留该限制供单独修复。

## 23:50–23:52 Korvo physical test: actual decision routes

Audited Agent log `home interpretation` and `home turn` entries, correlated by
turn ID (times below are final-result times, Asia/Shanghai). No primary/Laya
proposal was directly executed in these 11 turns.

| Time | Utterance | Decision route | Result |
|---|---|---|---|
| 23:50:11 | 打开客厅灯 | Laya timeout → LLM | executed |
| 23:50:39 | 关闭客厅灯 | context → LLM | executed |
| 23:51:33 | 关闭主灯 | Laya timeout → LLM | ambiguous, buttons |
| 23:51:41 | 关闭主灯 | context → LLM | clarification, text only |
| 23:51:48 | 关闭主卧灯 | context → LLM | executed |
| 23:51:59 | 关闭主灯 | context → LLM | ambiguous, buttons |
| 23:52:03 | 客厅主灯 | context → LLM | executed |
| 23:52:20 | 打开它 | context → LLM | executed |
| 23:52:28 | 打开主灯 | context → LLM | executed |
| 23:52:34 | 打开主卧灯 | context → LLM | executed |
| 23:52:40 | 主卧灯调亮20% | context → LLM | executed, 60% |

The current routing sends **every** turn with unexpired context to the LLM,
including self-contained new commands. This is a conservative single-turn Laya
boundary, not evidence of low Laya confidence on those nine contextual turns.
These tests demonstrate LLM fallback and contextual execution, not Laya direct
execution. They do not establish why Laya exceeded its 800 ms deadline.

### Structured clarification presentation

`ask_home_clarification` now requires known target refs and a complete action
(or explicit null when the action/required parameters are missing). Agent
validates refs and normalizes complete, capable multi-device choices through
the existing ambiguity builder. The selected LLM tool no longer by itself
chooses text-only versus actionable presentation. Partial facts are retained
in session context. Questions never execute a device merely because filtering
leaves one capable candidate. Firmware and Channel contracts are unchanged.

Verification: 77 related Agent tests passed. Real-model replay with test device
ports produced five ambiguous results for repeated “关闭主灯”, each with the
same two candidates and off command, with zero executions before selection.
“客厅那个” executed once; “主灯” retained candidates but asked for action;
“算了” cleared the request. This replay is not physical microphone/UI testing.

Deployed by restarting only the Mac Agent. Readiness returned HTTP 200. Three
consecutive “关闭主灯” requests through the deployed authenticated command API
all returned the same two choices and off template; cancellation succeeded and
the isolated verification session was closed. Physical Korvo UI confirmation
of this optimization remains for the user.
