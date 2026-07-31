"""Provider-swappable LLM and embedding access.

One interface, three providers, chosen by env var. The point is that the agent,
the ingestion tagger, and the persona extractor never learn which vendor is
behind them — so whichever API key is available, nothing else in the codebase
changes.

Each provider is called over plain HTTP rather than its vendor SDK: three SDKs
with three async styles and three dependency trees, to hide all of them behind
one method, is a worse trade than owning ~40 lines of request shaping. The wire
formats differ in ways that matter and are handled per-provider below.

`echo` and `hash` are offline stand-ins so the whole application — ingestion,
retrieval, chat — runs end-to-end in tests and local dev with no API key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import EMBED_DIM, settings

log = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-opus-5",
}
DEFAULT_EMBED_MODELS = {
    # gemini-embedding-001 is natively 3072-dim and supports Matryoshka
    # truncation, so `outputDimensionality` trims it to EMBED_DIM. Truncated
    # vectors lose unit length, which is why every provider's output is
    # re-normalised before it reaches pgvector — cosine distance depends on it.
    "gemini": "gemini-embedding-001",
    "openai": "text-embedding-3-small",
}


class LLMError(RuntimeError):
    """Raised when a provider call fails in a way worth retrying."""


class LLMRefusal(RuntimeError):
    """The provider's safety layer declined the request."""


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class Completion:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


_retry = retry(
    retry=retry_if_exception_type((LLMError, httpx.TransportError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)


# --------------------------------------------------------------------------- #
# Chat completion
# --------------------------------------------------------------------------- #
@_retry
async def complete(
    system: str,
    messages: Sequence[Message],
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> Completion:
    """Single-turn-or-multi-turn completion, provider-agnostic.

    `json_mode` asks the provider for strict JSON where it supports a native
    switch, and falls back to prompt instruction plus extraction where it
    does not. Callers should still parse defensively via `parse_json`.
    """
    provider = settings.llm_provider
    model = settings.llm_model or DEFAULT_MODELS.get(provider, "")
    limit = max_tokens or settings.llm_max_tokens

    if provider == "echo":
        return _echo_completion(system, messages, json_mode=json_mode)
    if provider == "gemini":
        return await _gemini_complete(model, system, messages, json_mode, limit)
    if provider == "openai":
        return await _openai_complete(model, system, messages, json_mode, limit)
    if provider == "anthropic":
        return await _anthropic_complete(model, system, messages, json_mode, limit)
    raise LLMError(f"unknown llm_provider {provider!r}")


async def _gemini_complete(
    model: str,
    system: str,
    messages: Sequence[Message],
    json_mode: bool,
    max_tokens: int,
) -> Completion:
    generation: dict[str, Any] = {
        "temperature": settings.llm_temperature,
        "maxOutputTokens": max_tokens,
    }
    if json_mode:
        generation["responseMimeType"] = "application/json"
        # Gemini 2.5 reasons before answering by default, and those thinking
        # tokens are drawn from maxOutputTokens. For structured extraction the
        # budget was spent reasoning and the JSON came back truncated — it
        # parsed as far as the opening brace and then failed, so sentiment
        # tagging and persona extraction both returned nothing without ever
        # raising. Disabled here because schema-filling needs no deliberation;
        # the conversational path keeps it.
        generation["thinkingConfig"] = {"thinkingBudget": 0}

    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        # Gemini calls the assistant role "model".
        "contents": [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
        ],
        "generationConfig": generation,
    }

    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, params={"key": settings.google_api_key}, json=payload)
    if resp.status_code != 200:
        raise LLMError(f"gemini {resp.status_code}: {resp.text[:400]}")

    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        # Gemini reports prompt-level blocks here rather than as an HTTP error.
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise LLMRefusal(f"gemini returned no candidates (blockReason={reason})")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)

    # A truncated response is otherwise indistinguishable from a bad one: the
    # call succeeds, the text is merely incomplete, and the caller sees only a
    # parse failure with no cause. Surfacing it as an error makes the retry
    # meaningful and the log honest.
    finish = candidates[0].get("finishReason")
    if finish == "MAX_TOKENS":
        raise LLMError(
            f"gemini response truncated at {max_tokens} tokens "
            f"(usage: {data.get('usageMetadata')}) — raise max_tokens"
        )

    return Completion(text=text, model=model, usage=data.get("usageMetadata", {}))


async def _openai_complete(
    model: str,
    system: str,
    messages: Sequence[Message],
    json_mode: bool,
    max_tokens: int,
) -> Completion:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": settings.llm_temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}]
        + [{"role": m.role, "content": m.content} for m in messages],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=payload,
        )
    if resp.status_code != 200:
        raise LLMError(f"openai {resp.status_code}: {resp.text[:400]}")

    data = resp.json()
    return Completion(
        text=data["choices"][0]["message"]["content"] or "",
        model=model,
        usage=data.get("usage", {}),
    )


