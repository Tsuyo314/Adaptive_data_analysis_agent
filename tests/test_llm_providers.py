import json
import time
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from data_agent.llm.chatgpt_auth import ChatGPTAuthClient, create_pkce
from data_agent.llm.preferences import Preferences
from data_agent.llm.providers import ChatGPTProvider, OpenRouterProvider
from data_agent.llm.schemas import QuestionSelection


class FakeAuth:
    def access_token(self) -> str:
        return "secret-chatgpt-token"


def test_pkce_uses_s256_without_padding():
    verifier, challenge = create_pkce()
    assert verifier
    assert challenge
    assert "=" not in challenge


def test_chatgpt_lists_visible_models_and_streams_required_request(tmp_path: Path):
    del tmp_path
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-chatgpt-token"
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "visible", "display_name": "Visible", "visibility": "list"},
                        {"slug": "hidden", "display_name": "Hidden", "visibility": "hide"},
                    ]
                },
            )
        captured.update(json.loads(request.content))
        events = [
            {"type": "response.output_text.delta", "delta": '{"question_ids":['},
            {"type": "response.output_text.delta", "delta": '"analysis_scope"],"rationale":"ok"}'},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 3}}},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ChatGPTProvider(FakeAuth(), "visible", http_client=client)
    assert [model.id for model in provider.list_models()] == ["visible"]
    result = provider.generate_structured(
        task="question_planner",
        instructions="test",
        payload={"candidate_questions": [{"id": "analysis_scope"}]},
        schema=QuestionSelection,
    )
    assert result.value["question_ids"] == ["analysis_scope"]
    assert captured["store"] is False
    assert captured["stream"] is True
    assert isinstance(captured["input"], list)
    assert captured["text"]["format"]["schema"]["additionalProperties"] is False
    for forbidden in ("temperature", "max_output_tokens", "top_p", "previous_response_id"):
        assert forbidden not in captured


def test_openrouter_filters_models_and_requires_structured_output():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer router-secret"
        if request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "structured/model",
                            "name": "Structured",
                            "supported_parameters": ["structured_outputs"],
                        },
                        {"id": "plain/model", "supported_parameters": ["tools"]},
                    ]
                },
            )
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"question_ids":[],"rationale":"ok"}'}}],
                "usage": {"prompt_tokens": 2},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenRouterProvider("structured/model", api_key="router-secret", http_client=client)
    assert [model.id for model in provider.list_models()] == ["structured/model"]
    provider.generate_structured(
        task="question_planner",
        instructions="test",
        payload={},
        schema=QuestionSelection,
    )
    assert captured["provider"]["require_parameters"] is True
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True
    schema = captured["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False


def test_chatgpt_id_token_signature_claims_nonce_and_scope_are_validated(tmp_path: Path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "test-key"
    now = int(time.time())
    id_token = jwt.encode(
        {
            "iss": "https://auth.openai.com",
            "aud": "oaiapp_test",
            "sub": "account-subject",
            "email": "user@example.com",
            "nonce": "expected-nonce",
            "iat": now,
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.openai.com",
                    "jwks_uri": "https://auth.openai.com/.well-known/jwks.json",
                },
            )
        return httpx.Response(200, json={"keys": [public_jwk]})

    auth = ChatGPTAuthClient(
        preferences=Preferences(tmp_path / "preferences.json"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    tokens = auth._validate_tokens(
        {
            "id_token": id_token,
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
            "scope": "openid profile email offline_access resource.invoke "
            "chatgpt.tokens.use.direct",
        },
        "oaiapp_test",
        "expected-nonce",
    )
    assert tokens.subject == "account-subject"
    assert tokens.access_token == "access-secret"
