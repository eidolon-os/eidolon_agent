"""HTTP implementation of the participation contract, independent of model family."""

import httpx
from eidolon_sdk.biz.participation import DecisionRequest, DecisionResult, validate_proposal

from eidolon_agent.core.ports.participation import DecisionUnavailable


class HttpParticipationDecision:
    def __init__(self, url: str, *, token: str = "", transport=None):
        self._url, self._token, self._transport = url, token, transport

    async def __call__(self, request: DecisionRequest) -> DecisionResult:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            async with httpx.AsyncClient(
                timeout=request.timeout_ms / 1000, transport=self._transport,
                follow_redirects=False, trust_env=False,
            ) as client, client.stream(
                "POST", self._url, json=request.model_dump(mode="json"), headers=headers,
            ) as response:
                if response.status_code != 200:
                    raise DecisionUnavailable(f"DECISION_HTTP_{response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 262144:
                        raise DecisionUnavailable("DECISION_RESPONSE_TOO_LARGE")
            result = DecisionResult.model_validate_json(bytes(body))
            validate_proposal(request, result)
            return result
        except httpx.TimeoutException as exc:
            raise DecisionUnavailable("DECISION_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise DecisionUnavailable("DECISION_TRANSPORT_ERROR") from exc
        except ValueError as exc:
            raise DecisionUnavailable("DECISION_INVALID_RESULT") from exc
