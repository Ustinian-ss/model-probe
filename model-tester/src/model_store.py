from __future__ import annotations

import json
from pathlib import Path
import sys

from models import ModelItem


def _default_models_path() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "config" / "models.json"
    return Path(__file__).resolve().parents[1] / "config" / "models.json"


DEFAULT_MODELS = _default_models_path()


def load_model_items(path: Path | None = None) -> list[ModelItem]:
    file_path = path or DEFAULT_MODELS
    if not file_path.exists():
        return []

    data = json.loads(file_path.read_text(encoding="utf-8"))
    return [
        ModelItem(
            id=item["id"],
            vendor=item.get("vendor", item["id"].split("/", 1)[0]),
            name=item.get("name", item["id"].split("/", 1)[-1]),
        )
        for item in data.get("models", [])
    ]
