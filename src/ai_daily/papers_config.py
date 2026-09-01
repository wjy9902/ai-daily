"""Independent configuration loader for the papers command."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .models import BudgetConfig, ModelsConfig, SourceConfig


class PapersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts_dir: str = Field(min_length=1)
    site_base_url: HttpUrl
    score_threshold: float = Field(ge=0)
    min_main_papers: int = Field(ge=3, le=8)
    max_papers: int = Field(ge=3, le=8)
    max_supplements: int = Field(ge=0, le=2)
    first_tier_organizations: list[str] = Field(min_length=1)
    budget: BudgetConfig
    sources: list[SourceConfig] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_limits(self) -> PapersConfig:
        if self.min_main_papers > self.max_papers:
            raise ValueError("min_main_papers cannot exceed max_papers")
        kinds = {source.kind for source in self.sources if source.enabled}
        if not {"arxiv", "huggingface"}.issubset(kinds):
            raise ValueError("papers sources require enabled arxiv and huggingface feeds")
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def load_papers_config(config_dir: Path = Path("config")) -> PapersConfig:
    """Load only papers.yaml; daily AppConfig never sees this schema."""

    return PapersConfig.model_validate(_read_yaml(config_dir / "papers.yaml"))


def load_papers_models(config_dir: Path = Path("config")) -> ModelsConfig:
    """Reuse model endpoints without constructing the daily AppConfig."""

    return ModelsConfig.model_validate(_read_yaml(config_dir / "models.yaml"))
