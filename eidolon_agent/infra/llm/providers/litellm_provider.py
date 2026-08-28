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
from eidolon_sdk.integrations.llm import render_openai_tool_calls, validate_openai_tool_transcript

from eidolon_agent.core.errors import LLMUnavailableError
from eidolon_agent.core.types.llm import LLMActivityKind, LLMDelta, LLMFinishReason, LLMUsage
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolCall, ToolSchema

litellm.drop_params = True

_log = logging.getLogger(__name__)
_shared_http_client: httpx.AsyncClient | None = None
# Monotonic timestamp of the previous acompletion *start*, process-wide. Used
# only for F1 diagnostics: correlating a slow connect_ms with the idle gap
# since the last call distinguishes a cold-connection / cold-upstream-worker
# spike (large connect after large idle) from upstream queueing/load (large
# connect after a short idle). Concurrency races are acceptable for a
# diagnostic gauge.
_last_call_monotonic: float | None = None
_ACTIVITY_HEARTBEAT_S = 1.0


def _idle_ms_and_mark() -> int:
    """Return ms since the previous call start and mark 'now' as the latest."""
    global _last_call_monotonic
    now = time.monotonic()
    idle_ms = -1 if _last_call_monotonic is None else int((now - _last_call_monotonic) * 1000)
    _last_call_monotonic = now
    return idle_ms


