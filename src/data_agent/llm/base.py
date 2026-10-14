from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ProviderError(RuntimeError):
    """A provider, credential, transport, or model response failure."""


class InvalidStructuredResponse(ProviderError):
    """The provider completed, but the returned JSON was malformed."""


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str


@dataclass(frozen=True)
class GenerationResult:
    value: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    model: str

    def list_models(self) -> list[ModelInfo]: ...

    def generate_structured(
        self,
        *,
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: type[SchemaT],
        repair_error: str | None = None,
    ) -> GenerationResult: ...
