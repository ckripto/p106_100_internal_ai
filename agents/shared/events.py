"""Stable event envelope for the web-visible agent journal."""

import json
import time


def emit_message(
    callback,
    *,
    attempt,
    sender,
    recipient,
    kind,
    content,
    step=None,
    response_seconds=None,
):
    if callback is None:
        return
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    callback({
        "attempt": attempt,
        "step": step,
        "sender": sender,
        "recipient": recipient,
        "kind": kind,
        "content": content,
        "created": time.time(),
        "response_seconds": response_seconds,
    })
