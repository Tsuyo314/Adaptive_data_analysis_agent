from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from platformdirs import user_config_path


class Preferences:
    """Non-secret preferences and versioned external-data consent."""

    def __init__(self, path: Path | None = None):
        self.path = path or user_config_path("adaptive-data-analysis-agent") / "preferences.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "consents": {}, "last_models": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "consents": {}, "last_models": {}}
        return value if isinstance(value, dict) else {"schema_version": 1}

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".preferences-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def host_id(self) -> str:
        value = self.load()
        host_id = value.get("ext_agent_host_id")
        if not host_id:
            host_id = f"urn:uuid:{uuid.uuid4()}"
            value["ext_agent_host_id"] = host_id
            self.save(value)
        return str(host_id)

    def issued_client_id(self) -> str | None:
        value = self.load().get("chatgpt_issued_client_id")
        return str(value) if value else None

    def set_issued_client_id(self, client_id: str) -> None:
        value = self.load()
        value["chatgpt_issued_client_id"] = client_id
        self.save(value)

    def has_consent(self, provider: str, policy_version: str) -> bool:
        stored_version = self.load().get("consents", {}).get(provider, {}).get("policy_version")
        return stored_version == policy_version

    def grant_consent(self, provider: str, policy_version: str) -> None:
        value = self.load()
        value.setdefault("consents", {})[provider] = {
            "policy_version": policy_version,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save(value)

    def last_selection(self) -> tuple[str | None, dict[str, str]]:
        value = self.load()
        provider = value.get("last_provider")
        models = value.get("last_models", {})
        return (str(provider) if provider else None, {str(k): str(v) for k, v in models.items()})

    def remember_selection(self, provider: str, model: str = "") -> None:
        value = self.load()
        value["last_provider"] = provider
        if model:
            value.setdefault("last_models", {})[provider] = model
        self.save(value)
