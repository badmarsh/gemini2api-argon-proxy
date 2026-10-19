"""/v1/responses must not deliver Google's soft-error phrases as a successful assistant reply (audit item K).

chat/completions already turns such a phrase into a retryable error once fallback cannot rescue it; the
responses router computed the same flag and never read it, so a known upstream failure came back as HTTP 200.
"""
from app.core.gemini_client import GOOGLE_WEB_ERROR_PHRASES

_AUTH = {"Authorization": "Bearer sk-test-key"}
_PHRASE = sorted(GOOGLE_WEB_ERROR_PHRASES)[0]


def _fake_generate(text):
    async def fake_generate(prompt, model, conversation_id="", attachments=None,
                            gem_id=None, account_id=None, extended_thinking=False):
        return {"text": text, "conversation_id": "", "images": []}
    return fake_generate


def test_non_stream_error_phrase_is_an_error_not_a_200(gem_client, monkeypatch):
    import app.routers.responses as rr

    monkeypatch.setattr(rr.gemini_client, "generate", _fake_generate(_PHRASE))
    r = gem_client.post("/v1/responses", json={"model": "gemini-pro", "input": "hi"}, headers=_AUTH)
    assert r.status_code >= 400, r.text
    assert "error" in r.json()


def test_non_stream_normal_text_still_succeeds(gem_client, monkeypatch):
    import app.routers.responses as rr

    monkeypatch.setattr(rr.gemini_client, "generate", _fake_generate("All good"))
    r = gem_client.post("/v1/responses", json={"model": "gemini-pro", "input": "hi"}, headers=_AUTH)
    assert r.status_code == 200
    assert r.json()["output"][0]["content"][0]["text"] == "All good"


def test_stream_error_phrase_fails_the_response(gem_client, monkeypatch):
    import app.routers.responses as rr

    async def fake_generate_stream(prompt, model, conversation_id="", attachments=None,
                                   gem_id=None, account_id=None, extended_thinking=False):
        yield {"type": "final", "text": _PHRASE, "conversation_id": "", "images": []}

    async def fake_regeneration(*args, **kwargs):
        raise RuntimeError(_PHRASE)

    monkeypatch.setattr(rr.gemini_client, "generate_stream", fake_generate_stream)
    monkeypatch.setattr(rr.argon_fallback, "generate", fake_regeneration)
    with gem_client.stream("POST", "/v1/responses",
                           json={"model": "gemini-pro", "input": "hi", "stream": True},
                           headers=_AUTH) as r:
        body = "".join(r.iter_text())
    assert "response.failed" in body
    assert "response.completed" not in body
