"""Minimal shared Gemini inference helper for agent tooling.

This is intentionally small and geared toward synthetic data generation,
filtering, and rubric-style judging workloads.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
import os
import random
import threading
import time
from typing import Any, Iterable

try:
    from google import genai
    from google.genai import types
except ImportError as exc:  # pragma: no cover - import guard for clean failure
    raise ImportError(
        "google-genai is required. Install it with `uv add google-genai`."
    ) from exc


GEMINI_3_1_PRO_PREVIEW = "gemini-3.1-pro-preview"
GEMINI_3_1_PRO_PREVIEW_CUSTOMTOOLS = "gemini-3.1-pro-preview-customtools"
GEMINI_3_FLASH_PREVIEW = "gemini-3-flash-preview"
GEMINI_EMBEDDING_2_PREVIEW = "gemini-embedding-2-preview"
DEFAULT_EMBEDDING_DIM = 1536

# Official Google docs checked on 2026-04-05:
# - Gemini 3 Developer Guide: Gemini 3.1 Pro supports `low`/`medium`/`high`;
#   Gemini 3 Flash and Gemini 3.1 Flash-Lite support
#   `minimal`/`low`/`medium`/`high`.
# - Live API capabilities: Gemini 3.1 Flash Live uses `thinkingLevel` with
#   `minimal`/`low`/`medium`/`high`, defaulting to `minimal` for lowest latency.
# - No public Google doc page was found for `gemini-3.1-pro-preview-customtools`.
#   Treat it as a custom-tools variant of `gemini-3.1-pro-preview`, so the
#   safest assumption is `low`/`medium`/`high` only. This last point is an
#   inference, not a directly documented capability table.
#
# Sources:
# https://ai.google.dev/gemini-api/docs/gemini-3
# https://ai.google.dev/gemini-api/docs/thinking
# https://ai.google.dev/gemini-api/docs/live-api/capabilities
#
# Gemini 3 temperature policy (Gemini 3 Developer Guide):
# Google strongly recommends keeping temperature at the API default (1.0).
# Gemini 3 reasoning is tuned for that default; lowering temperature can
# cause looping or degraded performance on complex / structured tasks.
# Prefer `thinking_level` plus JSON schema constraints for determinism — do
# not pass `temperature` from call sites. `None` omits the field entirely.
#
# Gemini 3 thinking_level guidance (Gemini 3 Developer Guide):
# Use `thinking_level` — not temperature — to trade latency/cost vs depth.
# Levels are relative guidelines, not strict token budgets. Do not combine
# `thinking_level` with legacy `thinking_budget` in the same request.
#   minimal — Flash / Flash-Lite only; near-no-thinking for chat/throughput.
#   low     — simple instruction-following, classification, structured JSON.
#   medium  — balanced default for most pipeline tasks (brief, judges).
#   high    — deep reasoning; slower time-to-first-token (API default when unset).
# Model allowlists (same doc):
#   gemini-3.1-pro-preview: low | medium | high (default: high)
#   gemini-3-flash-preview: minimal | low | medium | high (default: high)
# Pick explicitly at call sites when latency or depth matters; omit only when
# the API default (high) is acceptable.

# Default to the best general text model in the current Gemini 3 series.
DEFAULT_GEMINI_MODEL = GEMINI_3_FLASH_PREVIEW

GEMINI_MODELS = {
    "default": DEFAULT_GEMINI_MODEL,
    "pro": GEMINI_3_1_PRO_PREVIEW,
    "pro_customtools": GEMINI_3_1_PRO_PREVIEW_CUSTOMTOOLS,
    "flash": GEMINI_3_FLASH_PREVIEW,
}


@dataclass(slots=True)
class GeminiResult:
    text: str
    model: str
    latency_seconds: float
    response: Any


@dataclass(slots=True)
class GeminiRequest:
    contents: str | list[Any]
    model: str = DEFAULT_GEMINI_MODEL
    system_instruction: str | None = None
    # Omit from API requests for Gemini 3 — see module temperature policy above.
    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking_level: str | None = None
    response_mime_type: str | None = None
    response_json_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class GeminiBatchResult:
    index: int
    request: GeminiRequest
    result: GeminiResult | None
    error: str | None
    attempts: int


class GeminiRateLimiter:
    """Simple sliding-window request limiter keyed on requests/minute."""

    def __init__(self, requests_per_minute: int | None = None) -> None:
        self.requests_per_minute = requests_per_minute
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if not self.requests_per_minute or self.requests_per_minute <= 0:
            return

        while True:
            with self._lock:
                now = time.monotonic()
                window_start = now - 60.0
                while self._timestamps and self._timestamps[0] < window_start:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.requests_per_minute:
                    self._timestamps.append(now)
                    return

                sleep_for = max(0.05, 60.0 - (now - self._timestamps[0]))
            time.sleep(sleep_for)


def _resolve_api_key(api_key: str | None = None) -> str:
    key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key or not key.strip():
        raise RuntimeError(
            "Missing Gemini API key. Set GEMINI_API_KEY or GOOGLE_API_KEY."
        )
    return key


_client_lock = threading.Lock()
_client_cache: dict[str, genai.Client] = {}


def _build_client(api_key: str) -> genai.Client:
    with _client_lock:
        if api_key not in _client_cache:
            _client_cache[api_key] = genai.Client(api_key=api_key)
        return _client_cache[api_key]


def get_gemini_client(api_key: str | None = None) -> genai.Client:
    return _build_client(_resolve_api_key(api_key))


def _extract_text(response: object) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    gathered: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                gathered.append(part_text.strip())
                continue
            if isinstance(part, dict):
                raw_text = part.get("text")
                if isinstance(raw_text, str) and raw_text.strip():
                    gathered.append(raw_text.strip())

    return "\n".join(gathered).strip()


def _build_config(
    *,
    system_instruction: str | None,
    temperature: float | None,
    max_output_tokens: int | None,
    thinking_level: str | None,
    response_mime_type: str | None,
    response_json_schema: dict[str, Any] | None,
) -> types.GenerateContentConfig:
    config_kwargs: dict[str, Any] = {}
    if system_instruction is not None:
        config_kwargs["system_instruction"] = system_instruction
    # Only send temperature when explicitly set. Gemini 3 call sites should
    # leave it None so the API default (1.0) applies per Google guidance.
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if max_output_tokens is not None:
        config_kwargs["max_output_tokens"] = max_output_tokens
    if response_mime_type is not None:
        config_kwargs["response_mime_type"] = response_mime_type
    if response_json_schema is not None:
        config_kwargs["response_json_schema"] = response_json_schema
    if thinking_level is not None:
        # Do not pass arbitrary values here. Gemini 3 models validate
        # `thinking_level` against a model-specific allowlist.
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=thinking_level
        )
    return types.GenerateContentConfig(**config_kwargs)


def _is_retryable_exception(exc: Exception) -> bool:
    message = str(exc).lower()
    retry_markers = (
        "429",
        "rate limit",
        "resource exhausted",
        "quota exceeded",
        "deadline exceeded",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "503",
        "500",
        "502",
        "504",
        "internal error",
        "service unavailable",
        "unavailable",
    )
    return any(marker in message for marker in retry_markers)


def _generate_from_request(
    request: GeminiRequest,
    *,
    api_key: str | None = None,
    rate_limiter: GeminiRateLimiter | None = None,
) -> GeminiResult:
    if rate_limiter is not None:
        rate_limiter.acquire()

    start = time.perf_counter()
    response = get_gemini_client(api_key).models.generate_content(
        model=request.model,
        contents=request.contents,
        config=_build_config(
            system_instruction=request.system_instruction,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            thinking_level=request.thinking_level,
            response_mime_type=request.response_mime_type,
            response_json_schema=request.response_json_schema,
        ),
    )
    latency_seconds = time.perf_counter() - start
    return GeminiResult(
        text=_extract_text(response),
        model=request.model,
        latency_seconds=latency_seconds,
        response=response,
    )


def generate_text_with_retry(
    contents: str | list[Any],
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    system_instruction: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    thinking_level: str | None = None,
    response_mime_type: str | None = None,
    response_json_schema: dict[str, Any] | None = None,
    api_key: str | None = None,
    max_retries: int = 4,
    initial_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
    requests_per_minute: int | None = None,
    rate_limiter: GeminiRateLimiter | None = None,
) -> GeminiResult:
    request = GeminiRequest(
        contents=contents,
        model=model,
        system_instruction=system_instruction,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        thinking_level=thinking_level,
        response_mime_type=response_mime_type,
        response_json_schema=response_json_schema,
    )
    limiter = rate_limiter or GeminiRateLimiter(requests_per_minute)
    attempts = 0

    while True:
        attempts += 1
        try:
            return _generate_from_request(
                request,
                api_key=api_key,
                rate_limiter=limiter,
            )
        except Exception as exc:
            if attempts > max_retries or not _is_retryable_exception(exc):
                raise
            backoff_seconds = min(
                max_backoff_seconds,
                initial_backoff_seconds * (2 ** (attempts - 1)),
            )
            time.sleep(backoff_seconds * random.uniform(0.8, 1.2))


def batch_generate_texts(
    requests: Iterable[GeminiRequest],
    *,
    api_key: str | None = None,
    max_workers: int = 4,
    max_retries: int = 4,
    initial_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
    requests_per_minute: int | None = None,
    rate_limiter: GeminiRateLimiter | None = None,
    fail_fast: bool = False,
) -> list[GeminiBatchResult]:
    """Run multiple Gemini requests in parallel while preserving input order."""
    request_list = list(requests)
    if not request_list:
        return []

    results: list[GeminiBatchResult | None] = [None] * len(request_list)
    limiter = rate_limiter or GeminiRateLimiter(requests_per_minute)

    def _run_one(index: int, request: GeminiRequest) -> GeminiBatchResult:
        attempts = 0
        while True:
            attempts += 1
            try:
                result = _generate_from_request(
                    request,
                    api_key=api_key,
                    rate_limiter=limiter,
                )
                return GeminiBatchResult(
                    index=index,
                    request=request,
                    result=result,
                    error=None,
                    attempts=attempts,
                )
            except Exception as exc:
                if attempts > max_retries or not _is_retryable_exception(exc):
                    return GeminiBatchResult(
                        index=index,
                        request=request,
                        result=None,
                        error=str(exc),
                        attempts=attempts,
                    )
                backoff_seconds = min(
                    max_backoff_seconds,
                    initial_backoff_seconds * (2 ** (attempts - 1)),
                )
                time.sleep(backoff_seconds * random.uniform(0.8, 1.2))

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        future_to_index = {
            executor.submit(_run_one, index, request): index
            for index, request in enumerate(request_list)
        }
        for future in as_completed(future_to_index):
            batch_result = future.result()
            results[batch_result.index] = batch_result
            if fail_fast and batch_result.error:
                for pending in future_to_index:
                    pending.cancel()
                raise RuntimeError(
                    f"Gemini batch request {batch_result.index} failed: {batch_result.error}"
                )

    return [result for result in results if result is not None]


def generate_text(
    contents: str | list[Any],
    *,
    model: str = DEFAULT_GEMINI_MODEL,
    system_instruction: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    thinking_level: str | None = None,
    response_mime_type: str | None = None,
    response_json_schema: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> GeminiResult:
    """Run one Gemini text generation call and return text plus raw response.

    `thinking_level` is passed through for Gemini 3-series models that support it.

    Official doc compatibility checked on 2026-04-05:
    - `gemini-3.1-pro-preview`: `low`, `medium`, `high`
    - `gemini-3.1-pro-preview-customtools`: no public capability table found;
      inferred to match Pro (`low`, `medium`, `high`)
    - `gemini-3-flash-preview`: `minimal`, `low`, `medium`, `high`
    - `gemini-3.1-flash-lite-preview`: `minimal`, `low`, `medium`, `high`
    - `gemini-3.1-flash-live-preview`: Live API docs list
      `minimal`, `low`, `medium`, `high` with default `minimal`

    Caveat: `gemini-3.1-flash-live-preview` is primarily documented for the
    Live API, while this helper uses `models.generate_content`.
    """
    request = GeminiRequest(
        contents=contents,
        model=model,
        system_instruction=system_instruction,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        thinking_level=thinking_level,
        response_mime_type=response_mime_type,
        response_json_schema=response_json_schema,
    )
    return _generate_from_request(request, api_key=api_key)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EmbeddingResult:
    embeddings: list[list[float]]
    model: str
    latency_seconds: float


def embed_content(
    contents: str | list[str],
    *,
    model: str = GEMINI_EMBEDDING_2_PREVIEW,
    output_dimensionality: int = DEFAULT_EMBEDDING_DIM,
    task_type: str = "CLUSTERING",
    api_key: str | None = None,
) -> EmbeddingResult:
    """Embed one or more texts via Gemini embedding API.

    Returns an EmbeddingResult whose `.embeddings` is a list of float vectors,
    one per input text (or one if a single string was passed).
    """
    client = get_gemini_client(api_key)
    start = time.perf_counter()
    response = client.models.embed_content(
        model=model,
        contents=contents if isinstance(contents, list) else [contents],
        config={
            "output_dimensionality": output_dimensionality,
            "task_type": task_type,
        },
    )
    latency = time.perf_counter() - start
    return EmbeddingResult(
        embeddings=[list(emb.values) for emb in response.embeddings],
        model=model,
        latency_seconds=latency,
    )


def batch_embed_contents(
    text_batches: Iterable[list[str]],
    *,
    model: str = GEMINI_EMBEDDING_2_PREVIEW,
    output_dimensionality: int = DEFAULT_EMBEDDING_DIM,
    task_type: str = "CLUSTERING",
    api_key: str | None = None,
    max_workers: int = 4,
    max_retries: int = 4,
    initial_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
    requests_per_minute: int | None = None,
    rate_limiter: GeminiRateLimiter | None = None,
) -> list[EmbeddingResult]:
    """Embed multiple batches of texts in parallel with retry and rate limiting.

    Each element in *text_batches* is a list of strings sent as one
    ``embed_content`` call.  Returns results in input order.
    """
    batches = list(text_batches)
    if not batches:
        return []

    limiter = rate_limiter or GeminiRateLimiter(requests_per_minute)
    results: list[EmbeddingResult | None] = [None] * len(batches)

    def _run(index: int, texts: list[str]) -> tuple[int, EmbeddingResult]:
        attempts = 0
        while True:
            attempts += 1
            limiter.acquire()
            try:
                r = embed_content(
                    texts,
                    model=model,
                    output_dimensionality=output_dimensionality,
                    task_type=task_type,
                    api_key=api_key,
                )
                return index, r
            except Exception as exc:
                if attempts > max_retries or not _is_retryable_exception(exc):
                    raise
                backoff = min(
                    max_backoff_seconds,
                    initial_backoff_seconds * (2 ** (attempts - 1)),
                )
                time.sleep(backoff * random.uniform(0.8, 1.2))

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {
            executor.submit(_run, i, batch): i for i, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            idx, result = future.result()
            results[idx] = result

    return [r for r in results if r is not None]
