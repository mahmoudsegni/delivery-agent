"""Shared raw-Groq HTTP client for the executive-layer features.

The raw httpx path to Groq is the one reliable LLM route with the current
keys (litellm has no Groq branch), so new features (morning brief, autopilot
triage, ask-the-DM) call Groq through here instead of copying the boilerplate
again. Mirrors the proven settings of ``standup_service._analyze_standup_json``
and the ``/agent/analyze`` endpoint: gpt-oss ``reasoning_effort``, 60s timeout,
and empty-content handling (a reasoning model can exhaust ``max_tokens`` on
reasoning and return empty content with finish_reason="length" — surfaced as a
:class:`GroqError`, never a silent empty string). Existing call sites are
intentionally untouched.
"""

import asyncio
import json
import logging
import random
import time

import httpx

from app.config import settings
from app.integrations.demo_llm import demo_completion

logger = logging.getLogger(__name__)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

# Transient failures worth one more attempt; every other status fails fast.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    """Backoff for retry `attempt` (0-based): 0.5 * 2**attempt s ±25% jitter.

    A Retry-After header in seconds wins, capped at 5s so a bad header can't
    stall the request.
    """
    if response is not None:
        try:
            return min(float(response.headers["Retry-After"]), 5.0)
        except (KeyError, ValueError):
            pass
    return 0.5 * 2**attempt * random.uniform(0.75, 1.25)


class GroqError(RuntimeError):
    """Raised when Groq is unconfigured, unreachable, or returns no usable content."""


async def _record_usage(
    feature: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    latency_ms: int,
) -> None:
    """Persist one ledger row. A logging failure must NEVER break the LLM call."""
    try:
        # Imported here so the client stays importable without a DB (tests,
        # CLI tools) — engine creation is lazy anyway.
        from app.database import async_session_maker
        from app.models.llm_call import LLMCall

        async with async_session_maker() as session:
            session.add(
                LLMCall(
                    feature=feature,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                )
            )
            await session.commit()
    except Exception:  # ledger is best-effort by design
        logger.warning("LLM usage ledger write failed", exc_info=True)


def extract_json(text: str) -> dict:
    """Extract the first balanced JSON object from an LLM response."""
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object in response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("Incomplete JSON object in response")


async def groq_chat(
    messages: list[dict],
    *,
    temperature: float = 0.4,
    max_tokens: int | None = None,
    feature: str = "other",
) -> str:
    """Send a chat completion to Groq and return the response content.

    In DEMO_MODE, replay a pre-recorded response instead (no API key needed).
    Successful calls (demo included) are logged to the ``llm_calls`` ledger
    tagged with ``feature``.

    Raises :class:`GroqError` on a missing key, transport failure, non-200
    response, or empty content. Transient failures (429/5xx, transport
    errors) are retried up to 2 times with backoff before giving up.
    """
    if settings.demo_mode:
        await _record_usage(feature, "demo", 0, 0, 0, 0)
        return demo_completion(messages)

    if not settings.groq_api_key:
        raise GroqError("GROQ_API_KEY not configured")

    payload: dict = {
        "model": settings.groq_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or settings.groq_max_tokens,
    }
    # gpt-oss reasoning models accept reasoning_effort; sending it to a
    # non-reasoning model would be rejected, so only attach it for those.
    if "gpt-oss" in settings.groq_model:
        payload["reasoning_effort"] = settings.groq_reasoning_effort

    # ponytail: fixed 3 attempts, plain loop — circuit breaker only if Groq
    # outages ever make this the bottleneck.
    response: httpx.Response | None = None
    t0 = time.monotonic()
    for attempt in range(3):
        t0 = time.monotonic()  # resets per attempt: latency excludes backoff sleeps
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    GROQ_CHAT_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {settings.groq_api_key}",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            # Transport errors (connect/read) are transient; other HTTPError
            # subclasses are not worth another attempt.
            if isinstance(exc, httpx.TransportError) and attempt < 2:
                delay = _retry_delay(attempt, None)
                logger.warning(f"Groq request failed ({exc}), retrying in {delay:.2f}s")
                await asyncio.sleep(delay)
                continue
            logger.error(f"Groq API request failed: {exc}")
            raise GroqError(f"Groq API request failed: {exc}") from exc

        if response.status_code == 200:
            break

        try:
            detail = response.json().get("error", {}).get("message", "Unknown error")
        except Exception:  # noqa: BLE001 - error body may not be JSON
            detail = response.text[:200]

        if response.status_code in RETRYABLE_STATUSES and attempt < 2:
            delay = _retry_delay(attempt, response)
            logger.warning(
                f"Groq API {response.status_code} ({detail}), retrying in {delay:.2f}s"
            )
            await asyncio.sleep(delay)
            continue

        logger.error(f"Groq API error: {response.status_code} - {detail}")
        raise GroqError(f"Groq API error: {detail}")

    assert response is not None  # loop exits only via break-on-200 or raise
    body = response.json()
    usage = body.get("usage") or {}
    await _record_usage(
        feature,
        payload["model"],
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        usage.get("total_tokens", 0),
        round((time.monotonic() - t0) * 1000),
    )
    choice = body["choices"][0]
    content = choice["message"].get("content") or ""
    finish_reason = choice.get("finish_reason")

    # A reasoning model can exhaust max_tokens on reasoning and return
    # empty/truncated content with finish_reason="length". Surface that as an
    # error instead of handing callers an empty string.
    if not content.strip():
        logger.error(f"Groq returned empty content (finish_reason={finish_reason})")
        raise GroqError(
            "Groq returned an empty response "
            f"(finish_reason={finish_reason}). The model likely ran out of "
            "token budget; try a shorter prompt or raise GROQ_MAX_TOKENS."
        )
    if finish_reason == "length":
        logger.warning("Groq response truncated (finish_reason=length)")

    return content


async def groq_json(
    prompt: str,
    system_prompt: str,
    *,
    temperature: float = 0.3,
    feature: str = "other",
) -> dict:
    """Call Groq for a structured answer and return the parsed JSON object."""
    content = await groq_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        feature=feature,
    )
    try:
        return extract_json(content)
    except ValueError as exc:  # includes json.JSONDecodeError
        logger.error(f"Groq response was not valid JSON: {exc}")
        raise GroqError(f"Groq response was not valid JSON: {exc}") from exc
