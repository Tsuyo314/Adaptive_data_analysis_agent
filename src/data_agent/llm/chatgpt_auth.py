from __future__ import annotations

import base64
import hashlib
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt

from data_agent.llm.base import ProviderError
from data_agent.llm.preferences import Preferences

AUTHORIZATION_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
OIDC_CONFIGURATION = "https://auth.openai.com/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


@dataclass
class ChatGPTTokens:
    access_token: str
    refresh_token: str
    id_token: str
    client_id: str
    scopes: set[str]
    expires_at: float
    subject: str
    email: str = ""


def create_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class ChatGPTAuthClient:
    def __init__(
        self,
        *,
        preferences: Preferences | None = None,
        timeout: float = 120.0,
        http_client: httpx.Client | None = None,
    ):
        self.preferences = preferences or Preferences()
        self.timeout = timeout
        self.http = http_client or httpx.Client(timeout=timeout)
        self.tokens: ChatGPTTokens | None = None

    def authenticate(self, browser_open: Callable[[str], Any] = webbrowser.open) -> ChatGPTTokens:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier, challenge = create_pkce()
        callback: dict[str, str] = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if urlparse(self.path).path != "/auth/callback":
                    self.send_error(404)
                    return
                values = parse_qs(urlparse(self.path).query)
                callback.update({key: items[0] for key, items in values.items() if items})
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write("認証が完了しました。この画面を閉じてください。".encode())

            def log_message(self, format: str, *args: Any) -> None:
                del format, args

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        server.timeout = self.timeout
        redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
        saved_client_id = self.preferences.issued_client_id()
        client_id = saved_client_id or "dynamic_agent_client"
        parameters = {
            "client_id": client_id,
            "ext_agent_host_id": self.preferences.host_id(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if not saved_client_id:
            parameters["agent_name_hint"] = "Adaptive Data Analysis Agent"
        browser_open(f"{AUTHORIZATION_ENDPOINT}?{urlencode(parameters)}")
        try:
            server.handle_request()
        finally:
            server.server_close()
        if not callback:
            raise ProviderError("ChatGPT認証コールバックがタイムアウトしました")
        if not secrets.compare_digest(callback.get("state", ""), state):
            raise ProviderError("ChatGPT認証のstateが一致しません")
        if callback.get("error"):
            raise ProviderError(f"ChatGPT認証が拒否されました: {callback['error']}")
        code = callback.get("code")
        issued_id = callback.get("client_id") or saved_client_id
        if not code or not issued_id or issued_id == "dynamic_agent_client":
            raise ProviderError("ChatGPT認証コードまたは発行済みclient_idがありません")
        if saved_client_id and issued_id != saved_client_id:
            raise ProviderError("ChatGPT認証で別のclient_idが返されました")
        token_response = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": issued_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "resource": RESOURCE,
            }
        )
        tokens = self._validate_tokens(token_response, issued_id, nonce)
        self.preferences.set_issued_client_id(issued_id)
        self.tokens = tokens
        return tokens

    def access_token(self) -> str:
        if self.tokens is None:
            raise ProviderError("Continue with ChatGPT でサインインしてください")
        if time.time() >= self.tokens.expires_at - 60:
            self.tokens = self._refresh(self.tokens)
        return self.tokens.access_token

    def _refresh(self, current: ChatGPTTokens) -> ChatGPTTokens:
        response = self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": current.client_id,
                "refresh_token": current.refresh_token,
                "resource": RESOURCE,
            }
        )
        # Refresh responses can omit an ID token; the already validated identity remains in memory.
        scopes = set(str(response.get("scope", "")).split()) or current.scopes
        self._require_plan_scope(scopes)
        return ChatGPTTokens(
            access_token=str(response["access_token"]),
            refresh_token=str(response.get("refresh_token", current.refresh_token)),
            id_token=str(response.get("id_token", current.id_token)),
            client_id=current.client_id,
            scopes=scopes,
            expires_at=time.time() + float(response.get("expires_in", 3600)),
            subject=current.subject,
            email=current.email,
        )

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.http.post(TOKEN_ENDPOINT, data=data)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"ChatGPTトークン取得に失敗しました: {exc}") from exc
        if not isinstance(value, dict):
            raise ProviderError("ChatGPTトークン応答が不正です")
        return value

    def _validate_tokens(
        self, response: dict[str, Any], client_id: str, nonce: str
    ) -> ChatGPTTokens:
        try:
            configuration_response = self.http.get(OIDC_CONFIGURATION)
            configuration_response.raise_for_status()
            configuration = configuration_response.json()
            issuer = str(configuration["issuer"])
            if issuer.rstrip("/") != "https://auth.openai.com":
                raise ValueError("unexpected issuer")
            jwks_response = self.http.get(str(configuration["jwks_uri"]))
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
            header = jwt.get_unverified_header(str(response["id_token"]))
            if header.get("alg") not in {"RS256", "ES256"}:
                raise ValueError("unsupported signing algorithm")
            matching = next(key for key in jwks["keys"] if key.get("kid") == header.get("kid"))
            public_key = jwt.PyJWK.from_dict(matching).key
            claims = jwt.decode(
                str(response["id_token"]),
                public_key,
                algorithms=[str(header["alg"])],
                audience=client_id,
                issuer=issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except Exception as exc:
            raise ProviderError(f"ChatGPT IDトークン検証に失敗しました: {exc}") from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ProviderError("ChatGPT IDトークンのnonceが一致しません")
        scopes = set(str(response.get("scope", "")).split())
        self._require_plan_scope(scopes)
        try:
            access_token = str(response["access_token"])
            refresh_token = str(response["refresh_token"])
        except KeyError as exc:
            raise ProviderError("ChatGPTトークン応答に必要な資格情報がありません") from exc
        return ChatGPTTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=str(response["id_token"]),
            client_id=client_id,
            scopes=scopes,
            expires_at=time.time() + float(response.get("expires_in", 3600)),
            subject=str(claims["sub"]),
            email=str(claims.get("email", "")),
        )

    @staticmethod
    def _require_plan_scope(scopes: set[str]) -> None:
        required = {"resource.invoke", "chatgpt.tokens.use.direct"}
        missing = required - scopes
        if missing:
            raise ProviderError(f"ChatGPTプラン利用権限がありません: {', '.join(sorted(missing))}")
