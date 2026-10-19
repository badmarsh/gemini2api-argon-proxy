"""Gemini model aliases must advertise their image/multimodal input support."""


_AUTH = {"Authorization": "Bearer sk-test-key"}
_MODELS = ("gemini-4-argon", "gemini-argon", "gemini-3.8-flash-high", "gemini-3.8-flash")


def test_models_endpoint_advertises_vision_for_gemini_and_related_aliases(gem_client):
    response = gem_client.get("/v1/models", headers=_AUTH)

    assert response.status_code == 200
    cards = {card["id"]: card for card in response.json()["data"]}

    for model in _MODELS:
        assert model in cards
        card = cards[model]
        assert card["vision"] is True
        assert card["supports_vision"] is True
        assert card["multimodal"] is True
        assert "image" in card["input_modalities"]
        assert "vision" in card["capabilities"]


def test_unknown_third_party_model_card_omits_unverified_modalities():
    from app.routers.openai import _model_info_card

    card = _model_info_card("third-party/example", 1).model_dump(exclude_none=True)

    assert card["id"] == "third-party/example"
    assert "vision" not in card
    assert "input_modalities" not in card
