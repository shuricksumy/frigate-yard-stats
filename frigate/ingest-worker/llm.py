"""LLM transport: chat-completion and embedding calls, across all three supported providers.

Split out of ai_worker.py, which had grown to own both the AI *stage* (claim a row, analyse it,
write the sighting back) and every detail of *talking to a model*. Two other callers already
needed the latter without the former -- visit_summary_worker reaches for the chat/embed helpers for
its text-only summary call, and api.py's POST /search embeds the user's query text -- and both were
importing ai_worker's underscore-private functions to get at them, which misrepresented a shared
dependency as a private implementation detail.

Everything here is provider-agnostic at the call site: pass a profiles.yaml type_config and the
right request shape is built from its `provider` key (llama_proxy, the default, or openai /
anthropic). Nothing in this module knows about queues, rows, or sightings.
"""
import logging

import requests

import config

logger = logging.getLogger(__name__)


_warned_llama_proxy_multi_image = False


def _llama_proxy_chat_request(type_config: dict, prompt: str, images: list[str], timeout: float) -> dict:
    # The original, still-default shape: llama_slot_proxy speaks an OpenAI-compatible
    # chat-completions API with no "model" field at all -- the slot is selected entirely by
    # chat_path (one URL path segment per model), not a body field. Multi-image reasoning quality
    # on a self-hosted llama.cpp+mmproj backend is unverified (see ai_worker.py's own multi-image
    # docstring note / CLAUDE.md), so only the first image is ever sent here regardless of how many
    # were gathered -- a warning is logged once (module-global, not per-call) rather than silently
    # dropping the rest with no visibility at all. images=[] (the visit-summary stage's text-only
    # call, see visit_summary_worker.py) sends no image block at all -- a plain text message, same
    # as the other two providers already handle via their own empty list-comprehension spread.
    global _warned_llama_proxy_multi_image
    if len(images) > 1 and not _warned_llama_proxy_multi_image:
        logger.warning(
            "%d images were gathered for this call but llama_proxy only ever sends the first one "
            "-- set provider to 'openai' or 'anthropic' for multi-image analysis",
            len(images),
        )
        _warned_llama_proxy_multi_image = True
    headers = {}
    if config.LLAMA_PROXY_TOKEN:
        headers["Authorization"] = f"Bearer {config.LLAMA_PROXY_TOKEN}"
    content = [{"type": "text", "text": prompt}]
    if images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{images[0]}"}})
    resp = requests.post(
        f"{config.LLAMA_PROXY_BASE_URL}{type_config['chat_path']}",
        json={
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
        },
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _openai_chat_request(type_config: dict, prompt: str, images: list[str], timeout: float) -> dict:
    # Same request/response shape llama_slot_proxy already speaks (it's deliberately
    # OpenAI-compatible) -- the two real differences are the base URL/auth and that OpenAI needs a
    # "model" field in the body instead of selecting the model via the URL path. OpenAI's vision
    # API natively supports several image_url blocks in one message's content array, so every
    # gathered image is sent, not just the first.
    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
    resp = requests.post(
        f"{config.OPENAI_BASE_URL}/v1/chat/completions",
        json={
            "model": type_config["model"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        *[
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}}
                            for img in images
                        ],
                    ],
                }
            ],
            "temperature": 0,
        },
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _anthropic_chat_request(type_config: dict, prompt: str, images: list[str], timeout: float) -> dict:
    # Claude's Messages API -- a genuinely different shape from the other two providers: auth is
    # x-api-key + anthropic-version headers (not Authorization: Bearer), images are a "source"
    # block instead of a data-URI image_url, and max_tokens is required (there's no server-side
    # default the way OpenAI/llama_slot_proxy have one). Claude's Messages API natively supports
    # several image blocks in one message's content array, so every gathered image is sent.
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": config.ANTHROPIC_VERSION,
    }
    resp = requests.post(
        f"{config.ANTHROPIC_BASE_URL}/v1/messages",
        json={
            "model": type_config["model"],
            "max_tokens": type_config.get("max_tokens", config.AI_STAGE_DEFAULT_MAX_TOKENS),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        *[
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/jpeg", "data": img},
                            }
                            for img in images
                        ],
                    ],
                }
            ],
        },
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def chat_request(type_config: dict, prompt: str, images: list[str], timeout: float) -> dict:
    # Dispatches on this type's own `provider` (profiles.yaml, per object type -- see
    # profiles.yaml.example) -- "llama_proxy" (the default, unchanged behavior) if the key is
    # omitted entirely, so an existing deployment's profiles.yaml needs no edit to keep working.
    # `images` is always a list -- the events stage passes a one-element list (unchanged
    # single-image behavior); the alert stage passes however many high-res crops it gathered.
    provider = type_config.get("provider", "llama_proxy")
    if provider == "openai":
        return _openai_chat_request(type_config, prompt, images, timeout)
    if provider == "anthropic":
        return _anthropic_chat_request(type_config, prompt, images, timeout)
    return _llama_proxy_chat_request(type_config, prompt, images, timeout)


