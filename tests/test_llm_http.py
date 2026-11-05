import json

import httpx
import pytest

from app import llm
from app.config import Settings
from app.http import HttpError, backoff_s, retry_after, send
from app.scoring import Assessment

S = Settings(
    database_url="",
    api_key="",
    reviewer_key="",
    llm_endpoint="http://model.test/v1",
    llm_api_key="k",
    llm_model="gemini-3.5-flash-lite",
)
ANSWER = {
    "fit": 70,
    "intent": 60,
    "route": "sales",
    "confidence": 0.8,
    "fleet_size": None,
    "reasons": ["r"],
    "summary": "s",
}


def replies(*responses):
    calls = []

    def handle(request):
        calls.append(request)
        r = responses[min(len(calls), len(responses)) - 1]
        return r(request) if callable(r) else r

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
        },
    )


def test_complete_sends_a_strict_schema_and_validates_the_answer():
    client, calls = replies(completion(json.dumps(ANSWER)))
    a, usage = llm.complete(client, S, "system", "user", Assessment)
    assert a.route == "sales" and usage == {"tokens_in": 1000, "tokens_out": 200}
    body = json.loads(calls[0].content)
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning_effort"] == "low" and calls[0].headers["authorization"] == "Bearer k"


def test_rate_limit_is_retried_after_the_requested_wait(monkeypatch):
    waits = []
    monkeypatch.setattr("app.http.time.sleep", waits.append)
    client, calls = replies(httpx.Response(429, headers={"retry-after": "3"}), completion(json.dumps(ANSWER)))
    llm.complete(client, S, "s", "u", Assessment)
    assert len(calls) == 2 and waits == [3.0]


def test_answers_that_break_the_schema_raise_with_usage():
    client, _ = replies(completion(json.dumps({**ANSWER, "fit": 140})))
    with pytest.raises(llm.LlmError) as e:
        llm.complete(client, S, "s", "u", Assessment)
    assert "expected format" in str(e.value) and e.value.detail and e.value.usage["tokens_in"] == 1000


def test_client_errors_are_not_retried():
    client, calls = replies(httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(llm.LlmError):
        llm.complete(client, S, "s", "u", Assessment)
    assert len(calls) == 1


def test_send_retries_network_errors_then_gives_up():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    client, calls = replies(down)
    with pytest.raises(HttpError) as e:
        send(client, "GET", "http://x.test/", what="X", attempts=3)
    assert len(calls) == 3 and e.value.retryable


def test_a_wait_longer_than_a_minute_is_not_retried():
    client, calls = replies(httpx.Response(429, headers={"retry-after": "3600"}))
    with pytest.raises(HttpError) as e:
        send(client, "GET", "http://x.test/", what="X")
    assert len(calls) == 1 and not e.value.retryable


def test_errors_read_as_sentences_and_keep_what_the_service_said():
    busy = [{"error": {"code": 503, "message": "This model is currently experiencing high demand.", "status": "X"}}]
    client, _ = replies(httpx.Response(503, json=busy))
    with pytest.raises(llm.LlmError) as e:
        llm.complete(client, S, "s", "u", Assessment)
    assert str(e.value) == "The model was down or overloaded (HTTP 503)"
    assert e.value.detail == "This model is currently experiencing high demand."

    client, _ = replies(httpx.Response(404, text="<!DOCTYPE html><pre>Cannot POST /webhook/x</pre>"))
    with pytest.raises(HttpError) as e2:
        send(client, "POST", "http://x.test/", what="n8n")
    assert str(e2.value) == "n8n refused the request (HTTP 404)" and e2.value.detail == ""

    client, _ = replies(httpx.Response(400, json={"status": "error", "message": "Property values were not valid"}))
    with pytest.raises(HttpError) as e3:
        send(client, "POST", "http://x.test/", what="HubSpot")
    assert e3.value.detail == "Property values were not valid"


def test_backoff_and_retry_after():
    assert [backoff_s(n) for n in (1, 2, 3, 4, 5, 6)] == [0.5, 1, 2, 4, 8, 8]
    assert backoff_s(1, wait=5) == 5
    assert retry_after(httpx.Response(429, headers={"retry-after": "2.5"})) == 2.5
    assert retry_after(httpx.Response(429)) == 0


def test_cost_uses_list_prices():
    assert llm.cost_usd("gemini-3.5-flash-lite", 1_000_000, 1_000_000) == pytest.approx(2.80)
    assert llm.cost_usd("unknown", 1000, 1000) == 0
