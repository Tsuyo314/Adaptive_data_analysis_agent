from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Settings:
    max_file_size_mb: int = 100
    metadata_sample_rows: int = 200
    max_supporting_file_kb: int = 256
    max_questions_per_round: int = 4
    max_question_rounds: int = 5
    max_retries: int = 2
    random_seed: int = 42
    missing_strategy: str = "keep"
    duplicate_strategy: str = "drop"
    runs_dir: str = "runs"
    allowed_data_extensions: tuple[str, ...] = (".csv", ".xlsx")
    allowed_support_extensions: tuple[str, ...] = (
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".py",
    )
    phase_weights: dict[str, int] = field(
        default_factory=lambda: {
            "discovery": 10,
            "questions": 15,
            "planning": 10,
            "preprocessing": 15,
            "eda": 20,
            "specialist": 15,
            "reporting": 15,
        }
    )

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or Path("conf/config.yaml")
        if not path.exists():
            return cls()
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        values = {key: value for key, value in raw.items() if key in cls.__dataclass_fields__}
        for key in ("allowed_data_extensions", "allowed_support_extensions"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)
