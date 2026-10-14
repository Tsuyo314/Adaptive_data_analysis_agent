import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from data_agent.config import Settings
from data_agent.llm.chatgpt_auth import ChatGPTAuthClient, create_pkce
from data_agent.llm.preferences import Preferences
from data_agent.llm.providers import ChatGPTProvider, OpenRouterProvider
from data_agent.llm.schemas import QuestionSelection


class FakeAuth:
    def access_token(self) -> str:
        return "secret-chatgpt-token"


class MemoryCredentialStore:
    def __init__(self, value=None):
        self.value = value
        self.clear_count = 0

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def clear(self):
        self.value = None
        self.clear_count += 1


def credential_record(now: float, authenticated_at: float, expires_at: float) -> dict:
    return {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "id_token": "old-id",
        "client_id": "oaiapp_test",
        "scopes": [
            "openid",
            "profile",
            "email",
            "offline_access",
            "resource.invoke",
            "chatgpt.tokens.use.direct",
        ],
        "expires_at": expires_at,
        "subject": "account-subject",
        "email": "user@example.com",
        "authenticated_at": authenticated_at,
    }


def test_pkce_uses_s256_without_padding():
    verifier, challenge = create_pkce()
    assert verifier
    assert challenge
    assert "=" not in challenge


def test_chatgpt_reuses_and_refreshes_credentials_within_one_day(tmp_path: Path):
    now = 2_000_000.0
    authenticated_at = now - 12 * 60 * 60
    credentials = MemoryCredentialStore(
        credential_record(now, authenticated_at, expires_at=now - 10)
    )
    preferences = Preferences(tmp_path / "preferences.json")
    preferences.set_issued_client_id("oaiapp_test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/accounts/oauth/token"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["old-refresh"]
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "scope": "openid profile email offline_access resource.invoke "
                "chatgpt.tokens.use.direct",
            },
        )

    auth = ChatGPTAuthClient(
        preferences=preferences,
        credential_store=credentials,
        reauth_days=1,
        now=lambda: now,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    tokens = auth.authenticate(browser_open=lambda _: (_ for _ in ()).throw(AssertionError()))
    assert auth.reused_session is True
    assert tokens.access_token == "new-access"
    assert credentials.value["refresh_token"] == "new-refresh"
    assert credentials.value["authenticated_at"] == authenticated_at


def test_chatgpt_expires_saved_session_after_one_day(tmp_path: Path):
    now = 2_000_000.0
    credentials = MemoryCredentialStore(
        credential_record(now, authenticated_at=now - 86_401, expires_at=now + 3600)
    )
    preferences = Preferences(tmp_path / "preferences.json")
    preferences.set_issued_client_id("oaiapp_test")
    revoked = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={"revocation_endpoint": "https://auth.openai.com/oauth/revoke"},
            )
        revoked.append(parse_qs(request.content.decode()))
        return httpx.Response(200)

    auth = ChatGPTAuthClient(
        preferences=preferences,
        credential_store=credentials,
        reauth_days=1,
        now=lambda: now,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert auth._restore_tokens() is None
    assert credentials.value is None
    assert credentials.clear_count == 1
    assert revoked[0]["token"] == ["old-refresh"]


def test_chatgpt_reauthentication_defaults_to_one_day():
    assert Settings().llm.chatgpt_reauth_days == 1.0


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
