"""LiteLLM streaming provider — unified interface for 100+ LLM providers.

Implements :class:`~eidolon_agent.core.ports.llm.LLMPort` using ``litellm``.
Model names follow LiteLLM convention: ``gpt-4o-mini``, ``claude-3-5-sonnet-latest``,
``ollama/llama3``, etc.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
import litellm

from eidolon_agent.core.errors import LLMUnavailableError
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason, LLMUsage
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolCall, ToolSchema

litellm.drop_params = True

_log = logging.getLogger(__name__)
_shared_http_client: httpx.AsyncClient | None = None


class LiteLLMProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        shared_http_client: bool = True,
        trust_env: bool = False,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._shared_http_client = shared_http_client
        self._trust_env = trust_env

    @property
    def model_id(self) -> str:
        return self._model

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        request_id: str,
    ) -> AsyncIterator[LLMDelta]:
        kwargs = self._completion_kwargs(
            messages=messages,
            model=model,
            temperature=temperature,
            stream=True,
            timeout_s=self._timeout,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = [t.to_openai_function() for t in tools]

        tool_buf: dict[int, dict] = {}
        t0 = time.monotonic()
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            connect_ms = int((time.monotonic() - t0) * 1000)
            _log.warning(
                "litellm acompletion failed req=%s model=%s connect_ms=%d err=%s",
                request_id,
                kwargs["model"],
                connect_ms,
                exc,
            )
            raise LLMUnavailableError(f"litellm call failed: {exc}") from exc
        connect_ms = int((time.monotonic() - t0) * 1000)
        first_chunk_logged = False

        try:
            try:
                async for chunk in response:
                    if not first_chunk_logged:
                        ttft_ms = int((time.monotonic() - t0) * 1000)
                        _log.info(
                            "litellm_timings req=%s model=%s connect_ms=%d ttft_ms=%d",
                            request_id,
                            kwargs["model"],
                            connect_ms,
                            ttft_ms,
                        )
                        first_chunk_logged = True
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    if delta and getattr(delta, "content", None):
                        yield LLMDelta(text_delta=delta.content)

                    if delta and getattr(delta, "tool_calls", None):
                        for tc in delta.tool_calls:
                            idx = tc.index if hasattr(tc, "index") else 0
                            buf = tool_buf.setdefault(idx, {"id": None, "name": None, "args": ""})
                            if tc.id:
                                buf["id"] = tc.id
                            if tc.function and tc.function.name:
                                buf["name"] = tc.function.name
                            if tc.function and tc.function.arguments:
                                buf["args"] += tc.function.arguments

                    if choice.finish_reason:
                        for buf in tool_buf.values():
                            if not buf["name"]:
                                continue
                            try:
                                args = json.loads(buf["args"] or "{}")
                            except json.JSONDecodeError:
                                args = {"_raw": buf["args"]}
                            yield LLMDelta(
                                tool_call=ToolCall(
                                    id=buf["id"] or "", name=buf["name"], arguments=args
                                )
                            )
                        yield LLMDelta(finish=_map_finish(choice.finish_reason))

                    usage = getattr(chunk, "usage", None)
                    if usage:
                        yield LLMDelta(
                            usage=LLMUsage(
                                tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                                tokens_out=getattr(usage, "completion_tokens", 0) or 0,
                            )
                        )
            except Exception as exc:
                # CancelledError is BaseException and bypasses this — we WANT
                # cancellation to propagate so the finally below closes the
                # upstream HTTP stream, but we don't want to wrap it.
                raise LLMUnavailableError(f"litellm stream error: {exc}") from exc
        finally:
            # When the caller cancels mid-stream (TCP close, RPC cancel, …),
            # the underlying HTTP connection would otherwise be orphaned and
            # keep pulling tokens we'll never read — billing against a
            # disconnected client. Closing the response (when the provider
            # exposes aclose) tears the upstream socket down immediately.
            aclose = getattr(response, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

    async def warmup(self, *, timeout_s: float = 10.0) -> bool:
        """Prime DNS/TCP/TLS/client cache with a tiny non-streaming request."""
        now = time.monotonic()
        msg = ChatMessage(
            id="warmup",
            role=MessageRole.USER,
            content=".",
            created_at=datetime.now(timezone.utc),
        )
        kwargs = self._completion_kwargs(
            messages=[msg],
            temperature=0,
            stream=False,
            timeout_s=timeout_s,
        )
        kwargs["max_tokens"] = 1
        try:
            await litellm.acompletion(**kwargs)
        except Exception as exc:
            _log.warning(
                "litellm_warmup failed model=%s elapsed_ms=%d err=%s",
                self._model,
                int((time.monotonic() - now) * 1000),
                exc,
            )
            return False
        _log.info(
            "litellm_warmup ok model=%s elapsed_ms=%d",
            self._model,
            int((time.monotonic() - now) * 1000),
        )
        return True

    async def close(self) -> None:
        await close_shared_client()

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        try:
            return litellm.token_counter(model=self._model, messages=[_to_msg(m) for m in messages])
        except Exception:
            return sum(max(1, len(m.content) // 3) for m in messages)

    def _completion_kwargs(
        self,
        *,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float,
        stream: bool,
        timeout_s: float,
    ) -> dict:
        if self._shared_http_client:
            _ensure_shared_client(trust_env=self._trust_env)
        kwargs: dict = {
            "model": model or self._model,
            "messages": [_to_msg(m) for m in messages],
            "temperature": temperature,
            "stream": stream,
            "timeout": timeout_s,
            "max_retries": self._max_retries,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base
        return kwargs


def _ensure_shared_client(*, trust_env: bool) -> httpx.AsyncClient:
    """Install one reusable HTTPX client for LiteLLM/OpenAI-compatible calls."""
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            trust_env=trust_env,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=60.0,
            ),
            follow_redirects=True,
        )
    litellm.aclient_session = _shared_http_client
    return _shared_http_client


async def close_shared_client() -> None:
    global _shared_http_client
    if _shared_http_client is not None and not _shared_http_client.is_closed:
        await _shared_http_client.aclose()
    if litellm.aclient_session is _shared_http_client:
        litellm.aclient_session = None
    _shared_http_client = None


def _to_msg(m: ChatMessage) -> dict:
    role = {
        MessageRole.SYSTEM: "system",
        MessageRole.USER: "user",
        MessageRole.ASSISTANT: "assistant",
        MessageRole.TOOL: "tool",
        MessageRole.PROACTIVE: "user",
    }.get(m.role, "user")
    out: dict = {"role": role, "content": m.content}
    if m.role is MessageRole.TOOL:
        out["tool_call_id"] = m.tool_call_id
        out["name"] = m.tool_name
    return out


def _map_finish(r: str) -> LLMFinishReason:
    return {
        "stop": LLMFinishReason.STOP,
        "length": LLMFinishReason.LENGTH,
        "tool_calls": LLMFinishReason.TOOL_CALLS,
        "content_filter": LLMFinishReason.CONTENT_FILTER,
    }.get(r, LLMFinishReason.STOP)
