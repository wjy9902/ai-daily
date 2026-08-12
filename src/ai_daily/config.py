from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_daily.models import ModelsConfig, PipelineConfig, SourceConfig


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
