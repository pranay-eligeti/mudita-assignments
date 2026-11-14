"""Exercise the real Anthropic SDK code path (request shape + response parsing) against a mock HTTP transport."""
from __future__ import annotations

import json

import anthropic
import pytest

try:  # the installed SDK may be built on httpx2; use whichever HTTP library it expects
    import httpx2 as httpx
except ImportError:
    import httpx

from app import llm
from app.a1.extract import Extraction
from app.a2.schemas import IntakeOutput, PlanOutput, ReviewOutput
from app.llm import AnthropicBackend, LLMError, call_structured, inline_refs


def sdk(handler):
    return anthropic.Anthropic(api_key="x", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def tool_use_response(data, in_tok=11, out_tok=7):
    return httpx.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                                     "content": [{"type": "tool_use", "id": "tu_1", "name": "emit", "input": data}],
                                     "stop_reason": "tool_use", "usage": {"input_tokens": in_tok, "output_tokens": out_tok}})


def test_schemas_are_self_contained_for_tool_use():
    for m in (IntakeOutput, PlanOutput, ReviewOutput, Extraction):
        s = inline_refs(m.model_json_schema())
        assert "$defs" not in s and "$ref" not in json.dumps(s) and s["type"] == "object"


def test_request_shape_and_cost_logging(env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("PRICE_IN_PER_MTOK", "3")
    monkeypatch.setenv("PRICE_OUT_PER_MTOK", "15")
    from app import config
    config.reset_settings()
    seen = {}

    def handler(req: httpx.Request):
        seen.update(json.loads(req.content))
        return tool_use_response({"verdict": "approved", "issues": [], "checked_rules": ["R1"]})

    llm.set_backend(AnthropicBackend(client=sdk(handler)))
    out = call_structured(scope="a2:run:x", agent="review", system="SYS", user="USR", model_cls=ReviewOutput)
    assert out.verdict == "approved"
    assert seen["model"] == "claude-sonnet-5-5" and seen["system"] == "SYS"
    assert seen["tool_choice"] == {"type": "tool", "name": "emit"} and seen["tools"][0]["name"] == "emit"
    from app.db import q1
    row = q1("SELECT * FROM llm_calls WHERE scope='a2:run:x'")
    assert (row["input_tokens"], row["output_tokens"], row["ok"]) == (11, 7, 1) and row["cost_usd"] > 0


def test_api_errors_become_llm_errors_and_are_logged(env):
    llm.set_backend(AnthropicBackend(client=sdk(lambda r: httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}))))
    with pytest.raises(LLMError):
        call_structured(scope="a1:lead:1", agent="a1_extract", system="s", user="u", model_cls=ReviewOutput)
    from app.db import q1
    assert q1("SELECT ok, error FROM llm_calls WHERE scope='a1:lead:1'")["ok"] == 0


def test_missing_tool_block_is_an_error(env):
    llm.set_backend(AnthropicBackend(client=sdk(lambda r: httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "m",
                                                                                      "content": [{"type": "text", "text": "I refuse"}], "stop_reason": "end_turn",
                                                                                      "usage": {"input_tokens": 1, "output_tokens": 1}}))))
    with pytest.raises(LLMError):
        call_structured(scope="s", agent="review", system="s", user="u", model_cls=ReviewOutput)


# ---------------- Gemini backend (request shape, parsing, backoff) ----------------
from app.llm import GeminiBackend


def gem_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def gem_ok(data, pin=13, pout=5):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(data)}]}, "finishReason": "STOP"}],
                                     "usageMetadata": {"promptTokenCount": pin, "candidatesTokenCount": pout}})


REVIEW_OK = {"verdict": "approved", "issues": [], "checked_rules": ["R1"]}


def test_gemini_request_shape_and_logging(env):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["key"] = req.headers.get("x-goog-api-key")
        seen["body"] = json.loads(req.content)
        return gem_ok(REVIEW_OK)

    llm.set_backend(GeminiBackend(client=gem_client(handler), sleep=lambda s: None))
    out = call_structured(scope="a2:run:g", agent="review", system="SYS", user="USR", model_cls=ReviewOutput)
    assert out.verdict == "approved"
    gc = seen["body"]["generationConfig"]
    assert ":generateContent" in seen["url"] and seen["key"]
    assert gc["responseMimeType"] == "application/json" and gc["responseJsonSchema"]["type"] == "object"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "SYS"
    from app.db import q1
    row = q1("SELECT * FROM llm_calls WHERE scope='a2:run:g'")
    assert (row["input_tokens"], row["output_tokens"], row["ok"]) == (13, 5, 1)


