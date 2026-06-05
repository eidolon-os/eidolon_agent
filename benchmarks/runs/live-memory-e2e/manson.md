# Replay Report

- schema: `eidolon_agent.live_service_replay_report.v1`
- generated_at: `2026-06-05T10:03:11.668962+00:00`
- passed: `False`

## Summary
- scenario_count: `11`
- passed: `5`
- failed: `6`

## Metrics
- turn_count: `24`
- first_delta_p50_ms: `251`
- first_delta_p95_ms: `544`
- total_p50_ms: `654`
- total_p95_ms: `1013`

## Scenarios

### live-call-name-preference - FAIL
真实服务：称呼偏好写入后，后续回复能自然使用该称呼。
- `live-pref-1` first_delta=`58646` total=`58737` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `privacy_mode` (got=normal)
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona'])
  - FAIL `max_first_delta_ms` (got=58646)
  - FAIL `max_total_ms` (got=58737)
- `live-pref-2` first_delta=`617` total=`630` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:小满`
  - PASS `context_segment:persona` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=617)
  - PASS `max_total_ms` (got=630)

### live-fact-correction - PASS
真实服务：事实更正后，新事实应覆盖旧事实，后续回答不混用。
- `live-fact-1` first_delta=`487` total=`591` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=487)
  - PASS `max_total_ms` (got=591)
- `live-fact-2` first_delta=`544` total=`696` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=544)
  - PASS `max_total_ms` (got=696)
- `live-fact-3` first_delta=`524` total=`669` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:杭州`
  - PASS `forbidden_assistant:上海`
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=524)
  - PASS `max_total_ms` (got=669)

### live-promise-recall - FAIL
真实服务：承诺/提醒类输入能写入并在后续 recall 中强制出现。
- `live-promise-1` first_delta=`482` total=`840` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=promise_create)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=482)
  - PASS `max_total_ms` (got=840)
- `live-promise-2` first_delta=`502` total=`835` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - FAIL `required_assistant:喝水`
  - PASS `context_segment:memory` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=502)
  - PASS `max_total_ms` (got=835)

### live-forget-privacy - FAIL
真实服务：forget 后，后续回复、memory block、history 注入不应重新暴露目标称呼。
- `live-forget-1` first_delta=`178` total=`298` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=semantic_upsert)
  - PASS `memory_fanout_allowed` (got=True)
  - PASS `max_first_delta_ms` (got=178)
  - PASS `max_total_ms` (got=298)
- `live-forget-2` first_delta=`2` total=`2` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=2)
  - PASS `max_total_ms` (got=2)
- `live-forget-3` first_delta=`267` total=`482` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - FAIL `forbidden_assistant:阿满`
  - PASS `max_first_delta_ms` (got=267)
  - PASS `max_total_ms` (got=482)
- FAIL `scenario_forbidden_assistant:阿满`

### live-private-and-temporary - FAIL
真实服务：private/temporary turn 可回答，但不进入普通 history/context，也不触发 memory fanout。
- `live-private-1` first_delta=`184` total=`283` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `privacy_mode` (got=private)
  - PASS `max_first_delta_ms` (got=184)
  - PASS `max_total_ms` (got=283)
- `live-temporary-1` first_delta=`172` total=`320` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `privacy_mode` (got=temporary)
  - PASS `max_first_delta_ms` (got=172)
  - PASS `max_total_ms` (got=320)
- `live-private-2` first_delta=`170` total=`539` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `forbidden_assistant:私密名字`
  - PASS `forbidden_assistant:临时名字`
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=170)
  - PASS `max_total_ms` (got=539)
- FAIL `scenario_forbidden_assistant:私密名字`
- FAIL `scenario_forbidden_assistant:临时名字`

### live-sensitive-consent - PASS
真实服务：敏感记忆候选需要用户同意，默认不 fanout。
- `live-sensitive-1` first_delta=`257` total=`492` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=sensitive_requires_consent)
  - PASS `memory_fanout_allowed` (got=False)
  - PASS `memory_write_requires_consent` (got=sensitive_requires_consent)
  - PASS `max_first_delta_ms` (got=257)
  - PASS `max_total_ms` (got=492)
- `live-sensitive-2` first_delta=`280` total=`683` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=280)
  - PASS `max_total_ms` (got=683)

### live-tool-turn - PASS
真实服务：tool call → tool result → second LLM stream。
- `live-tool-1` first_delta=`351` total=`765` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `tool_name:get_time` (got=['get_time'])
  - PASS `tool_error_count` (got=0)
  - PASS `required_event:TOOL_CALL`
  - PASS `required_event:TOOL_RESULT`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=351)
  - PASS `max_total_ms` (got=765)

### live-tool-permission-denied - FAIL
真实服务：当环境收紧工具权限时，side-effect tool 应返回 permission denied 而不是执行副作用。默认开发配置允许 SYSTEM 权限，此场景用于发布前收紧配置 smoke。
- `live-tool-denied-1` first_delta=`181` total=`1040` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `tool_name:emit_event` (got=['emit_event'])
  - FAIL `tool_error_count` (got=0)
  - PASS `required_event:TOOL_CALL`
  - PASS `required_event:TOOL_RESULT`
  - PASS `required_event:DONE`
  - PASS `max_first_delta_ms` (got=181)
  - PASS `max_total_ms` (got=1040)

### live-long-conversation-summary-restart - PASS
真实服务：长对话后重开流仍应从 SQLite/history/summary 回填自然 recap。当前如未注入 summary，会在 context segment 检查中暴露缺口。
- `live-long-1` first_delta=`245` total=`640` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=245)
  - PASS `max_total_ms` (got=640)
- `live-long-2` first_delta=`202` total=`629` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=202)
  - PASS `max_total_ms` (got=629)
- `live-long-3` first_delta=`232` total=`812` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `max_first_delta_ms` (got=232)
  - PASS `max_total_ms` (got=812)
- `live-long-4` first_delta=`294` total=`904` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:演示`
  - PASS `context_segment:history` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `context_segment:current_user` (got=['current_user', 'history', 'memory', 'persona'])
  - PASS `max_first_delta_ms` (got=294)
  - PASS `max_total_ms` (got=904)

### live-realtime-persona-emotion - PASS
真实服务：inline realtime digest 应进入 context，并影响陪伴体当轮语气。
- `live-emotion-1` first_delta=`187` total=`479` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `required_assistant:继续`
  - PASS `context_segment:persona` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `context_segment:realtime` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `context_segment:current_user` (got=['current_user', 'memory', 'persona', 'realtime'])
  - PASS `max_first_delta_ms` (got=187)
  - PASS `max_total_ms` (got=479)

### live-context-budget-shadow - FAIL
真实服务：context budget shadow 模式应记录 would-drop 结果但不改变 prompt。需要服务以 EIDOLON_TURN_CONTEXT_BUDGET_MODE=shadow 且较小 budget 启动。
- `live-budget-1` first_delta=`189` total=`671` passed=`True`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - PASS `memory_write_disposition` (got=ignore)
  - PASS `max_first_delta_ms` (got=189)
  - PASS `max_total_ms` (got=671)
- `live-budget-2` first_delta=`236` total=`1013` passed=`False`
  - PASS `stream_done`
  - PASS `admin_trace_available`
  - FAIL `context_budget_shadow_dropped_count_min` (got=0)
  - PASS `max_first_delta_ms` (got=236)
  - PASS `max_total_ms` (got=1013)
