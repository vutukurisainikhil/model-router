"""Global test fixtures.

Autouse guard: any test that accidentally triggers a real HTTP call through
httpx will fail immediately rather than hitting a live provider.
"""
from __future__ import annotations

import pytest
import httpx
from unittest.mock import patch


@pytest.fixture(autouse=True)
def block_real_network():
    """Raise if any test makes a real outbound HTTP request via httpx."""
    _real_send = httpx.Client.send

    def _guarded(self, request, *args, **kwargs):
        host = request.url.host
        if host not in ("localhost", "127.0.0.1", "testserver", ""):
            raise AssertionError(
                f"Real HTTP call blocked in tests: {request.url}\n"
                "Patch the adapter's call() method instead."
            )
        return _real_send(self, request, *args, **kwargs)

    with patch.object(httpx.Client, "send", _guarded):
        yield
