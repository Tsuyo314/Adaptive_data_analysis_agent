from __future__ import annotations

import json
from typing import Any, Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from data_agent.llm.base import ProviderError

SERVICE_NAME = "adaptive-data-analysis-agent"
CHATGPT_ACCOUNT = "chatgpt-oauth"


class CredentialStore(Protocol):
    def load(self) -> dict[str, Any] | None: ...

    def save(self, value: dict[str, Any]) -> None: ...

    def clear(self) -> None: ...


class KeyringCredentialStore:
    """Store ChatGPT OAuth credentials in the operating-system credential store."""

    def __init__(self, service: str = SERVICE_NAME, account: str = CHATGPT_ACCOUNT):
        self.service = service
        self.account = account

    def load(self) -> dict[str, Any] | None:
        try:
            payload = keyring.get_password(self.service, self.account)
        except KeyringError as exc:
            raise ProviderError(f"OS資格情報ストアを読み取れません: {exc}") from exc
        if payload is None:
            return None
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProviderError("保存済みChatGPT認証情報が破損しています") from exc
        if not isinstance(value, dict):
            raise ProviderError("保存済みChatGPT認証情報の形式が不正です")
        return value

    def save(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        try:
            keyring.set_password(self.service, self.account, payload)
        except KeyringError as exc:
            raise ProviderError(f"OS資格情報ストアへ保存できません: {exc}") from exc

    def clear(self) -> None:
        try:
            keyring.delete_password(self.service, self.account)
        except PasswordDeleteError:
            return
        except KeyringError as exc:
            raise ProviderError(f"OS資格情報ストアから削除できません: {exc}") from exc
