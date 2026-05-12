from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any


@cache
def catalog() -> dict[str, Any]:
    return json.loads((Path(__file__).with_name("model_catalog.json")).read_text())


def model_option(model_key: str) -> dict[str, str]:
    try:
        model = catalog()["models"][model_key]
    except KeyError as exc:
        raise KeyError(f"Unknown model key: {model_key}") from exc
    return {"value": model["value"], "label": model["label"]}


def model_options(group: str) -> list[dict[str, str]]:
    try:
        keys = catalog()["groups"][group]
    except KeyError as exc:
        raise KeyError(f"Unknown model group: {group}") from exc
    return [model_option(key) for key in keys]


def model_values(group: str) -> list[str]:
    return [option["value"] for option in model_options(group)]


def default_model(role: str) -> str:
    try:
        model_key = catalog()["defaults"][role]
    except KeyError as exc:
        raise KeyError(f"Unknown default model role: {role}") from exc
    return model_option(model_key)["value"]
