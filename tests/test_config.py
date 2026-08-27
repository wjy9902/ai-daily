from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ai_daily.config import AppConfig, load_config
from ai_daily.models import ModelsConfig, SourceConfig, SourceTier


def test_project_config_loads_without_secrets() -> None:
    config = load_config(Path("config"))
    assert config.pipeline.lead_max == 5
    assert config.pipeline.follow_max == 8
    assert config.pipeline.brief_max == 16
    assert len(config.sources) >= 25
    # Assert the invariant, not the vendor: which provider leads is an
    # operational choice that changes with pricing and availability, but every
    # role must always have a primary and a fallback on different providers.
    for role in config.models.roles.values():
        assert role.primary.provider != role.fallback.provider
    text = "\n".join(path.read_text() for path in Path("config").glob("*.yaml"))
    assert "sk-" not in text
    assert "api_key:" not in text.lower()


def test_model_fallback_must_cross_provider() -> None:
    value = yaml.safe_load(Path("config/models.yaml").read_text())
    value["roles"]["judge"]["fallback"]["provider"] = value["roles"]["judge"]["primary"]["provider"]
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


def test_persona_recovery_request_budget_stays_within_schema_ceiling() -> None:
    config = load_config(Path("config"))

    assert config.persona is not None
    assert config.persona.budget.request_limit == 160


def test_huggingface_model_source_requires_namespace() -> None:
    with pytest.raises(ValidationError, match="require namespace"):
        SourceConfig(
            name="models",
            kind="huggingface_models",
            url="https://huggingface.co/api/models",
            tier=SourceTier.A,
        )


def test_namespace_is_rejected_for_unrelated_source_kinds() -> None:
    with pytest.raises(ValidationError, match="only valid"):
        SourceConfig(
            name="feed",
            kind="rss",
            url="https://example.com/feed",
            tier=SourceTier.A,
            namespace="Qwen",
        )
