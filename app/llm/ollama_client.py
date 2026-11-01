"""Ollama HTTP client — chat + embed. All LLM traffic goes through here."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class OllamaClientProtocol(Protocol):
    """Protocol so tests can inject fakes."""

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        format_schema: dict[str, Any] | None = None,
        temperature: float = 0.4,
    ) -> dict[str, Any]: ...

    def embed(self, model: str, inputs: list[str]) -> list[list[float]]: ...

    def health(self) -> dict[str, Any]: ...


@dataclass
class OllamaClient:
    """Real Ollama HTTP client."""

    base_url: str = "http://127.0.0.1:11434"
    timeout: float = 120.0
    last_latency_ms: float = 0.0
    last_eval_count: int = 0

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        format_schema: dict[str, Any] | None = None,
        temperature: float = 0.4,
    ) -> dict[str, Any]:
        """POST /api/chat with JSON schema format and think:false."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_ctx": 8192},
            "keep_alive": "10m",
        }
        if format_schema is not None:
            payload["format"] = format_schema
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                t0 = time.perf_counter()
                with self._client() as client:
                    resp = client.post("/api/chat", json=payload)
                    if resp.status_code >= 400 and "think" in (resp.text or "").lower():
                        payload.pop("think", None)
                        resp = client.post("/api/chat", json=payload)
                    if resp.status_code == 404:
                        raise RuntimeError(
                            f"Model '{model}' not found in Ollama. "
                            f"Run: ollama pull {model}"
                        )
                    resp.raise_for_status()
                    data = resp.json()
                self.last_latency_ms = (time.perf_counter() - t0) * 1000
                self.last_eval_count = int(data.get("eval_count") or 0)
                logger.info(
                    "ollama chat model=%s latency_ms=%.0f eval_count=%s",
                    model,
                    self.last_latency_ms,
                    self.last_eval_count,
                )
                return data
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                # Don't retry hard 404 model-missing wrapped above; other 404s still retry once
                if "404" in str(exc) and attempt == 0:
                    # Likely missing model — stop retrying and give pull hint
                    raise RuntimeError(
                        f"Ollama returned 404 for model '{model}'. "
                        f"Install it with: ollama pull {model} "
                        f"(then check Settings → drafting model matches `ollama list`)."
                    ) from exc
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Ollama chat failed: {last_err}")

    def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        """POST /api/embed with batched inputs."""
        if not inputs:
            return []
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with self._client() as client:
                    resp = client.post("/api/embed", json={"model": model, "input": inputs})
                    resp.raise_for_status()
                    data = resp.json()
                embeddings = data.get("embeddings") or data.get("embedding")
                if embeddings and isinstance(embeddings[0], (int, float)):
                    return [list(embeddings)]
                return [list(e) for e in embeddings]
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Ollama embed failed: {last_err}")

    def health(self, required_models: list[str] | None = None) -> dict[str, Any]:
        """GET /api/tags; report missing models with pull commands."""
        required_models = required_models or []
        try:
            with self._client() as client:
                resp = client.get("/api/tags")
                resp.raise_for_status()
                data = resp.json()
            full = {m.get("name", "") for m in data.get("models", [])}
            missing = [m for m in required_models if m not in full and m.split(":")[0] not in {x.split(":")[0] for x in full}]
            return {
                "ok": True,
                "models": sorted(full),
                "missing": missing,
                "pull_commands": [f"ollama pull {m}" for m in missing],
                "latency_ms": self.last_latency_ms,
                "eval_count": self.last_eval_count,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": str(exc),
                "models": [],
                "missing": required_models,
                "pull_commands": [f"ollama pull {m}" for m in required_models],
            }


@dataclass
class FakeOllamaClient:
    """Deterministic fake for tests — never hits the network."""

    chat_responses: list[dict[str, Any]] = field(default_factory=list)
    embed_vectors: dict[str, list[float]] = field(default_factory=dict)
    default_embed_dim: int = 8
    last_latency_ms: float = 1.0
    last_eval_count: int = 10
    _chat_calls: list[dict[str, Any]] = field(default_factory=list)

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        format_schema: dict[str, Any] | None = None,
        temperature: float = 0.4,
    ) -> dict[str, Any]:
        self._chat_calls.append({"model": model, "messages": messages, "format": format_schema})
        if self.chat_responses:
            return self.chat_responses.pop(0)
        import json

        content = json.dumps(
            {
                "subject": "PhD enquiry",
                "body": "Dear Professor, I am interested in your work on topic. My CV is attached.",
                "referenced_items": [],
                "cv_key": "general",
                "reason": "default",
                "confidence": 0.7,
            }
        )
        return {"message": {"content": content}, "eval_count": 10}

    def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in inputs:
            if text in self.embed_vectors:
                out.append(self.embed_vectors[text])
            else:
                # simple hash-based pseudo vector
                vec = [((hash(text) >> i) & 0xFF) / 255.0 for i in range(self.default_embed_dim)]
                out.append(vec)
        return out

    def health(self, required_models: list[str] | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "models": required_models or ["fake"],
            "missing": [],
            "pull_commands": [],
            "latency_ms": self.last_latency_ms,
            "eval_count": self.last_eval_count,
        }


_client: OllamaClientProtocol | None = None


def get_ollama_client(base_url: str | None = None) -> OllamaClientProtocol:
    """Return the global client (real unless overridden in tests)."""
    global _client
    if _client is not None:
        return _client
    return OllamaClient(base_url=base_url or "http://127.0.0.1:11434")


def set_ollama_client(client: OllamaClientProtocol | None) -> None:
    """Override global client (tests)."""
    global _client
    _client = client
