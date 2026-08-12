from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ai_daily.config import load_config
from ai_daily.models import ModelsConfig


def test_project_config_loads_without_secrets() -> None:
    config = load_config(Path("config"))
    assert config.pipeline.selected_max == 12
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
