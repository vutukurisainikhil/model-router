"""SSE framing helpers and constants."""
from __future__ import annotations

import json

SSE_DONE = "data: [DONE]\n\n"


def sse_event(data: dict) -> str:
    """Serialise a dict as a single SSE data frame (ends with double newline)."""
    return f"data: {json.dumps(data)}\n\n"


def parse_sse_line(line: str) -> tuple[str, bool]:
    """
    Parse one SSE text line.

    Returns (json_string, is_done).
    - is_done=True means this was the [DONE] sentinel.
    - json_string is the raw JSON text (not yet parsed).
    - Returns ("", False) if the line is not a data event.
    """
    if not isinstance(line, str):
        return "", False
    line = line.strip()
    if not line.startswith("data: "):
        return "", False
    payload = line[6:]
    if payload.strip() == "[DONE]":
        return "", True
    return payload, False
