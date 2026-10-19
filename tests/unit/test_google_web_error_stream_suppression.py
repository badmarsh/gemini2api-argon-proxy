"""Google Workspace/Drive soft-error prefixes must never be emitted as SSE assistant text."""

import json


_AUTH = {"Authorization": "Bearer sk-test-key"}
_PHRASE = "First, I'll need your permission to turn on the Google Drive app"
_RECOVERED = "The request recovered successfully."


def _stream_in_pieces():
    async def fake_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        # Deliberately split the sensitive prefix across several upstream deltas.
        for start in range(0, len(_PHRASE), 9):
            yield {"type": "delta", "text": _PHRASE[start:start + 9]}
        yield {"type": "final", "text": _PHRASE, "conversation_id": "", "images": []}
    return fake_generate_stream


async def _successful_regeneration(*args, **kwargs):
    return {"text": _RECOVERED, "conversation_id": "", "images": [], "thoughts": ""}


async def _failed_regeneration(*args, **kwargs):
    raise RuntimeError(_PHRASE)


def test_openai_true_stream_suppresses_google_drive_phrase_and_regenerates(gem_client, monkeypatch):
    import app.routers.openai as oai

    monkeypatch.setattr(oai.gemini_client, "generate_stream", _stream_in_pieces())
    monkeypatch.setattr(oai.argon_fallback, "generate", _successful_regeneration)

    with gem_client.stream("POST", "/v1/chat/completions", json={
        "model": "gemini-flash",
        "messages": [{"role": "user", "content": "open my files"}],
        "stream": True,
    }, headers=_AUTH) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert _RECOVERED in body
    assert _PHRASE not in body
    assert "[DONE]" in body


def test_openai_true_stream_returns_retryable_error_if_regeneration_fails(gem_client, monkeypatch):
    import app.routers.openai as oai

    monkeypatch.setattr(oai.gemini_client, "generate_stream", _stream_in_pieces())
    monkeypatch.setattr(oai.argon_fallback, "generate", _failed_regeneration)

    with gem_client.stream("POST", "/v1/chat/completions", json={
        "model": "gemini-flash",
        "messages": [{"role": "user", "content": "open my files"}],
        "stream": True,
    }, headers=_AUTH) as response:
        body = "".join(response.iter_text())

    events = [line[6:] for line in body.splitlines() if line.startswith("data: {")]
    errors = [json.loads(event)["error"] for event in events if "\"error\"" in event]
    assert errors
    assert errors[-1]["code"] == 502
    assert _PHRASE not in body


def test_responses_true_stream_suppresses_google_drive_phrase_and_regenerates(gem_client, monkeypatch):
    import app.routers.responses as responses

    monkeypatch.setattr(responses.gemini_client, "generate_stream", _stream_in_pieces())
    monkeypatch.setattr(responses.argon_fallback, "generate", _successful_regeneration)

    with gem_client.stream("POST", "/v1/responses", json={
        "model": "gemini-flash",
        "input": "open my files",
        "stream": True,
    }, headers=_AUTH) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert _RECOVERED in body
    assert _PHRASE not in body
    assert "response.completed" in body
    assert "response.failed" not in body


def test_responses_true_stream_fails_without_leaking_google_drive_phrase(gem_client, monkeypatch):
    import app.routers.responses as responses

    monkeypatch.setattr(responses.gemini_client, "generate_stream", _stream_in_pieces())
    monkeypatch.setattr(responses.argon_fallback, "generate", _failed_regeneration)

    with gem_client.stream("POST", "/v1/responses", json={
        "model": "gemini-flash",
        "input": "open my files",
        "stream": True,
    }, headers=_AUTH) as response:
        body = "".join(response.iter_text())

    assert "response.failed" in body
    assert "response.completed" not in body
    assert "Google web soft-error detected; regeneration/fallback failed" in body
    assert _PHRASE not in body


def test_responses_buffered_google_error_exception_regenerates_without_leaking(gem_client, monkeypatch):
    import app.routers.responses as responses

    async def raise_google_error(*args, **kwargs):
        raise responses.GoogleWebError(f"Google web error: {_PHRASE}")

    monkeypatch.setattr(responses.gemini_client, "generate", raise_google_error)
    monkeypatch.setattr(responses.argon_fallback, "generate", _successful_regeneration)

    with gem_client.stream("POST", "/v1/responses", json={
        "model": "gemini-flash",
        "input": "open my files",
        "tools": [{"type": "function", "name": "run_shell", "description": "run a command",
                   "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}],
        "stream": True,
    }, headers=_AUTH) as response:
        body = "".join(response.iter_text())

    assert "response.completed" in body
    assert _RECOVERED in body
    assert _PHRASE not in body


def test_openai_true_stream_suppresses_local_filesystem_refusal_phrase_and_regenerates(gem_client, monkeypatch):
    """Verify local filesystem refusal phrases are suppressed by InitialPrefixBuffer and regenerated."""
    import app.routers.openai as oai

    refusal_phrase = "I do not have access to a local command shell, filesystem, or sandbox tools."

    async def fake_refusal_stream(*args, **kwargs):
        for start in range(0, len(refusal_phrase), 10):
            yield {"type": "delta", "text": refusal_phrase[start:start + 10]}
        yield {"type": "final", "text": refusal_phrase, "conversation_id": "", "images": []}

    monkeypatch.setattr(oai.gemini_client, "generate_stream", fake_refusal_stream)
    monkeypatch.setattr(oai.argon_fallback, "generate", _successful_regeneration)

    with gem_client.stream("POST", "/v1/chat/completions", json={
        "model": "gemini-flash",
        "messages": [{"role": "user", "content": "analyze local files"}],
        "stream": True,
    }, headers=_AUTH) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert _RECOVERED in body
    assert refusal_phrase not in body
    assert "[DONE]" in body