def test_gemini_429_backoff_honours_retry_after_then_succeeds(env):
    calls, sleeps = [], []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "quota"}})
        return gem_ok(REVIEW_OK)

    llm.set_backend(GeminiBackend(client=gem_client(handler), sleep=sleeps.append))
    assert call_structured(scope="s", agent="review", system="s", user="u", model_cls=ReviewOutput).verdict == "approved"
    assert len(calls) == 3 and sleeps == [7.0, 7.0]


def test_gemini_gives_up_after_bounded_attempts(env):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    llm.set_backend(GeminiBackend(client=gem_client(handler), sleep=lambda s: None))
    with pytest.raises(LLMError, match="unavailable on"):
        call_structured(scope="s2", agent="review", system="s", user="u", model_cls=ReviewOutput)
    assert len(calls) == llm.get_settings().llm_max_attempts  # single model: one full retry budget


def test_gemini_auth_error_is_not_retried(env):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(403, json={"error": {"message": "API key not valid"}})

    llm.set_backend(GeminiBackend(client=gem_client(handler), sleep=lambda s: None))
    with pytest.raises(LLMError, match="403"):
        call_structured(scope="s3", agent="review", system="s", user="u", model_cls=ReviewOutput)
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [
    {"candidates": []},
    {"candidates": [{"content": {"parts": [{"text": "not json {"}]}, "finishReason": "MAX_TOKENS"}]},
    {"candidates": [{"content": {"parts": [{"text": "[1,2]"}]}}]},
])
def test_gemini_bad_outputs_become_llm_errors(env, payload):
    llm.set_backend(GeminiBackend(client=gem_client(lambda r: httpx.Response(200, json=payload)), sleep=lambda s: None))
    with pytest.raises(LLMError):
        call_structured(scope="s4", agent="review", system="s", user="u", model_cls=ReviewOutput)


def test_gemini_schema_violation_gets_one_repair(env):
    n = []

    def handler(req):
        n.append(json.loads(req.content)["contents"][0]["parts"][0]["text"])
        return gem_ok({"verdict": "bogus"}) if len(n) == 1 else gem_ok(REVIEW_OK)

    llm.set_backend(GeminiBackend(client=gem_client(handler), sleep=lambda s: None))
    assert call_structured(scope="s5", agent="review", system="s", user="u", model_cls=ReviewOutput).verdict == "approved"
    assert len(n) == 2 and "failed schema validation" in n[1]


def test_gemini_falls_back_to_next_model_when_primary_overloaded(env, monkeypatch):
    monkeypatch.setenv("MODEL", "primary-m")
    monkeypatch.setenv("MODEL_FALLBACKS", "backup-m, other-m")
    from app import config
    config.reset_settings()
    urls = []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(503, json={}) if "primary-m" in str(req.url) else gem_ok(REVIEW_OK)

    llm.set_backend(GeminiBackend(client=gem_client(handler), sleep=lambda s: None))
    assert call_structured(scope="fb", agent="review", system="s", user="u", model_cls=ReviewOutput).verdict == "approved"
    assert any("backup-m" in u for u in urls) and not any("other-m" in u for u in urls)
    assert sum("primary-m" in u for u in urls) == llm.get_settings().llm_max_attempts


def test_gemini_fallback_not_used_for_auth_errors(env, monkeypatch):
    monkeypatch.setenv("MODEL", "primary-m")
    monkeypatch.setenv("MODEL_FALLBACKS", "backup-m")
    from app import config
    config.reset_settings()
    urls = []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(404, json={"error": {"message": "no such model"}})

    llm.set_backend(GeminiBackend(client=gem_client(handler), sleep=lambda s: None))
    with pytest.raises(LLMError, match="primary-m: HTTP 404"):
        call_structured(scope="fb2", agent="review", system="s", user="u", model_cls=ReviewOutput)
    assert len(urls) == 1


def _check_required_in_properties(node, path="$"):
    """Every name in `required` must exist in `properties` (providers like Gemini reject the schema otherwise)."""
    if isinstance(node, dict):
        if "required" in node and "properties" in node:
            missing = [r for r in node["required"] if r not in node["properties"]]
            assert not missing, f"{path}: required {missing} not in properties {list(node['properties'])}"
        for k, v in node.items():
            _check_required_in_properties(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _check_required_in_properties(v, f"{path}[{i}]")


def test_flattened_schemas_are_self_consistent_and_keep_fields_named_title():
    for m in (IntakeOutput, PlanOutput, ReviewOutput, Extraction):
        s = inline_refs(m.model_json_schema())
        _check_required_in_properties(s)
    task = inline_refs(PlanOutput.model_json_schema())["properties"]["tasks"]["items"]
    assert "title" in task["properties"] and task["properties"]["title"]["type"] == "string"
    assert "title" not in task  # the cosmetic annotation on the node itself is still stripped
