"""Abstract base class every provider adapter must implement."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseAdapter(ABC):
    name: str  # e.g. "do", "mock"

    @abstractmethod
    def translate_request(self, unified: dict) -> dict:
        """Map unified payload → provider-native payload."""

    @abstractmethod
    def call(self, native_payload: dict, *, stream: bool) -> Any:
        """Execute the HTTP call; return raw provider response object."""

    @abstractmethod
    def translate_response(self, native_response: Any) -> dict:
        """Map provider-native response → unified response dict."""

    @abstractmethod
    def translate_stream_chunk(self, native_chunk: Any) -> dict | None:
        """Map one SSE chunk → unified chunk dict, or None to skip."""
