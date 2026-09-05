"""Bounded llama.cpp transport and response validation."""

import json
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class TransportSettings:
    url: str
    response_timeout: float
    connect_timeout: float = 10
    max_tokens: int = 1000


class ProtocolError(Exception):
    """The model returned a response outside the agent protocol."""


class TransportError(Exception):
    """The inference service could not complete a request."""


class TransportTimeout(TransportError):
    """The inference service did not answer before the deadline."""


def clip(value, limit=800):
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "…[truncated]"


def object_json(text):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise ProtocolError("Invalid JSON; resend a complete, shorter object") from None
    if not isinstance(data, dict):
        raise ProtocolError("Expected a JSON object")
    return data


def request(messages, settings, tools=None, response_timeout=None):
    body = {
        "messages": messages,
        "temperature": 0,
        "max_tokens": settings.max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if tools:
        body.update(tools=tools, tool_choice="required", parallel_tool_calls=False)
    else:
        body["response_format"] = {"type": "json_object"}
    timeout = settings.response_timeout if response_timeout is None else response_timeout
    try:
        response = requests.post(
            settings.url,
            json=body,
            timeout=(settings.connect_timeout, max(0.1, timeout)),
        )
        response.raise_for_status()
    except requests.Timeout:
        raise TransportTimeout(f"LLM response timed out after {timeout:g} seconds") from None
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f", HTTP {status}" if status is not None else ""
        raise TransportError(f"LLM request failed ({type(exc).__name__}{detail})") from None
    try:
        choice = response.json()["choices"][0]
        if choice["finish_reason"] not in {"stop", "tool_calls"}:
            raise ProtocolError("Incomplete output; use smaller chunks")
        message = choice["message"]
        if not isinstance(message, dict):
            raise TypeError
        return message
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProtocolError("Malformed API response") from None


def string(data, key, limit=800, allow_empty=False):
    value = data.get(key)
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > limit:
        raise ProtocolError(f"Invalid {key}: expected string of at most {limit} characters")
    return value
