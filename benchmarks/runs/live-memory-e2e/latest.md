# Replay Report

- schema: `eidolon_agent.live_service_replay_report.v1`
- generated_at: `2026-06-05T10:00:36.367118+00:00`
- passed: `False`

## Summary
- scenario_count: `11`
- passed: `0`
- failed: `11`
- startup_error: `HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503`

## Metrics
- turn_count: `0`
- first_delta_p50_ms: `None`
- first_delta_p95_ms: `None`
- total_p50_ms: `None`
- total_p95_ms: `None`

## Scenarios

### live-call-name-preference - FAIL
真实服务：称呼偏好写入后，后续回复能自然使用该称呼。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-fact-correction - FAIL
真实服务：事实更正后，新事实应覆盖旧事实，后续回答不混用。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-promise-recall - FAIL
真实服务：承诺/提醒类输入能写入并在后续 recall 中强制出现。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-forget-privacy - FAIL
真实服务：forget 后，后续回复、memory block、history 注入不应重新暴露目标称呼。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-private-and-temporary - FAIL
真实服务：private/temporary turn 可回答，但不进入普通 history/context，也不触发 memory fanout。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-sensitive-consent - FAIL
真实服务：敏感记忆候选需要用户同意，默认不 fanout。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-tool-turn - FAIL
真实服务：tool call → tool result → second LLM stream。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-tool-permission-denied - FAIL
真实服务：当环境收紧工具权限时，side-effect tool 应返回 permission denied 而不是执行副作用。默认开发配置允许 SYSTEM 权限，此场景用于发布前收紧配置 smoke。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-long-conversation-summary-restart - FAIL
真实服务：长对话后重开流仍应从 SQLite/history/summary 回填自然 recap。当前如未注入 summary，会在 context segment 检查中暴露缺口。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-realtime-persona-emotion - FAIL
真实服务：inline realtime digest 应进入 context，并影响陪伴体当轮语气。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)

### live-context-budget-shadow - FAIL
真实服务：context budget shadow 模式应记录 would-drop 结果但不改变 prompt。需要服务以 EIDOLON_TURN_CONTEXT_BUDGET_MODE=shadow 且较小 budget 启动。
- FAIL `service_startup` (HTTPStatusError: Server error '503 Service Unavailable' for url 'http://127.0.0.1:9000/api/users'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503)
