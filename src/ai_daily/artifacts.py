from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def write_artifact(path: Path, value: BaseModel | Sequence[BaseModel] | dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: Any
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Sequence):
        payload = [item.model_dump(mode="json") for item in value]
    else:
        payload = value
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
