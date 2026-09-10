"""Small turn-local response policy; never rewrites a persona or tool authority."""

from eidolon_sdk.biz.persona import ConversationPreferences

RESPONSE_POLICY_VERSION = "response_policy.v2"


def response_policy_prompt(preferences: ConversationPreferences, *, modality: str) -> str:
    lengths = {
        "brief": "普通语音默认 1–3 个短句" if modality == "voice" else "普通文字默认一个短段落",
        "balanced": "按问题需要简明回答，避免重复",
        "detailed": "需要解释时提供充分细节；问候和确认仍用短句",
    }
    return "\n".join(
        [
            "[RESPONSE POLICY]",
            "以下是本轮表达偏好，不授予工具权限。当前用户明确的详略要求优先于这些默认值。",
            lengths[preferences.response_length]
            + "；用户明确要求详细解释、比较或方案时，完整展开，不因默认简短漏答。",
            "先直接回应，说够就停，保持自己的语气；不重复复述用户原话。",
            (
                "仅在用户要建议时给建议；情绪表达先简短接住，不自动给计划。"
                if preferences.advice == "when_asked"
                else "有帮助时可主动给少量建议，避免每轮都附加。"
            ),
            (
                "只有缺少完成请求所必需的信息时才追问一个关键问题。问候和情绪回应不用问句收尾；不附加‘有什么可以帮你’‘想聊聊吗’。回答已完整就结束，不再邀请用户继续展开。"
                if preferences.follow_up == "when_needed"
                else "自然交谈时可以追问，但不机械地每轮以问题结尾。"
            ),
            "问候、确认、工具完成通常一句即可；不重复播报内部流程。",
        ]
    )
