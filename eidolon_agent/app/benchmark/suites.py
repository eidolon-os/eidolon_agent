"""Built-in replay benchmark suites for agent + memory experience."""

from __future__ import annotations

from typing import Any

AGENT_MEMORY_BENCHMARK_NAME = "agent_memory_experience.v1"
LIVE_AGENT_MEMORY_BENCHMARK_NAME = "live_agent_memory_experience.v1"

_BASE_EXPECT = {
    "current_request_authority": True,
    "no_tool_calls": True,
    "context_structure_version": "context_structure.v2",
    "history_presentation": "background_context",
    "max_tool_repeat_suppressed": 0,
    "max_first_delta_ms": 100,
}


def agent_memory_experience_scenarios() -> list[dict[str, Any]]:
    """Return a deterministic broad replay suite for fast daily regression."""

    scenarios: list[dict[str, Any]] = []
    scenarios.extend(_topic_switch_scenarios(24))
    scenarios.extend(_personal_memory_scenarios(20))
    scenarios.extend(_memory_update_and_abstention_scenarios(16))
    scenarios.extend(_privacy_and_forgetting_scenarios(16))
    scenarios.extend(_tool_drift_scenarios(16))
    scenarios.extend(_interrupt_recovery_scenarios(16))
    scenarios.extend(_multi_turn_reference_scenarios(12))
    return scenarios


def live_agent_memory_experience_scenarios() -> list[dict[str, Any]]:
    """Return a broad benchmark intended for a running real service stack.

    These scenarios use robust, strongly constrained prompts and observable
    trace checks. They are meant to exercise real gRPC/admin/SQLite/memory/LLM
    paths without grading open-ended wording too tightly.
    """

    scenarios: list[dict[str, Any]] = []
    scenarios.extend(_live_context_authority_scenarios(24))
    scenarios.extend(_live_memory_recall_scenarios(20))
    scenarios.extend(_live_memory_update_scenarios(16))
    scenarios.extend(_live_privacy_scenarios(16))
    scenarios.extend(_live_tool_control_scenarios(12))
    scenarios.extend(_live_interrupt_recovery_scenarios(12))
    return scenarios


