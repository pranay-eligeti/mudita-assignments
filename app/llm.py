"""LLM access. One narrow seam: structured output in, validated pydantic object out.

* Real backend uses Anthropic tool-use with a forced tool so the model must return JSON matching the schema.
* Every call is validated with pydantic; one bounded repair attempt is made on schema violations.
* Every call (success or failure) is written to `llm_calls` with token usage and an estimated cost.
* Tests inject a scripted backend with `set_backend`, so no test needs network or an API key.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .config import get_settings
from .db import now_iso, run

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Real (non-simulated) model failure: network, auth, refusal, unparseable output."""


class SimulatedModelFailure(LLMError):
    """Raised by the A2 fault injector. Always labelled as simulated in the UI."""


@dataclass
class RawResult:
    data: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""  # model that actually answered (differs from MODEL when a fallback was used)


class LLMBackend(Protocol):
    def complete(self, *, agent: str, system: str, user: str, schema: dict[str, Any], model: str) -> RawResult: ...


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic emits $defs/$ref; flatten so the tool input_schema is self-contained."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node: Any, in_properties: bool = False) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and not in_properties:
                name = node["$ref"].split("/")[-1]
                return walk(copy.deepcopy(defs[name]))
            # Inside a `properties` map the keys are FIELD NAMES (a field may legitimately be called "title"),
            # so only strip pydantic's cosmetic "title" annotation on schema nodes, never on property maps.
            return {k: walk(v, in_properties=(k == "properties" and not in_properties)) for k, v in node.items()
                    if in_properties or k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


class AnthropicBackend:
    def __init__(self, client: Any = None) -> None:
        import anthropic  # local import so tests don't need the key

        s = get_settings()
        if client is not None:          # tests inject a client wired to a mock transport
            self._client = client
            return
        if not s.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=s.llm_timeout_s, max_retries=2)

    def complete(self, *, agent: str, system: str, user: str, schema: dict[str, Any], model: str) -> RawResult:
        import anthropic

        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=6000,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[{
                    "name": "emit",
                    "description": f"Return the {agent} result as structured JSON.",
                    "input_schema": schema,
                }],
                tool_choice={"type": "tool", "name": "emit"},
            )
        except anthropic.APIError as e:  # includes timeouts, 4xx, 5xx after SDK retries
            raise LLMError(f"{type(e).__name__}: {e}") from e
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "emit":
                return RawResult(dict(block.input), resp.usage.input_tokens, resp.usage.output_tokens)
        raise LLMError(f"model returned no tool_use block (stop_reason={resp.stop_reason})")


class _Retryable(Exception):
    """Internal: one model exhausted its retries on a transient error."""