def extract_response_text(response: dict, type_config: dict | None) -> str:
    # Claude's response shape (content[0].text) differs from the OpenAI-compatible shape
    # llama_slot_proxy and OpenAI itself both use (choices[0].message.content) -- type_config is
    # optional so existing callers/tests that only ever dealt with the OpenAI-compatible shape
    # keep working unchanged.
    if (type_config or {}).get("provider") == "anthropic":
        return response["content"][0]["text"]
    return response["choices"][0]["message"]["content"]


def _embed_request(text: str, timeout: float) -> dict:
    # config.EMBEDDING_PROVIDER is independent of whichever provider(s) profiles.yaml routes chat
    # calls to -- Claude has no embeddings endpoint at all, so a deployment using
    # `provider: anthropic` for chat still needs this set to "llama_proxy" (default) or "openai"
    # for semantic search/backfill to work.
    if config.EMBEDDING_PROVIDER == "openai":
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
        resp = requests.post(
            f"{config.OPENAI_BASE_URL}/v1/embeddings",
            json={"model": config.OPENAI_EMBED_MODEL, "input": text},
            headers=headers,
            timeout=timeout,
        )
    else:
        headers = {}
        if config.LLAMA_PROXY_TOKEN:
            headers["Authorization"] = f"Bearer {config.LLAMA_PROXY_TOKEN}"
        resp = requests.post(
            f"{config.LLAMA_PROXY_BASE_URL}{config.LLAMA_PROXY_EMBED_PATH}",
            json={"input": text},
            headers=headers,
            timeout=timeout,
        )
    resp.raise_for_status()
    return resp.json()


def embed_text(text: str | None) -> list[float] | None:
    # An embedding failure shouldn't lose an already-computed sighting -- same decision made for
    # n8n's Call Embedding Model nodes (continueErrorOutput, falls back to null). Never raises;
    # the sighting still gets inserted, just not semantically searchable.
    if not text:
        return None
    try:
        embedding = _embed_request(text, config.AI_STAGE_EMBED_TIMEOUT_SECONDS)["data"][0]["embedding"]
        if len(embedding) != config.EMBEDDING_DIMENSIONS:
            logger.warning(
                "Embedding call returned %d dims, expected %d (wrong model loaded at "
                "LLAMA_PROXY_EMBED_PATH?), storing sighting without one",
                len(embedding),
                config.EMBEDDING_DIMENSIONS,
            )
            return None
        return embedding
    except Exception:
        logger.warning("Embedding call failed, storing sighting without one", exc_info=True)
        return None


def embed_query_text(text: str) -> list[float]:
    """Embeds arbitrary free-text (the web UI Search tab's own query, not a stored sighting) via
    the same embedding backend _embed_text uses. Raises on any failure -- unlike _embed_text's
    "fine, store the sighting without one" fallback, a search request has nothing useful to do
    with a missing vector, so the caller (api.py's POST /search) turns this into a real error
    response instead of silently returning empty results."""
    if not text or not text.strip():
        raise ValueError("query text must not be empty")
    embedding = _embed_request(text, config.AI_STAGE_EMBED_TIMEOUT_SECONDS)["data"][0]["embedding"]
    if len(embedding) != config.EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"embedding backend returned {len(embedding)} dims, expected {config.EMBEDDING_DIMENSIONS} "
            "(wrong model loaded at LLAMA_PROXY_EMBED_PATH?)"
        )
    return embedding

