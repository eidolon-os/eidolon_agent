# 家居 LLM 传输延迟诊断与改进

2026-09-29。训练问题已拆到`/Users/manson/tmp/laya-home-continuation-training-20260929/`，本轮没有继续训练Laya、修改模型权重或家居提示词。

## 结论与边界

已有LiteLLM首块/有效片段日志继续复用，补上完整工具结果、finish、流结束、关闭连接耗时以及usage。`connect_ms`是acompletion取得流对象的时间，包含SDK/网络/服务响应，不是纯TCP连接时间。`tool_ready_ms`在收到finish且完成工具JSON解析时记录，不允许提前执行未完成提案。

确认并修复usage尾包`choices=[]`被跳过；请求标准stream_options.include_usage，记录已有SDK返回的输入/输出及缓存token。缺失usage在日志记None；cost_usd_micro默认0不是免费或实际费用结论。router回退日志带request_id；configured_max_retries是配置上限，不能视为实际重试次数。

临时HTTPX trace只记录事件名、时间和HTTP状态，不记录URL、请求头、凭据或响应正文。60条HTTP/1.1均建立新TLS；最慢2.84秒请求的TLS阶段约2.02秒。检查当前OpenAI SDK流实现，在[DONE]后关闭响应；HTTP/1.1追踪没有body-complete，连接不能留待下次复用。原始6–7秒请求没有这些底层trace，不能追溯认定全部由TLS导致。

改动只在Agent现有共享HTTPX客户端启用官方HTTP/2协商，加入httpx[http2]锁定依赖。保留HTTP/1.1支持、原代理策略、超时、重试和模型设置。没有修改SDK源码、增加网络旁路或新路由服务。HTTP/2实测复用连接，40次仅建立一次TLS；20次正式代码复验也只建立一次TLS。

## 实测

输入仅来自SDK内置apartment演示目录及10条手写语义用例，所有上下文由脚本构造，不读取真实家庭目录/用户对话，不调用执行端。每次8秒预算，错误/超时不剔除。四批顺序执行，未随机交错，时段差异可能影响上游延迟，不能把全部差值归因于协议。

| 批次 | 样本 | 正确 | p50 | p95 | 最大 | TLS建立 |
|---|---:|---:|---:|---:|---:|---:|
| 初始阶段日志 | 40 | 40/40 | 1085.75ms | 1399.5ms | 2348.5ms | 未追踪 |
| HTTP/1.1 trace | 60 | 60/60 | 1167.95ms | 2053.3ms | 2839.9ms | 60 |
| HTTP/2隔离实验 | 40 | 40/40 | 819.95ms | 1300.2ms | 1792.9ms | 1 |
| 正式代码复验 | 20 | 20/20 | 794.7ms | 1230.4ms | 1347.0ms | 1 |

三批HTTP trace均每请求只有一次HTTP尝试，没有观察到重试。HTTP/1.1与HTTP/2隔离实验的缓存token占比均约95.62%，差异不能解释为新增缓存命中。完整工具结果之后到流结束通常仅数毫秒，无提前执行优化的依据。

这是LLM理解函数的耗时，非ASR到屏幕端到端耗时；四批均未重现6–7秒离群，不宣称长尾已根除，也不推算生产p99。需要真实交互继续使用按turn_id关联的理解/执行与传输日志确认；opi5max的网络路径不同，不将Mac数据当成远端性能。

## 回归与复现

309项Agent相关回归通过，包括usage尾包、缓存token、取消不被包装成重试错误、取消后的连接关闭，以及家居动作/澄清/否定/会话版本规则。新增依赖仅h2、hpack、hyperframe，没有升级原LLM/HTTP库。

从eidolon_agent根目录运行（外部LLM请求仅合成数据，使用已有配置和凭据）：

```sh
.venv/bin/python docs/reviews/2026-09-29-home-llm-latency/probe.py --output /tmp/home-http2-new-run --repeats 2
.venv/bin/python docs/reviews/2026-09-29-home-llm-latency/probe.py --output /tmp/home-http1-new-run --repeats 2 --http1
```

输出目录必须不存在。原始四批结果、transport.log和summary.json在本目录。依赖版本：httpx0.28.1、httpcore1.0.9、openai2.37.0、litellm1.85.1；HTTP/2依赖h2 4.4.1、hpack4.2.0、hyperframe6.1.0。
