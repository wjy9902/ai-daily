from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ai_daily.config import AppConfig, load_config
from ai_daily.models import ModelsConfig


def test_project_config_loads_without_secrets() -> None:
    config = load_config(Path("config"))
    assert config.pipeline.lead_max == 5
    assert config.pipeline.follow_max == 7
    assert config.pipeline.brief_max == 12
    assert len(config.sources) >= 25
    assert config.models.roles["judge"].primary.provider == "alibaba"
    text = "\n".join(path.read_text() for path in Path("config").glob("*.yaml"))
    assert "sk-" not in text
    assert "api_key:" not in text.lower()


def test_model_fallback_must_cross_provider() -> None:
    value = yaml.safe_load(Path("config/models.yaml").read_text())
    value["roles"]["judge"]["fallback"]["provider"] = "alibaba"
    with pytest.raises(ValidationError, match="different provider"):
        ModelsConfig.model_validate(value)


def test_ollama_cannot_be_production_fallback() -> None:
    value = yaml.safe_load(Path("config/models.yaml").read_text())
    value["roles"]["judge"]["fallback"]["provider"] = "ollama"
    with pytest.raises(ValidationError, match="Ollama"):
        ModelsConfig.model_validate(value)


def test_pipeline_minimum_selections_must_fit_candidate_limit() -> None:
    config = load_config(Path("config"))
    value = config.model_dump(mode="json")
    value["pipeline"]["candidate_limit"] = 20
    value["pipeline"]["max_research_candidates"] = 5
    value["pipeline"]["max_release_candidates"] = 5
    value["pipeline"]["lead_min"] = 8
    value["pipeline"]["lead_max"] = 8
    value["pipeline"]["follow_min"] = 8
    value["pipeline"]["follow_max"] = 8
    value["pipeline"]["brief_min"] = 8
    value["pipeline"]["brief_max"] = 8

    with pytest.raises(ValidationError, match="candidate_limit"):
        AppConfig.model_validate(value)


def test_normal_editorial_run_must_fit_model_request_budget() -> None:
    config = load_config(Path("config"))
    value = config.model_dump(mode="json")
    value["models"]["budget"]["request_limit"] = 10

    with pytest.raises(ValidationError, match="request budget"):
        AppConfig.model_validate(value)