def _scenario(
    *,
    scenario_id: str,
    category: str,
    description: str,
    turns: list[dict[str, Any]],
    tags: list[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    memory: dict[str, Any] | None = None,
    expect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": scenario_id,
        "category": category,
        "tags": tags or [],
        "description": description,
        "default_turn_expect": dict(_BASE_EXPECT),
        "turns": turns,
    }
    if history:
        payload["history"] = history
    if memory:
        payload["memory"] = memory
    if expect:
        payload["expect"] = expect
    return payload


def _turn(
    user: str,
    assistant: str,
    *,
    turn_id: str,
    expect: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "turn_id": turn_id,
        "user": user,
        "assistant": assistant,
        "expect": expect or {},
    }
    if metadata:
        payload["metadata"] = metadata
    return payload


def _background_expect(*, required: list[str] | None = None) -> dict[str, Any]:
    expect: dict[str, Any] = {
        "background_non_actionable": True,
        "required_prompt_substrings": ["[BACKGROUND CONTEXT]", "must_not_execute"],
        "required_context_tags": [
            {
                "kind": "history",
                "authority": "background",
                "actionability": "must_not_execute",
            }
        ],
    }
    if required:
        expect["required_prompt_substrings"].extend(required)
    return expect


def _topic_switch_scenarios(count: int) -> list[dict[str, Any]]:
    old_topics = [
        ("天气", "常州天气怎么样？", "常州今天偏热，傍晚留意阵雨。", "数三个数，一二三", "一二三。", "常州"),
        ("餐厅", "帮我想个晚餐餐厅", "可以考虑清淡一点的日料。", "翻译 hello world", "hello world 可以译为：你好，世界。", "餐厅"),
        ("会议", "明天会议要准备什么？", "可以准备议程和风险清单。", "讲个短笑话", "短笑话：日志一开，问题就害羞了。", "会议"),
        ("代码", "这个报错怎么修？", "先看堆栈和最近改动。", "帮我写一句生日祝福", "生日快乐，愿新的一岁轻松明亮。", "报错"),
        ("行程", "去上海两天怎么玩？", "第一天市区，第二天看展会更稳。", "解释一下递归", "递归就是函数在子问题上调用自己。", "上海"),
        ("提醒", "记得提醒我喝水", "我会把提醒意图记下来。", "只回答 ok", "ok", "提醒"),
    ]
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        label, old_user, old_answer, new_user, new_answer, forbidden = old_topics[
            idx % len(old_topics)
        ]
        scenarios.append(
            _scenario(
                scenario_id=f"topic_switch_{idx + 1:03d}",
                category="context_authority",
                tags=["topic_switch", label],
                description="旧话题进入背景后，新 turn 必须按当前请求回答。",
                turns=[
                    _turn(
                        old_user,
                        old_answer,
                        turn_id=f"topic-switch-{idx + 1:03d}-1",
                    ),
                    _turn(
                        new_user,
                        new_answer,
                        turn_id=f"topic-switch-{idx + 1:03d}-2",
                        expect={
                            **_background_expect(required=[old_user, new_user]),
                            "required_assistant_substrings": [new_answer[:2]],
                            "forbidden_assistant_substrings": [forbidden],
                        },
                    ),
                ],
            )
        )
    return scenarios


def _personal_memory_scenarios(count: int) -> list[dict[str, Any]]:
    names = ["小满", "阿满", "小林", "小周", "Manson"]
    prefs = ["低糖咖啡", "粤语歌", "黑色主题", "早晨工作", "短句回复"]
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        name = names[idx % len(names)]
        pref = prefs[idx % len(prefs)]
        memory_text = f"称呼偏好: 用户希望被叫作{name}\n偏好: 用户喜欢{pref}"
        question = "你记得我喜欢什么，以及该怎么称呼我吗？"
        answer = f"记得，你希望我叫你{name}，也提过喜欢{pref}。"
        scenarios.append(
            _scenario(
                scenario_id=f"personal_memory_{idx + 1:03d}",
                category="memory_use",
                tags=["personalization", "retrieval"],
                description="长期偏好只能作为回答当前请求的证据。",
                memory={"initial_context": memory_text},
                turns=[
                    _turn(
                        question,
                        answer,
                        turn_id=f"personal-memory-{idx + 1:03d}-1",
                        expect={
                            "required_prompt_substrings": [
                                "[RETRIEVED MEMORY]",
                                name,
                                pref,
                            ],
                            "required_assistant_substrings": [name, pref],
                        },
                    )
                ],
            )
        )
    return scenarios


def _memory_update_and_abstention_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        if idx % 2 == 0:
            old_city = "上海" if idx % 4 == 0 else "南京"
            new_city = "杭州" if idx % 4 == 0 else "苏州"
            scenarios.append(
                _scenario(
                    scenario_id=f"memory_update_{idx + 1:03d}",
                    category="memory_update",
                    tags=["fact_update"],
                    description="事实更新后，回答使用新事实而不是旧背景。",
                    history=[
                        {"role": "user", "content": f"我住在{old_city}"},
                        {"role": "assistant", "content": f"好的，我记下你住在{old_city}。"},
                    ],
                    memory={"initial_context": f"事实更正: 用户现在住在{new_city}"},
                    turns=[
                        _turn(
                            "我现在住在哪里？",
                            f"你现在住在{new_city}。",
                            turn_id=f"memory-update-{idx + 1:03d}-1",
                            expect={
                                **_background_expect(required=[old_city]),
                                "required_prompt_substrings": [new_city],
                                "required_assistant_substrings": [new_city],
                                "forbidden_assistant_substrings": [old_city],
                            },
                        )
                    ],
                )
            )
        else:
            unknown = f"银行卡后四位-{idx}"
            scenarios.append(
                _scenario(
                    scenario_id=f"memory_abstain_{idx + 1:03d}",
                    category="memory_abstention",
                    tags=["abstain", "privacy"],
                    description="记忆里没有的信息必须承认不知道。",
                    memory={"initial_context": "偏好: 用户喜欢短句回复"},
                    turns=[
                        _turn(
                            f"我上次说的{unknown}是什么？",
                            "我不知道，当前记忆里没有这个信息。",
                            turn_id=f"memory-abstain-{idx + 1:03d}-1",
                            expect={
                                "required_assistant_substrings": ["不知道"],
                                "forbidden_assistant_substrings": ["1234", "0000"],
                            },
                        )
                    ],
                )
            )
    return scenarios


def _privacy_and_forgetting_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        if idx % 2 == 0:
            temp_name = f"临时称呼{idx + 1}"
            scenarios.append(
                _scenario(
                    scenario_id=f"temporary_memory_{idx + 1:03d}",
                    category="memory_privacy",
                    tags=["temporary", "no_write"],
                    description="temporary turn 可以回答，但不能写入长期记忆。",
                    turns=[
                        _turn(
                            f"以后叫我{temp_name}",
                            f"好的，这轮我先叫你{temp_name}。",
                            turn_id=f"temporary-memory-{idx + 1:03d}-1",
                            metadata={"temporary": True},
                            expect={
                                "memory_fanout_allowed": False,
                                "memory_context_empty": True,
                                "required_assistant_substrings": [temp_name],
                            },
                        ),
                        _turn(
                            "你记得刚才那个临时称呼吗？",
                            "我没有把它写进长期记忆。",
                            turn_id=f"temporary-memory-{idx + 1:03d}-2",
                            expect={
                                "memory_context_empty": True,
                                "required_assistant_substrings": ["没有"],
                            },
                        ),
                    ],
                )
            )
        else:
            name = "小满" if idx % 4 == 1 else "阿满"
            scenarios.append(
                _scenario(
                    scenario_id=f"forget_memory_{idx + 1:03d}",
                    category="memory_privacy",
                    tags=["forget", "privacy"],
                    description="用户要求忘记后，记忆和历史都不应继续注入目标事实。",
                    turns=[
                        _turn(
                            f"以后叫我{name}",
                            f"好的，我会叫你{name}。",
                            turn_id=f"forget-memory-{idx + 1:03d}-1",
                        ),
                        _turn(
                            f"请忘记叫我{name}",
                            "好的，我会忘掉这个称呼。",
                            turn_id=f"forget-memory-{idx + 1:03d}-2",
                            expect={
                                "skip_default_expect": True,
                                "forget_called": True,
                                "memory_context_empty": True,
                            },
                        ),
                        _turn(
                            "你还记得怎么叫我吗？",
                            "我不确定你希望我怎么称呼你。",
                            turn_id=f"forget-memory-{idx + 1:03d}-3",
                            expect={
                                "memory_context_empty": True,
                                "forbidden_assistant_substrings": [name],
                            },
                        ),
                    ],
                    expect={"forget_called": True, "memory_context_empty": True},
                )
            )
    return scenarios


def _tool_drift_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    current_tasks = [
        ("帮我数到三", "一、二、三。", "天气"),
        ("把 good morning 翻译成中文", "good morning 是早上好。", "工具"),
        ("总结一句今天状态", "今天的状态可以概括为：先稳住节奏。", "查询"),
        ("换个话题，讲一句鼓励", "你已经在推进了，慢慢来。", "天气"),
    ]
    for idx in range(count):
        user, answer, forbidden = current_tasks[idx % len(current_tasks)]
        scenarios.append(
            _scenario(
                scenario_id=f"tool_drift_{idx + 1:03d}",
                category="agent_tool_control",
                tags=["tool_failure", "drift_guard"],
                description="旧工具失败只能作为背景，不能污染新任务。",
                history=[
                    {"role": "user", "content": "帮我查常州天气"},
                    {
                        "role": "assistant",
                        "content": "天气查询失败，我暂时拿不到结果。",
                        "metadata": {
                            "context_status": "failed",
                            "tool_relation": "failed",
                        },
                    },
                ],
                turns=[
                    _turn(
                        user,
                        answer,
                        turn_id=f"tool-drift-{idx + 1:03d}-1",
                        expect={
                            **_background_expect(required=["status=failed", user]),
                            "required_assistant_substrings": [answer[:2]],
                            "forbidden_assistant_substrings": [forbidden],
                            "required_context_tags": [
                                {
                                    "kind": "history",
                                    "status": "failed",
                                    "actionability": "must_not_execute",
                                }
                            ],
                        },
                    )
                ],
            )
        )
    return scenarios


def _interrupt_recovery_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    asks = [
        ("停一下，直接回答 42 是多少", "42 就是四十二。"),
        ("打断一下，换成讲一句晚安", "晚安，今天辛苦了。"),
        ("先别查了，解释一下什么是缓存", "缓存是把常用结果临时存起来。"),
        ("取消刚才的，帮我写一句感谢", "谢谢你刚才的帮忙，我很感激。"),
    ]
    for idx in range(count):
        user, answer = asks[idx % len(asks)]
        scenarios.append(
            _scenario(
                scenario_id=f"interrupt_recovery_{idx + 1:03d}",
                category="interrupt_realtime",
                tags=["interrupt", "cancel"],
                description="被打断的 preamble 只作为不可执行背景。",
                history=[
                    {
                        "role": "assistant",
                        "content": "我先调用相关工具处理一下。",
                        "metadata": {
                            "context_status": "interrupted",
                            "tool_relation": "abandoned_due_to_interrupt",
                        },
                    }
                ],
                turns=[
                    _turn(
                        user,
                        answer,
                        turn_id=f"interrupt-recovery-{idx + 1:03d}-1",
                        expect={
                            **_background_expect(
                                required=[
                                    "status=interrupted",
                                    "tool_relation=abandoned_due_to_interrupt",
                                ]
                            ),
                            "required_assistant_substrings": [answer[:2]],
                            "required_context_tags": [
                                {
                                    "kind": "history",
                                    "status": "interrupted",
                                    "tool_relation": "abandoned_due_to_interrupt",
                                }
                            ],
                        },
                    )
                ],
            )
        )
    return scenarios


def _multi_turn_reference_scenarios(count: int) -> list[dict[str, Any]]:
    seeds = [
        ("常州今天有阵雨。", "那明天呢？", "明天常州也要留意阵雨。", "常州"),
        ("这段代码的问题是空值没处理。", "那怎么改？", "可以先加空值判断。", "空值"),
        ("你刚才说我适合早晨做深度工作。", "那下午适合干嘛？", "下午更适合处理沟通和收尾。", "下午"),
        ("我们定了小项目目标：先跑通 benchmark。", "下一步呢？", "下一步是把结果报告固定下来。", "报告"),
    ]
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        first_answer, follow_up, second_answer, anchor = seeds[idx % len(seeds)]
        scenarios.append(
            _scenario(
                scenario_id=f"multi_turn_reference_{idx + 1:03d}",
                category="multi_turn_reference",
                tags=["anaphora", "conversation_memory"],
                description="多轮指代可以利用背景，但不能把背景当新任务。",
                turns=[
                    _turn(
                        f"先记一下上下文 {idx + 1}",
                        first_answer,
                        turn_id=f"multi-turn-reference-{idx + 1:03d}-1",
                    ),
                    _turn(
                        follow_up,
                        second_answer,
                        turn_id=f"multi-turn-reference-{idx + 1:03d}-2",
                        expect={
                            **_background_expect(required=[first_answer, follow_up]),
                            "required_assistant_substrings": [anchor],
                        },
                    ),
                ],
            )
        )
    return scenarios


def _live_base_expect() -> dict[str, Any]:
    return {
        "context_structure_version": "context_structure.v2",
        "history_presentation": "background_context",
        "current_request_authority": True,
        "max_first_delta_ms": 5000,
        "max_total_ms": 30000,
    }


def _live_scenario(
    *,
    scenario_id: str,
    category: str,
    description: str,
    turns: list[dict[str, Any]],
    tags: list[str] | None = None,
    expect: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": scenario_id,
        "category": category,
        "tags": tags or [],
        "description": description,
        "default_turn_expect": _live_base_expect(),
        "turns": turns,
        "expect": expect or {},
    }


def _live_context_authority_scenarios(count: int) -> list[dict[str, Any]]:
    old_tasks = [
        ("常州天气怎么样？", "AMB-LIVE-COUNT"),
        ("帮我计划一个晚餐。", "AMB-LIVE-DINNER"),
        ("先帮我查一个工具。", "AMB-LIVE-TOOL"),
        ("我们刚才在讨论会议。", "AMB-LIVE-MEETING"),
        ("之前那段代码怎么修？", "AMB-LIVE-CODE"),
        ("我想继续聊旅行。", "AMB-LIVE-TRAVEL"),
    ]
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        old_user, token = old_tasks[idx % len(old_tasks)]
        scenarios.append(
            _live_scenario(
                scenario_id=f"live_context_authority_{idx + 1:03d}",
                category="context_authority",
                tags=["live", "topic_switch"],
                description="真实服务：旧话题进入背景，新 turn 只执行当前请求。",
                turns=[
                    _turn(
                        old_user,
                        "",
                        turn_id=f"live-context-authority-{idx + 1:03d}-1",
                    ),
                    _turn(
                        (
                            f"换个话题。请只回答这个测试码，不要解释：{token}-{idx + 1:03d}"
                        ),
                        "",
                        turn_id=f"live-context-authority-{idx + 1:03d}-2",
                        expect={
                            "background_non_actionable": True,
                            "no_tool_calls": True,
                            "required_assistant_substrings": [f"{token}-{idx + 1:03d}"],
                            "forbidden_assistant_substrings": ["天气怎么样", "晚餐", "会议"],
                        },
                    ),
                ],
            )
        )
    return scenarios


def _live_memory_recall_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        token = f"AMB-MEM-{idx + 1:03d}"
        scenarios.append(
            _live_scenario(
                scenario_id=f"live_memory_recall_{idx + 1:03d}",
                category="memory_use",
                tags=["live", "memory_recall"],
                description="真实服务：写入唯一偏好/事实后，后续 turn 能召回。",
                turns=[
                    _turn(
                        f"请记住我的 benchmark 测试代号是 {token}。",
                        "",
                        turn_id=f"live-memory-recall-{idx + 1:03d}-1",
                        expect={
                            "memory_write_disposition": "semantic_upsert",
                            "memory_fanout_allowed": True,
                        },
                    ),
                    _turn(
                        "我的 benchmark 测试代号是什么？请只回答代号。",
                        "",
                        turn_id=f"live-memory-recall-{idx + 1:03d}-2",
                        expect={
                            "background_non_actionable": True,
                            "required_assistant_substrings": [token],
                            "context_contains_segments": ["memory", "history", "current_user"],
                        },
                    ),
                ],
            )
        )
    return scenarios


def _live_memory_update_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        old_token = f"AMB-OLD-{idx + 1:03d}"
        new_token = f"AMB-NEW-{idx + 1:03d}"
        scenarios.append(
            _live_scenario(
                scenario_id=f"live_memory_update_{idx + 1:03d}",
                category="memory_update",
                tags=["live", "fact_update"],
                description="真实服务：事实更正后，回答应使用新值。",
                turns=[
                    _turn(
                        f"请记住我的当前项目代号是 {old_token}。",
                        "",
                        turn_id=f"live-memory-update-{idx + 1:03d}-1",
                        expect={"memory_write_disposition": "semantic_upsert"},
                    ),
                    _turn(
                        f"更正一下，我的当前项目代号不是 {old_token}，而是 {new_token}。",
                        "",
                        turn_id=f"live-memory-update-{idx + 1:03d}-2",
                        expect={"memory_write_disposition": "semantic_upsert"},
                    ),
                    _turn(
                        "我的当前项目代号是什么？请只回答代号。",
                        "",
                        turn_id=f"live-memory-update-{idx + 1:03d}-3",
                        expect={
                            "background_non_actionable": True,
                            "required_assistant_substrings": [new_token],
                            "forbidden_assistant_substrings": [old_token],
                            "context_contains_segments": ["memory", "history", "current_user"],
                        },
                    ),
                ],
            )
        )
    return scenarios


def _live_privacy_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        token = f"AMB-PRIVATE-{idx + 1:03d}"
        metadata = {"temporary": True} if idx % 2 else {"private": True}
        privacy_mode = "temporary" if idx % 2 else "private"
        scenarios.append(
            _live_scenario(
                scenario_id=f"live_privacy_{idx + 1:03d}",
                category="memory_privacy",
                tags=["live", privacy_mode],
                description="真实服务：private/temporary turn 不写长期记忆。",
                turns=[
                    _turn(
                        f"这是一条{privacy_mode}测试，请记住私密代号 {token}。",
                        "",
                        turn_id=f"live-privacy-{idx + 1:03d}-1",
                        metadata=metadata,
                        expect={
                            "privacy_mode": privacy_mode,
                            "memory_fanout_allowed": False,
                        },
                    ),
                    _turn(
                        "刚才那个私密代号是什么？如果没有长期记忆请说不知道。",
                        "",
                        turn_id=f"live-privacy-{idx + 1:03d}-2",
                        expect={
                            "forbidden_assistant_substrings": [token],
                            "memory_context_injected": False,
                        },
                    ),
                ],
            )
        )
    return scenarios


def _live_tool_control_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        token = f"AMB-NOTOOL-{idx + 1:03d}"
        scenarios.append(
            _live_scenario(
                scenario_id=f"live_tool_control_{idx + 1:03d}",
                category="agent_tool_control",
                tags=["live", "no_drift_tools"],
                description="真实服务：当前请求可直接回答时，不延续旧工具目标。",
                turns=[
                    _turn(
                        "我们刚才提到过查天气和调用工具，但先别执行。",
                        "",
                        turn_id=f"live-tool-control-{idx + 1:03d}-1",
                    ),
                    _turn(
                        f"现在只回答这个 token，不要调用任何工具：{token}",
                        "",
                        turn_id=f"live-tool-control-{idx + 1:03d}-2",
                        expect={
                            "background_non_actionable": True,
                            "no_tool_calls": True,
                            "required_assistant_substrings": [token],
                            "max_tool_repeat_suppressed": 0,
                        },
                    ),
                ],
            )
        )
    return scenarios


def _live_interrupt_recovery_scenarios(count: int) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for idx in range(count):
        token = f"AMB-INT-{idx + 1:03d}"
        scenarios.append(
            _live_scenario(
                scenario_id=f"live_interrupt_recovery_{idx + 1:03d}",
                category="interrupt_realtime",
                tags=["live", "recovery"],
                description="真实服务：打断/换话题后，旧 preamble/工具目标不污染当前回答。",
                turns=[
                    _turn(
                        "如果要查工具，先等我确认。",
                        "",
                        turn_id=f"live-interrupt-recovery-{idx + 1:03d}-1",
                    ),
                    _turn(
                        f"打断一下，直接只回答 {token}",
                        "",
                        turn_id=f"live-interrupt-recovery-{idx + 1:03d}-2",
                        expect={
                            "background_non_actionable": True,
                            "no_tool_calls": True,
                            "required_assistant_substrings": [token],
                        },
                    ),
                ],
            )
        )
    return scenarios


__all__ = [
    "AGENT_MEMORY_BENCHMARK_NAME",
    "LIVE_AGENT_MEMORY_BENCHMARK_NAME",
    "agent_memory_experience_scenarios",
    "live_agent_memory_experience_scenarios",
]
