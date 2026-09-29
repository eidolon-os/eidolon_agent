# HTTP 连接池统一：真机发布验证

2026-09-29，用户授权部署后执行。opi5max 正式发布
`rk3588-http-pools-20260929-1`，精确提交见 deployment.json。六个 HTTP 改动组件
一起更新；Data、Memory 和 Models 保留现网版本，Models 固定 `7e71d6a`。
Mac 经 Ops restart 加载源码，12/12 健康；opi5max 21/21 active，无服务重启循环，
本次 release doctor 与两端 App readiness 通过。

Mac 保留现有 source-run 机制，Admin/Memory/Models 工作区存在其他未提交开发改动，
不能将 Mac 描述为完全 commit-pinned 发布；opi5max 封装只使用固定提交。
Mac Laya 仍在本机 torch/MPS，opi5max 在本机 RKNN NPU，没有跨 Host 随机路由。

## 测量范围与结果

脚本使用 SDK 演示目录、手写话语与内存执行替身，不读取实际用户对话、不控制真实设备。
模型调用使用各 Host 正式配置和发布代码；这是独立探针进程，不是生产 Agent 进程内 trace。
健康检查来自实际运行服务。未覆盖麦克风 ASR 到屏幕显示的完整交互，也未模拟整机断网。
Mac LLM 与多轮探针有并行运行，opi5max 两批顺序运行；数据不用于跨机优劣归因。

| 测量 | Mac | opi5max |
| --- | --- | --- |
| 合成 LLM 理解 | 20/20 正确 | 20/20 正确 |
| HTTP 协议 / 请求 / TLS 建立 | HTTP/2 / 20 / 1 | HTTP/2 / 20 / 1 |
| LLM p50 / p95 / max | 954.2 / 1319.9 / 1783.2 ms | 829.1 / 1364.3 / 1791.5 ms |
| 连续场景 | 16/17 正确 | 16/17 正确 |
| 多轮最大耗时 | 1692.3 ms | 1637.7 ms |
| 多轮内 Laya（4 次）p50 / max | 234.8 / 801.2 ms | 514.1 / 560.3 ms |

分位数用 nearest-rank；17 条的 p95 即最大值。样本不足以估计生产 p99，
未重现历史 6–7 秒离群不代表长尾根治。Mac 首条 Laya 约 801 ms，保留此边界样本。
远端 Laya diagnostics 明确返回 backend=rknn，恢复测试三次为315–399 ms。

两端健康读取五次各复用一条 TCP；空闲六秒后重新建连并成功，所有响应200。
调用方取消后同一 Laya 适配器可继续推理，最终关闭客户端。Mac 初次1ms取消没有可靠
触发，保留原始记录；补测 timeout(0)立即取消通过。opi5max 1ms取消通过。
这只能证明本次客户端取消/连接恢复，不能代替服务端执行结果未知时的对账测试。

## 保留的两个问题

1. 两端相同语义失败：打开窗帘→关闭窗帘→“打开它”，LLM 返回再次澄清，预期是引用
   当前窗帘。无错误设备动作。这是指代理解/任务表达问题，不能以连接优化宣称解决，
   也不能直接归入 Laya 训练，因为本轮走 LLM。下一步以此反例验证上下文表达和推理。
2. 单独执行基础环境 doctor 返回 degraded：旧安装记录 artifacts 多一项 llama-server；
   当前要求的所有制品版本/hash均与记录一致，没有缺失；工具、包、容量和基础服务健康。
   发布事务中的 release doctor 通过。未篡改安装记录，也未为清掉提示而重装基础环境。

此前 Admin 三项权限范围断言失败未在本轮修复；本轮发布没有改变权限列表。

## 复现

llm_probe.py 接受 --output（新目录）和 --repeats；replay.py 使用 PROBE_OUTPUT。
环境变量 PROBE_ROOT、PROBE_SETTINGS、PROBE_ENV_FILES 指向该 Host 已有运行路径。
凭据仅在 Host 本地加载，未保存到本证据目录。probe_fakes.py 复用既有测试替身并去掉
pytest fixture 装饰，远端无需安装测试依赖。所有 raw results 保留失败和耗时样本。