async def _anthropic_complete(
    model: str,
    system: str,
    messages: Sequence[Message],
    json_mode: bool,
    max_tokens: int,
) -> Completion:
    # Deliberately no temperature/top_p/top_k: current Claude models reject
    # sampling parameters outright with a 400. Steering happens in the prompt.
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }
    if json_mode:
        payload["system"] = system + "\n\nRespond with a single valid JSON object and nothing else."

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=payload,
        )
    if resp.status_code != 200:
        raise LLMError(f"anthropic {resp.status_code}: {resp.text[:400]}")

    data = resp.json()
    # A safety decline arrives as a 200 with an empty/partial content array,
    # so stop_reason has to be checked before reading content.
    if data.get("stop_reason") == "refusal":
        raise LLMRefusal("anthropic declined the request")

    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    return Completion(text=text, model=model, usage=data.get("usage", {}))


def _echo_completion(system: str, messages: Sequence[Message], *, json_mode: bool) -> Completion:
    """Deterministic offline stand-in.

    Returns something structurally valid so ingestion and chat paths can be
    exercised without a key. It never invents figures — which keeps the
    grounding contract honest even in dev.
    """
    last = messages[-1].content if messages else ""
    if json_mode:
        return Completion(text="{}", model="echo")
    return Completion(
        text=(
            "[dev mode] No LLM provider is configured, so I can't generate an "
            "analysis. The retrieval layer still ran and the sources below are "
            f"the ones that matched your question.\n\nQuestion: {last[:200]}"
        ),
        model="echo",
    )


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
@_retry
async def embed(texts: Sequence[str]) -> list[list[float]]:
    """Embed a batch of texts into EMBED_DIM-dimensional unit vectors.

    Always returns one vector per input, in order. Callers rely on that for
    zipping results back onto chunks.
    """
    if not texts:
        return []

    provider = settings.embedding_provider
    model = settings.embedding_model or DEFAULT_EMBED_MODELS.get(provider, "")

    if provider == "hash":
        return [_hash_embed(t) for t in texts]
    if provider == "gemini":
        return await _gemini_embed(model, texts)
    if provider == "openai":
        return await _openai_embed(model, texts)
    raise LLMError(f"unknown embedding_provider {provider!r}")


async def _gemini_embed(model: str, texts: Sequence[str]) -> list[list[float]]:
    url = f"{GEMINI_BASE}/models/{model}:batchEmbedContents"
    payload = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": t}]},
                "outputDimensionality": EMBED_DIM,
            }
            for t in texts
        ]
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, params={"key": settings.google_api_key}, json=payload)
    if resp.status_code != 200:
        raise LLMError(f"gemini embed {resp.status_code}: {resp.text[:400]}")

    data = resp.json()
    return [_normalise(e["values"]) for e in data["embeddings"]]


async def _openai_embed(model: str, texts: Sequence[str]) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            OPENAI_EMBED_URL,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            # text-embedding-3 supports truncating to a requested width, which
            # is how OpenAI is made to fit the same 768-dim pgvector column.
            json={"model": model, "input": list(texts), "dimensions": EMBED_DIM},
        )
    if resp.status_code != 200:
        raise LLMError(f"openai embed {resp.status_code}: {resp.text[:400]}")

    data = resp.json()
    ordered = sorted(data["data"], key=lambda d: d["index"])
    return [_normalise(d["embedding"]) for d in ordered]


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _hash_embed(text: str) -> list[float]:
    """Deterministic bag-of-words hashing embedding.

    Not semantically clever, but it is stable, dependency-free, and similar
    texts land near each other — enough for tests to assert real retrieval
    behaviour (including the near-duplicate threshold) without a network call.
    """
    vec = [0.0] * EMBED_DIM
    tokens = _TOKEN_RE.findall(text.lower())
    for token in tokens:
        digest = hashlib.sha256(token.encode()).digest()
        idx = int.from_bytes(digest[:4], "big") % EMBED_DIM
        # Sign from a second slice so collisions don't all add constructively.
        sign = 1.0 if digest[4] & 1 else -1.0
        weight = struct.unpack(">f", b"\x3f\x80\x00\x00")[0]  # 1.0
        vec[idx] += sign * weight
    return _normalise(vec)


def _normalise(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(v) * float(v) for v in vec))
    if norm == 0:
        # Keep a valid unit-ish vector rather than emitting zeros, which would
        # make cosine distance undefined in pgvector.
        return [0.0] * (len(vec) - 1) + [1.0] if vec else [0.0] * EMBED_DIM
    return [float(v) / norm for v in vec]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_json(text: str) -> Any:
    """Best-effort JSON extraction from a model response.

    Even in JSON mode, models occasionally wrap output in a fenced block or add
    a sentence of preamble, so the raw parse is tried first and a braces-span
    fallback second.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    log.warning("could not parse JSON from model output: %s", text[:200])
    return None
