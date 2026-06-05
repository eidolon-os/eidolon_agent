# Replay Report

- schema: `eidolon_agent.live_service_replay_report.v1`
- generated_at: `2026-06-05T10:09:36.656320+00:00`
- passed: `False`

## Summary
- scenario_count: `11`
- passed: `5`
- failed: `6`

## Metrics
- turn_count: `24`
- first_delta_p50_ms: `367`
- first_delta_p95_ms: `581`
- total_p50_ms: `706`
- total_p95_ms: `1208`

## Scenarios

### live-call-name-preference - PASS
真实服务：称呼偏好写入后，后续回复能自然使用该称呼。
- `live-pref-1` first_delta=`563` total=`735` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `privacy_mode` (got=normal)
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=563)
  - PASS `max_total_ms` (got=735)
- `live-pref-2` first_delta=`1401` total=`1572` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:小满`
  - PASS `context_segment:persona` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=1401)
  - PASS `max_total_ms` (got=1572)

### live-fact-correction - PASS
真实服务：事实更正后，新事实应覆盖旧事实，后续回答不混用。
- `live-fact-1` first_delta=`386` total=`588` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=386)
  - PASS `max_total_ms` (got=588)
- `live-fact-2` first_delta=`296` total=`701` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=296)
  - PASS `max_total_ms` (got=701)
- `live-fact-3` first_delta=`420` total=`715` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:杭州`
  - PASS `forbidden_assistant:上海`
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=420)
  - PASS `max_total_ms` (got=715)

### live-promise-recall - PASS
真实服务：承诺/提醒类输入能写入并在后续 recall 中强制出现。
- `live-promise-1` first_delta=`323` total=`711` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=promise_create)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=323)
  - PASS `max_total_ms` (got=711)
- `live-promise-2` first_delta=`325` total=`601` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:喝水`
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=325)
  - PASS `max_total_ms` (got=601)

### live-forget-privacy - FAIL
真实服务：forget 后，后续回复、memory block、history 注入不应重新暴露目标称呼。
- `live-forget-1` first_delta=`377` total=`548` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=377)
  - PASS `max_total_ms` (got=548)
- `live-forget-2` first_delta=`6` total=`6` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=6)
  - PASS `max_total_ms` (got=6)
- `live-forget-3` first_delta=`467` total=`583` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `forbidden_assistant:阿满`
  - PASS `max_first_delta_ms` (got=467)
  - PASS `max_total_ms` (got=583)
- FAIL `scenario_forbidden_assistant:阿满`

### live-private-and-temporary - FAIL
真实服务：private/temporary turn 可回答，但不进入普通 history/context，也不触发 memory fanout。
- `live-private-1` first_delta=`339` total=`699` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `privacy_mode` (got=private)
  - PASS `max_first_delta_ms` (got=339)
  - PASS `max_total_ms` (got=699)
- `live-temporary-1` first_delta=`245` total=`354` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `privacy_mode` (got=temporary)
  - PASS `max_first_delta_ms` (got=245)
  - PASS `max_total_ms` (got=354)
- `live-private-2` first_delta=`332` total=`643` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `forbidden_assistant:私密名字`
  - PASS `forbidden_assistant:临时名字`
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=332)
  - PASS `max_total_ms` (got=643)
- PASS `scenario_forbidden_assistant:私密名字`
- FAIL `scenario_forbidden_assistant:临时名字`

### live-sensitive-consent - PASS
真实服务：敏感记忆候选需要用户同意，默认不 fanout。
- `live-sensitive-1` first_delta=`278` total=`527` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=sensitive_requires_consent)
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `memory_write_requires_consent` (got=sensitive_requires_consent)
  - PASS `max_first_delta_ms` (got=278)
  - PASS `max_total_ms` (got=527)
- `live-sensitive-2` first_delta=`650` total=`977` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=650)
  - PASS `max_total_ms` (got=977)

### live-tool-turn - PASS
真实服务：tool call → tool result → second LLM stream。
- `live-tool-1` first_delta=`503` total=`985` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `tool_name:get_time` (got=['get_time'])
  - PASS `tool_error_count` (got=0)
  - PASS `required_event:TOOL_CALL`
  - PASS `required_event:TOOL_RESULT`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=503)
  - PASS `max_total_ms` (got=985)

### live-tool-permission-denied - FAIL
真实服务：当环境收紧工具权限时，side-effect tool 应返回 permission denied 而不是执行副作用。默认开发配置允许 SYSTEM 权限，此场景用于发布前收紧配置 smoke。
- `live-tool-denied-1` first_delta=`531` total=`1061` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `tool_name:emit_event` (got=['emit_event'])
  - FAIL `tool_error_count` (got=0)
  - PASS `required_event:TOOL_CALL`
  - PASS `required_event:TOOL_RESULT`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=531)
  - PASS `max_total_ms` (got=1061)

### live-long-conversation-summary-restart - FAIL
真实服务：长对话后重开流仍应从 SQLite/history/summary 回填自然 recap。当前如未注入 summary，会在 context segment 检查中暴露缺口。
- `live-long-1` first_delta=`355` total=`652` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=355)
  - PASS `max_total_ms` (got=652)
- `live-long-2` first_delta=`391` total=`813` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=391)
  - PASS `max_total_ms` (got=813)
- `live-long-3` first_delta=`401` total=`1161` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=401)
  - PASS `max_total_ms` (got=1161)
- `live-long-4` first_delta=`581` total=`1208` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - FAIL `required_assistant:演示`
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=581)
  - PASS `max_total_ms` (got=1208)

### live-realtime-persona-emotion - FAIL
真实服务：inline realtime digest 应进入 context，并影响陪伴体当轮语气。
- `live-emotion-1` first_delta=`214` total=`397` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - FAIL `required_assistant:继续`
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `context_segment:realtime` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `max_first_delta_ms` (got=214)
  - PASS `max_total_ms` (got=397)

### live-context-budget-shadow - FAIL
真实服务：context budget shadow 模式应记录 would-drop 结果但不改变 prompt。需要服务以 EIDOLON_TURN_CONTEXT_BUDGET_MODE=shadow 且较小 budget 启动。
- `live-budget-1` first_delta=`236` total=`811` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=ignore)
  - PASS `max_first_delta_ms` (got=236)
  - PASS `max_total_ms` (got=811)
- `live-budget-2` first_delta=`358` total=`1292` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - FAIL `context_budget_shadow_dropped_count_min` (got=0)
  - PASS `max_first_delta_ms` (got=358)
  - PASS `max_total_ms` (got=1292)
