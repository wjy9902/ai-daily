from __future__ import annotations

from math import ceil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_daily.models import JUDGE_BATCH_SIZE, ModelsConfig, PipelineConfig, SourceConfig


class SourcesDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[SourceConfig]


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    dashscope_api_key: str | None = None
    dashscope_base_url: str | None = None
    deepseek_api_key: str | None = None
    github_token: str | None = None
    github_repository: str | None = None
    site_base_url: str | None = None


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pipeline: PipelineConfig
    models: ModelsConfig
    sources: list[SourceConfig]

    @model_validator(mode="after")
    def validate_pipeline_budget(self) -> AppConfig:
        pipeline = self.pipeline
        minimum_items = pipeline.lead_min + pipeline.follow_min + pipeline.brief_min
        if minimum_items > pipeline.candidate_limit:
            raise ValueError("minimum editorial selections exceed candidate_limit")
        normal_requests = (
            ceil(pipeline.candidate_limit / JUDGE_BATCH_SIZE)
            + 1
            + pipeline.lead_max
            + pipeline.follow_max
        )
        if normal_requests > self.models.budget.request_limit:
            raise ValueError("normal editorial run exceeds model request budget")
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def load_config(config_dir: Path = Path("config")) -> AppConfig:
    pipeline = PipelineConfig.model_validate(_read_yaml(config_dir / "pipeline.yaml"))
    models = ModelsConfig.model_validate(_read_yaml(config_dir / "models.yaml"))
    sources = SourcesDocument.model_validate(_read_yaml(config_dir / "sources.yaml"))
    return AppConfig(pipeline=pipeline, models=models, sources=sources.sources)
