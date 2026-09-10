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

# 最近一次 load_model_items 失败的原因（供 UI 提示），None 表示正常
LAST_LOAD_ERROR: str | None = None


def load_model_items(path: Path | None = None) -> list[ModelItem]:
    global LAST_LOAD_ERROR
    file_path = path or DEFAULT_MODELS
    if not file_path.exists():
        LAST_LOAD_ERROR = f"内置模型清单缺失：{file_path}（打包产物未包含 config/models.json？）"
        return []
    LAST_LOAD_ERROR = None

    data = json.loads(file_path.read_text(encoding="utf-8"))
    return [
        ModelItem(
            id=item["id"],
            vendor=item.get("vendor", item["id"].split("/", 1)[0]),
            name=item.get("name", item["id"].split("/", 1)[-1]),
        )
        for item in data.get("models", [])
    ]
