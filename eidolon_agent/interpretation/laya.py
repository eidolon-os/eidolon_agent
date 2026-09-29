"""Bounded HTTP adapter for Laya's typed System One v1 response."""

from __future__ import annotations

import asyncio
import math
from urllib.parse import urlsplit

import httpx
from eidolon_sdk.core.http import create_async_client

from .contracts import (
    ACTIONS,
    INTENTS,
    MODEL_CONTRACT_VERSION,
    InterpretationRequest,
    Suggestion,
)

_NONE = "没有对应的设备"
_MULTI = "多个设备或整屋"


class LayaInterpretationAdapter:
    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:8771",
        revision: str = "45f3dedb",
        timeout_s: float = 1.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        url = urlsplit(endpoint)
        if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Laya endpoint must be loopback HTTP")
        if timeout_s <= 0 or timeout_s > 5:
            raise ValueError("Laya timeout must be in (0, 5] seconds")
        self.endpoint = endpoint.rstrip("/")
        self.revision = revision
        self.timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client if client is not None else create_async_client(trust_env=False, timeout=timeout_s)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def interpret(self, request: InterpretationRequest) -> Suggestion | None:
        if not request.devices:
            return None
        labels = {item.label: item.device_id for item in request.devices}
        questions = {
            "intent": {
                "type": "choice",
                "instructions": "`utterance` 是在让智能家居做什么？",
                "criteria": {
                    "控制": "要求改变家里设备的状态",
                    "查询": "询问设备状态，不改变设备",
                    "无关": "不是家居命令或查询",
                },
            },
            "device": {
                "type": "choice",
                "instructions": "`utterance` 说的是家里的哪台设备？",
                "criteria": {
                    **{d.label: f"{d.room}·{d.kind}" for d in request.devices},
                    _MULTI: "多台设备或整屋",
                    _NONE: "家里没有对应设备",
                },
            },
            "action": {
                "type": "choice",
                "instructions": "`utterance` 要对设备做什么操作？",
                "criteria": {name: name for name in ACTIONS},
            },
        }
        body = {
            "state": {"utterance": request.utterance},
            "questions": questions,
            "options": {
                "ask_if": {"device": {"intent": ["控制", "查询"]}, "action": {"intent": ["控制"]}}
            },
        }
        try:

            async def post() -> httpx.Response:
                return await self._client.post(
                    self.endpoint + "/v1/systemone", json=body, timeout=self.timeout_s,
                )

            response = await asyncio.wait_for(post(), timeout=self.timeout_s)
            response.raise_for_status()
            if len(response.content) > 256 * 1024:
                return None
            payload = response.json()
            if (
                payload.get("contract_version") != MODEL_CONTRACT_VERSION
                or payload.get("revision") != self.revision
                or payload.get("truncated")
                or payload.get("skipped", {}).get("intent")
            ):
                return None
            answers = payload["answers"]
            intent = _answer(answers, "intent", INTENTS)
            if intent is None:
                return None
            if intent[0] == "无关":
                return Suggestion("无关", None, None, intent[1], "laya", self.revision)
            device = _answer(answers, "device", (*labels, _NONE, _MULTI))
            if device is None or device[0] not in labels:
                return None
            if intent[0] == "查询":
                return Suggestion(
                    "查询",
                    labels[device[0]],
                    None,
                    min(intent[1], device[1]),
                    "laya",
                    self.revision,
                )
            action = _answer(answers, "action", ACTIONS)
            if action is None:
                return None
            return Suggestion(
                "控制",
                labels[device[0]],
                action[0],
                min(intent[1], device[1], action[1]),
                "laya",
                self.revision,
            )
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            return None


def _answer(answers: dict, key: str, allowed: tuple[str, ...]) -> tuple[str, float] | None:
    value = answers.get(key)
    if not isinstance(value, dict) or value.get("type") != "choice":
        return None
    choice = value.get("choice")
    confidence = value.get("confidence")
    if (
        choice not in allowed
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
    ):
        return None
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return None
    return choice, float(confidence)