class GeminiBackend:
    """Gemini via REST (no extra SDK). JSON-mode with a response schema; 429/5xx/timeouts retried with backoff.

    Free-tier note: requests are rate limited, so 429 is an expected condition, not an error. Retry-After is honoured.
    """

    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, client: Any = None, sleep: Any = None) -> None:
        import time

        import httpx

        s = get_settings()
        self._sleep = sleep or time.sleep
        if client is not None:
            self._client = client
            self._key = s.gemini_api_key or "test"
            return
        if not s.gemini_api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._key = s.gemini_api_key
        self._client = httpx.Client(timeout=s.llm_timeout_s)

    @staticmethod
    def _delay(resp: Any, attempt: int) -> float:
        ra = resp.headers.get("retry-after") if resp is not None else None
        try:
            if ra:
                return min(float(ra), 60.0)
        except ValueError:
            pass
        return min(2.0 ** attempt + 0.5, 30.0)  # 1.5, 2.5, 4.5, 8.5 ...

    def complete(self, *, agent: str, system: str, user: str, schema: dict[str, Any], model: str) -> RawResult:
        s = get_settings()
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "temperature": 0.2,
                "maxOutputTokens": 8000,
            },
        }
        chain = [model] + [m.strip() for m in s.model_fallbacks.split(",") if m.strip() and m.strip() != model]
        errors: list[str] = []
        for mdl in chain:
            try:
                return self._complete_one(mdl, body)
            except _Retryable as e:  # this model is overloaded/rate-limited: try the next one
                errors.append(f"{mdl}: {e}")
        raise LLMError("Gemini unavailable on " + "; ".join(errors))

    def _complete_one(self, mdl: str, body: dict[str, Any]) -> RawResult:
        s = get_settings()
        url = f"{self.BASE}/models/{mdl}:generateContent"
        last = "no attempt made"
        for attempt in range(max(1, s.llm_max_attempts)):
            resp = None
            try:
                resp = self._client.post(url, json=body, headers={"x-goog-api-key": self._key})
            except Exception as e:  # timeouts / connection errors: retryable
                last = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    r = self._parse(resp.json())
                    r.model = mdl
                    return r
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise LLMError(f"{mdl}: {last}")  # 400/401/403/404 will not improve by retrying
            if attempt < s.llm_max_attempts - 1:
                self._sleep(self._delay(resp, attempt))
        raise _Retryable(f"{s.llm_max_attempts} attempts failed, last {last}")

    @staticmethod
    def _parse(j: dict[str, Any]) -> RawResult:
        usage = j.get("usageMetadata", {})
        cands = j.get("candidates") or []
        if not cands:
            raise LLMError(f"Gemini returned no candidates (promptFeedback={j.get('promptFeedback')})")
        c = cands[0]
        text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []))
        if not text.strip():
            raise LLMError(f"Gemini returned empty output (finishReason={c.get('finishReason')})")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Gemini output is not valid JSON (finishReason={c.get('finishReason')}): {e}") from e
        if not isinstance(data, dict):
            raise LLMError("Gemini output JSON is not an object")
        return RawResult(data, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))


_backend: LLMBackend | None = None


def set_backend(backend: LLMBackend | None) -> None:
    global _backend
    _backend = backend


def get_backend() -> LLMBackend:
    global _backend
    if _backend is None:
        _backend = AnthropicBackend() if get_settings().llm_provider == "anthropic" else GeminiBackend()
    return _backend


def _cost(inp: int, out: int) -> float:
    s = get_settings()
    return round(inp / 1e6 * s.price_in_per_mtok + out / 1e6 * s.price_out_per_mtok, 6)


def _log(scope: str, agent: str, raw: RawResult | None, ok: bool, error: str | None) -> None:
    s = get_settings()
    inp = raw.input_tokens if raw else 0
    out = raw.output_tokens if raw else 0
    run(
        "INSERT INTO llm_calls(scope,agent,model,input_tokens,output_tokens,cost_usd,ok,error,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (scope, agent, (raw.model if raw and raw.model else s.model), inp, out, _cost(inp, out), 1 if ok else 0, error, now_iso()),
    )


def call_structured(*, scope: str, agent: str, system: str, user: str, model_cls: type[T], max_repairs: int = 1) -> T:
    """Call the model and return a validated `model_cls`. Raises LLMError on failure."""
    s = get_settings()
    schema = inline_refs(model_cls.model_json_schema())
    backend = get_backend()
    prompt = user
    last_err = ""
    for attempt in range(max_repairs + 1):
        try:
            raw = backend.complete(agent=agent, system=system, user=prompt, schema=schema, model=s.model)
        except SimulatedModelFailure as e:
            _log(scope, agent, None, False, f"SIMULATED: {e}")
            raise
        except LLMError as e:
            _log(scope, agent, None, False, str(e))
            raise
        try:
            parsed = model_cls.model_validate(raw.data)
            _log(scope, agent, raw, True, None)
            return parsed
        except ValidationError as e:
            last_err = e.json(include_url=False)[:1500]
            _log(scope, agent, raw, False, f"schema violation (attempt {attempt + 1}): {last_err[:300]}")
            prompt = (
                user
                + "\n\n---\nYour previous output failed schema validation with these errors:\n"
                + last_err
                + "\nReturn a corrected object that satisfies the schema."
            )
    raise LLMError(f"{agent}: output failed schema validation after {max_repairs + 1} attempts: {last_err[:300]}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str)
