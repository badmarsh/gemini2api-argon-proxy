"""Gemini Web code-interpreter UI annotations are removed in returned and streamed text."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.core import gemini_client as gc
from app.core.gemini_client import GeminiWebClient


_RAW_ARTIFACTS = "Before python?code_reference&code_event_index=17[file-tag: output.py] After"
_CLEAN_TEXT = "Before  After"


def _wrb_frame(text):
    candidate = [None, [text]]
    payload = [None, "conv-1", None, None, [candidate]]
    return json.dumps([["wrb.fr", None, json.dumps(payload)]], separators=(",", ":"))


def test_non_stream_parser_strips_code_reference_and_file_tag():
    client = GeminiWebClient.__new__(GeminiWebClient)
    raw = ")]}'\n" + _wrb_frame(_RAW_ARTIFACTS)

    result = client._parse_output(raw)

    assert result["text"] == _CLEAN_TEXT
    assert "python?code_reference" not in result["text"]
    assert "[file-tag:" not in result["text"]


@pytest.mark.asyncio
async def test_stream_holds_split_artifact_markers_and_emits_only_clean_text(monkeypatch):
    client = GeminiWebClient.__new__(GeminiWebClient)
    client._healthy = True
    client._family_model = {}
    client._session_uuid = "test-session"
    client._session_token = "test-token"
    client._cookie_jar = type("CookieJar", (), {"update_from_response": lambda self, response: None})()

    chunks = [
        (_wrb_frame("Before python?code_ref") + "\n").encode(),
        (_wrb_frame("Before python?code_reference&code_event_index=17[file-tag: output.py") + "\n").encode(),
        (_wrb_frame(_RAW_ARTIFACTS) + "\n").encode(),
    ]

    class FakeResponse:
        status_code = 200
        headers = {}

        async def aiter_content(self):
            for chunk in chunks:
                yield chunk

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

    class FakeSession:
        def stream(self, *args, **kwargs):
            return FakeStreamContext()

    monkeypatch.setattr(gc, "apply_jitter", AsyncMock())
    monkeypatch.setattr(client, "_ensure_session_current", AsyncMock())
    monkeypatch.setattr(client, "_get_cookies", lambda: {})
    monkeypatch.setattr(client, "_stream_generate_params", lambda: {})
    monkeypatch.setattr(client, "_stream_generate_headers", lambda: {})
    monkeypatch.setattr(client, "_encode_payload_thinking", lambda *args: ("encoded", "request-id"))
    monkeypatch.setattr(client, "_lease_http", lambda: FakeSession())
    monkeypatch.setattr(client, "_release_http", AsyncMock())

    events = [event async for event in client.generate_stream("run the code", "gemini-flash")]
    streamed_text = "".join(event.get("text", "") for event in events if event.get("type") == "delta")
    final = next(event for event in events if event.get("type") == "final")

    assert streamed_text == _CLEAN_TEXT
    assert final["text"] == _CLEAN_TEXT
    assert "python?code_reference" not in streamed_text + final["text"]
    assert "[file-tag:" not in streamed_text + final["text"]
