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
