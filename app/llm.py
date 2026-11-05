"""One chat-completions call whose answer must match a Pydantic model. Any
OpenAI-compatible endpoint with JSON-schema output works; the default is the Gemini
API's free tier. The schema goes to the model in strict mode, and Pydantic checks the answer."""

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .http import HttpError, send

# List prices in USD per million tokens (input, output), for the cost-per-lead
# figures. On a free plan the actual cost is zero; these show what a paid plan charges.
PRICES = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.8-flash": (0.75, 3.75),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}
# Constraints that not every provider accepts in strict mode; Pydantic enforces them.
_DROP = ("title", "default", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems")


class LlmError(Exception):
    def __init__(self, message: str, usage: dict | None = None, detail: str = ""):
        super().__init__(message)
        self.usage = usage or {"tokens_in": 0, "tokens_out": 0}
        self.detail = detail


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


def strict_schema(model: type[BaseModel]) -> dict:
    def clean(node: dict) -> None:
        for key in _DROP:
            node.pop(key, None)
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        for child in [*node.get("properties", {}).values(), *node.get("$defs", {}).values(), *node.get("anyOf", [])]:
            clean(child)
        if isinstance(node.get("items"), dict):
            clean(node["items"])

    schema = model.model_json_schema()
    clean(schema)
    return schema


def complete[T: BaseModel](client: httpx.Client, s: Settings, system: str, user: str, model: type[T]) -> tuple[T, dict]:
    body: dict = {
        "model": s.llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": model.__name__.lower(), "strict": True, "schema": strict_schema(model)},
        },
    }
    if s.llm_reasoning_effort:
        body["reasoning_effort"] = s.llm_reasoning_effort
    try:
        r = send(
            client,
            "POST",
            f"{s.llm_endpoint}/chat/completions",
            what="The model",
            json=body,
            headers={"Authorization": f"Bearer {s.llm_api_key}"},
            timeout=httpx.Timeout(60.0, connect=5.0),
        )
    except HttpError as e:
        raise LlmError(str(e), detail=e.detail) from e
    data = r.json()
    usage = {
        "tokens_in": (data.get("usage") or {}).get("prompt_tokens", 0),
        "tokens_out": (data.get("usage") or {}).get("completion_tokens", 0),
    }
    try:
        content = data["choices"][0]["message"]["content"]
        return model.model_validate_json(content), usage
    except (KeyError, IndexError, TypeError, ValidationError) as e:
        detail = e.errors()[0]["msg"] if isinstance(e, ValidationError) else "no message content"
        raise LlmError("The model's answer did not fit the expected format", usage, detail) from e
