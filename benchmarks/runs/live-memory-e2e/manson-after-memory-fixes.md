# Replay Report

- schema: `eidolon_agent.live_service_replay_report.v1`
- generated_at: `2026-06-05T11:28:45.793232+00:00`
- passed: `False`

## Summary
- scenario_count: `11`
- passed: `6`
- failed: `5`

## Metrics
- turn_count: `24`
- first_delta_p50_ms: `380`
- first_delta_p95_ms: `503`
- total_p50_ms: `825`
- total_p95_ms: `1118`

## Scenarios

### live-call-name-preference - PASS
真实服务：称呼偏好写入后，后续回复能自然使用该称呼。
- `live-pref-1` first_delta=`573` total=`925` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `privacy_mode` (got=normal)
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=573)
  - PASS `max_total_ms` (got=925)
- `live-pref-2` first_delta=`1474` total=`1590` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:小满`
  - PASS `context_segment:persona` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=1474)
  - PASS `max_total_ms` (got=1590)

### live-fact-correction - FAIL
真实服务：事实更正后，新事实应覆盖旧事实，后续回答不混用。
- `live-fact-1` first_delta=`371` total=`750` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=371)
  - PASS `max_total_ms` (got=750)
- `live-fact-2` first_delta=`400` total=`800` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=400)
  - PASS `max_total_ms` (got=800)
- `live-fact-3` first_delta=`417` total=`702` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:杭州`
  - FAIL `forbidden_assistant:上海`
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=417)
  - PASS `max_total_ms` (got=702)

### live-promise-recall - PASS
真实服务：承诺/提醒类输入能写入并在后续 recall 中强制出现。
- `live-promise-1` first_delta=`376` total=`905` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=promise_create)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=376)
  - PASS `max_total_ms` (got=905)
- `live-promise-2` first_delta=`375` total=`937` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:喝水`
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=375)
  - PASS `max_total_ms` (got=937)

### live-forget-privacy - FAIL
真实服务：forget 后，后续回复、memory block、history 注入不应重新暴露目标称呼。
- `live-forget-1` first_delta=`353` total=`567` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=353)
  - PASS `max_total_ms` (got=567)
- `live-forget-2` first_delta=`7` total=`7` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=7)
  - PASS `max_total_ms` (got=7)
- `live-forget-3` first_delta=`362` total=`425` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `forbidden_assistant:阿满`
  - PASS `max_first_delta_ms` (got=362)
  - PASS `max_total_ms` (got=425)
- FAIL `scenario_forbidden_assistant:阿满`

### live-private-and-temporary - FAIL
真实服务：private/temporary turn 可回答，但不进入普通 history/context，也不触发 memory fanout。
- `live-private-1` first_delta=`266` total=`484` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `privacy_mode` (got=private)
  - PASS `max_first_delta_ms` (got=266)
  - PASS `max_total_ms` (got=484)
- `live-temporary-1` first_delta=`176` total=`258` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `privacy_mode` (got=temporary)
  - PASS `max_first_delta_ms` (got=176)
  - PASS `max_total_ms` (got=258)
- `live-private-2` first_delta=`351` total=`835` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `forbidden_assistant:私密名字`
  - PASS `forbidden_assistant:临时名字`
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=351)
  - PASS `max_total_ms` (got=835)
- FAIL `scenario_forbidden_assistant:私密名字`
- FAIL `scenario_forbidden_assistant:临时名字`

### live-sensitive-consent - PASS
真实服务：敏感记忆候选需要用户同意，默认不 fanout。
- `live-sensitive-1` first_delta=`285` total=`675` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=sensitive_requires_consent)
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `memory_write_requires_consent` (got=sensitive_requires_consent)
  - PASS `max_first_delta_ms` (got=285)
  - PASS `max_total_ms` (got=675)
- `live-sensitive-2` first_delta=`417` total=`917` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=417)
  - PASS `max_total_ms` (got=917)

### live-tool-turn - PASS
真实服务：tool call → tool result → second LLM stream。
- `live-tool-1` first_delta=`503` total=`1042` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `tool_name:get_time` (got=['get_time'])
  - PASS `tool_error_count` (got=0)
  - PASS `required_event:TOOL_CALL`
  - PASS `required_event:TOOL_RESULT`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=503)
  - PASS `max_total_ms` (got=1042)

### live-tool-permission-denied - FAIL
真实服务：当环境收紧工具权限时，side-effect tool 应返回 permission denied 而不是执行副作用。默认开发配置允许 SYSTEM 权限，此场景用于发布前收紧配置 smoke。
- `live-tool-denied-1` first_delta=`380` total=`911` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - FAIL `tool_name:emit_event` (got=[])
  - FAIL `tool_error_count` (got=0)
  - FAIL `required_event:TOOL_CALL`
  - FAIL `required_event:TOOL_RESULT`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=380)
  - PASS `max_total_ms` (got=911)

### live-long-conversation-summary-restart - PASS
真实服务：长对话后重开流仍应从 SQLite/history/summary 回填自然 recap。当前如未注入 summary，会在 context segment 检查中暴露缺口。
- `live-long-1` first_delta=`351` total=`585` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=351)
  - PASS `max_total_ms` (got=585)
- `live-long-2` first_delta=`400` total=`816` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=400)
  - PASS `max_total_ms` (got=816)
- `live-long-3` first_delta=`410` total=`941` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=410)
  - PASS `max_total_ms` (got=941)
- `live-long-4` first_delta=`397` total=`1017` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:演示`
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=397)
  - PASS `max_total_ms` (got=1017)

### live-realtime-persona-emotion - PASS
真实服务：inline realtime digest 应进入 context，并影响陪伴体当轮语气。
- `live-emotion-1` first_delta=`392` total=`659` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:继续`
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `context_segment:realtime` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `max_first_delta_ms` (got=392)
  - PASS `max_total_ms` (got=659)

### live-context-budget-shadow - FAIL
真实服务：context budget shadow 模式应记录 would-drop 结果但不改变 prompt。需要服务以 EIDOLON_TURN_CONTEXT_BUDGET_MODE=shadow 且较小 budget 启动。
- `live-budget-1` first_delta=`381` total=`1118` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=ignore)
  - PASS `max_first_delta_ms` (got=381)
  - PASS `max_total_ms` (got=1118)
- `live-budget-2` first_delta=`396` total=`1526` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - FAIL `context_budget_shadow_dropped_count_min` (got=0)
  - PASS `max_first_delta_ms` (got=396)
  - PASS `max_total_ms` (got=1526)
