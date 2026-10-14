from __future__ import annotations

import json
import os
from typing import Any

import httpx
from pydantic import BaseModel

from data_agent.llm.base import (
    GenerationResult,
    InvalidStructuredResponse,
    ModelInfo,
    ProviderError,
    SchemaT,
)
from data_agent.llm.chatgpt_auth import ChatGPTAuthClient


def _parse_json_text(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidStructuredResponse(f"JSONとして解釈できません: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidStructuredResponse("構造化応答のルートはobjectである必要があります")
    return value


class ChatGPTProvider:
    name = "chatgpt"

    def __init__(
        self,
        auth: ChatGPTAuthClient,
        model: str = "",
        *,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
        http_client: httpx.Client | None = None,
    ):
        self.auth = auth
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.http = http_client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.access_token()}"}

    def list_models(self) -> list[ModelInfo]:
        try:
            response = self.http.get(f"{self.base_url}/models", headers=self._headers())
            response.raise_for_status()
            raw_models = response.json().get("models", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ProviderError(f"ChatGPTモデル一覧の取得に失敗しました: {exc}") from exc
        return [
            ModelInfo(str(item["slug"]), str(item.get("display_name") or item["slug"]))
            for item in raw_models
            if item.get("visibility") == "list" and item.get("slug")
        ]

    def generate_structured(
        self,
        *,
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: type[SchemaT],
        repair_error: str | None = None,
    ) -> GenerationResult:
        if not self.model:
            raise ProviderError("ChatGPTモデルが選択されていません")
        user_payload = {"task": task, "input": payload}
        if repair_error:
            user_payload["repair"] = repair_error
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": [{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": task,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                }
            },
            "store": False,
            "stream": True,
        }
        fragments: list[str] = []
        completed = False
        usage: dict[str, Any] = {}
        try:
            with self.http.stream(
                "POST", f"{self.base_url}/responses", headers=self._headers(), json=body
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        fragments.append(str(event.get("delta", "")))
                    elif event_type in {"response.failed", "response.incomplete", "error"}:
                        detail = (
                            event.get("response", {}).get("error") or event.get("error") or event
                        )
                        raise ProviderError(f"ChatGPT応答が失敗しました: {detail}")
                    elif event_type == "response.completed":
                        completed = True
                        usage = event.get("response", {}).get("usage", {}) or {}
        except InvalidStructuredResponse:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"ChatGPT推論に失敗しました: {exc}") from exc
        if not completed:
            raise ProviderError("ChatGPTストリームがresponse.completedなしで終了しました")
        return GenerationResult(_parse_json_text("".join(fragments)), usage)


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        model: str = "",
        *,
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 120.0,
        http_client: httpx.Client | None = None,
    ):
        self.model = model
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.http = http_client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderError("OPENROUTER_API_KEYが設定されていません")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def list_models(self) -> list[ModelInfo]:
        try:
            response = self.http.get(f"{self.base_url}/models", headers=self._headers())
            response.raise_for_status()
            values = response.json().get("data", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ProviderError(f"OpenRouterモデル一覧の取得に失敗しました: {exc}") from exc
        models = []
        for item in values:
            supported = set(item.get("supported_parameters") or [])
            if not {"structured_outputs", "response_format"}.intersection(supported):
                continue
            if item.get("id"):
                models.append(ModelInfo(str(item["id"]), str(item.get("name") or item["id"])))
        return models

    def generate_structured(
        self,
        *,
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: type[BaseModel],
        repair_error: str | None = None,
    ) -> GenerationResult:
        if not self.model:
            raise ProviderError("OpenRouterモデルが選択されていません")
        user_payload = {"task": task, "input": payload}
        if repair_error:
            user_payload["repair"] = repair_error
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": task,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
            "provider": {"require_parameters": True},
        }
        try:
            response = self.http.post(
                f"{self.base_url}/chat/completions", headers=self._headers(), json=body
            )
            response.raise_for_status()
            raw = response.json()
            content = raw["choices"][0]["message"]["content"]
            usage = raw.get("usage", {}) or {}
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"OpenRouter推論に失敗しました: {exc}") from exc
        return GenerationResult(_parse_json_text(str(content)), usage)