def _reasoning_content(delta) -> str | None:
    """Read OpenAI-compatible reasoning fields without exposing their text.

    LiteLLM providers have used both a direct ``reasoning_content`` attribute
    and ``provider_specific_fields``/``model_extra`` dictionaries. Returning
    the value only lets this adapter classify activity; callers receive only
    :class:`LLMActivityKind`, never the private chain-of-thought text.
    """

    if delta is None:
        return None
    if isinstance(delta, dict):
        value = delta.get("reasoning_content") or delta.get("reasoning")
        return value if isinstance(value, str) and value else None
    direct = getattr(delta, "reasoning_content", None)
    if isinstance(direct, str) and direct:
        return direct
    for attr in ("provider_specific_fields", "model_extra"):
        extra = getattr(delta, attr, None)
        if isinstance(extra, dict):
            value = extra.get("reasoning_content") or extra.get("reasoning")
            if isinstance(value, str) and value:
                return value
    return None


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
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._api_base = api_base
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._shared_http_client = shared_http_client

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
        idle_ms = _idle_ms_and_mark()
        t0 = time.monotonic()
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            connect_ms = int((time.monotonic() - t0) * 1000)
            _log.warning(
                "litellm acompletion failed req=%s model=%s connect_ms=%d idle_ms=%d err=%s",
                request_id,
                kwargs["model"],
                connect_ms,
                idle_ms,
                exc,
            )
            raise LLMUnavailableError(f"litellm call failed: {exc}") from exc
        connect_ms = int((time.monotonic() - t0) * 1000)
        first_raw_chunk_ms: int | None = None
        first_effective_delta_logged = False
        first_reasoning_ms: int | None = None
        reasoning_chunk_count = 0
        last_activity_emit_at: float | None = None
        raw_chunk_count = 0
        last_finish_reason: str | None = None
        stream_outcome = "interrupted"

        try:
            try:
                async for chunk in response:
                    raw_chunk_count += 1
                    if first_raw_chunk_ms is None:
                        first_raw_chunk_ms = int((time.monotonic() - t0) * 1000)
                        _log.info(
                            "litellm_stream_first_chunk req=%s model=%s connect_ms=%d "
                            "raw_ttft_ms=%d idle_ms=%d choices=%d",
                            request_id,
                            kwargs["model"],
                            connect_ms,
                            first_raw_chunk_ms,
                            idle_ms,
                            len(chunk.choices or []),
                        )
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if choice.finish_reason:
                        last_finish_reason = str(choice.finish_reason)

                    content = getattr(delta, "content", None) if delta else None
                    reasoning_content = _reasoning_content(delta)
                    tool_call_deltas = getattr(delta, "tool_calls", None) if delta else None
                    if reasoning_content:
                        reasoning_chunk_count += 1
                        if first_reasoning_ms is None:
                            first_reasoning_ms = int((time.monotonic() - t0) * 1000)
                            _log.info(
                                "litellm_reasoning_activity req=%s model=%s reasoning_ttft_ms=%d "
                                "raw_ttft_ms=%d",
                                request_id,
                                kwargs["model"],
                                first_reasoning_ms,
                                first_raw_chunk_ms,
                            )
                        now = time.monotonic()
                        if (
                            last_activity_emit_at is None
                            or now - last_activity_emit_at >= _ACTIVITY_HEARTBEAT_S
                        ):
                            last_activity_emit_at = now
                            yield LLMDelta(activity=LLMActivityKind.REASONING)
                    has_text = bool(content and content.strip())
                    has_tool_call_fragment = bool(
                        tool_call_deltas
                        and any(
                            getattr(tc, "id", None)
                            or (
                                getattr(tc, "function", None)
                                and (
                                    getattr(tc.function, "name", None)
                                    or getattr(tc.function, "arguments", None)
                                )
                            )
                            for tc in tool_call_deltas
                        )
                    )
                    if not first_effective_delta_logged and (has_text or has_tool_call_fragment):
                        effective_ttft_ms = int((time.monotonic() - t0) * 1000)
                        _log.info(
                            "litellm_timings req=%s model=%s connect_ms=%d ttft_ms=%d "
                            "raw_ttft_ms=%d raw_chunks_before_effective=%d idle_ms=%d kind=%s",
                            request_id,
                            kwargs["model"],
                            connect_ms,
                            effective_ttft_ms,
                            first_raw_chunk_ms,
                            raw_chunk_count - 1,
                            idle_ms,
                            "text" if has_text else "tool_call",
                        )
                        first_effective_delta_logged = True

                    if content:
                        yield LLMDelta(text_delta=content)

                    if tool_call_deltas:
                        now = time.monotonic()
                        if (
                            has_tool_call_fragment
                            and (
                                last_activity_emit_at is None
                                or now - last_activity_emit_at >= _ACTIVITY_HEARTBEAT_S
                            )
                        ):
                            last_activity_emit_at = now
                            yield LLMDelta(activity=LLMActivityKind.TOOL_CALL)
                        for tc in tool_call_deltas:
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
                stream_outcome = "completed"
            except Exception as exc:
                stream_outcome = "error"
                # CancelledError is BaseException and bypasses this — we WANT
                # cancellation to propagate so the finally below closes the
                # upstream HTTP stream, but we don't want to wrap it.
                raise LLMUnavailableError(f"litellm stream error: {exc}") from exc
        finally:
            if not first_effective_delta_logged:
                _log.info(
                    "litellm_stream_no_effective_delta req=%s model=%s connect_ms=%d "
                    "elapsed_ms=%d raw_ttft_ms=%d raw_chunks=%d finish_reason=%s outcome=%s",
                    request_id,
                    kwargs["model"],
                    connect_ms,
                    int((time.monotonic() - t0) * 1000),
                    -1 if first_raw_chunk_ms is None else first_raw_chunk_ms,
                    raw_chunk_count,
                    last_finish_reason or "none",
                    stream_outcome,
                )
            if reasoning_chunk_count:
                _log.info(
                    "litellm_reasoning_summary req=%s model=%s first_reasoning_ms=%d "
                    "reasoning_chunks=%d outcome=%s",
                    request_id,
                    kwargs["model"],
                    -1 if first_reasoning_ms is None else first_reasoning_ms,
                    reasoning_chunk_count,
                    stream_outcome,
                )
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
            _ensure_shared_client()
        openai_messages = [_to_msg(m) for m in messages]
        validate_openai_tool_transcript(openai_messages)
        kwargs: dict = {
            "model": model or self._model,
            "messages": openai_messages,
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


def _ensure_shared_client() -> httpx.AsyncClient:
    """Install one reusable HTTPX client for LiteLLM/OpenAI-compatible calls.

    Proxy policy is owned by the deployment layer (supervisord NO_PROXY for
    loopback bypass; the user's HTTP_PROXY/HTTPS_PROXY for external traffic
    when needed — e.g. operators in regions that require a VPN to reach a
    public LLM endpoint). httpx defaults (``trust_env=True``) transparently
    honor that policy without forcing per-call hardcoding here.
    """
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
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
    if m.role is MessageRole.ASSISTANT and m.tool_calls:
        out["tool_calls"] = render_openai_tool_calls(m.tool_calls)
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
