from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os
import tempfile
from typing import Any


APP_DIR = Path.home() / ".nvidia_model_tester"
PROVIDERS_FILE = APP_DIR / "providers.json"

# 最近一次 load_providers 遇到的错误（供 UI 提示），None 表示正常。
# 关键场景：providers.json 损坏时不要让 UI 悄悄回退默认值再把用户真实配置覆盖掉。
LAST_LOAD_ERROR: str | None = None


@dataclass
class Provider:
    name: str = "Nvidia"
    base_url: str = "https://integrate.api.nvidia.com/v1"
    api_key: str = ""
    timeout_seconds: int = 30
    max_workers: int = 5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_provider() -> Provider:
    return Provider()


def _provider_from_item(item: Any) -> Provider:
    """逐字段解析，容忍多余/缺失字段（直接 Provider(**item) 遇到多余键会抛 TypeError）。"""
    if not isinstance(item, dict):
        raise TypeError(f"provider 条目应为对象，实际是 {type(item).__name__}")
    return Provider(
        name=str(item.get("name", "Nvidia")),
        base_url=str(item.get("base_url", "")),
        api_key=str(item.get("api_key", "")),
        timeout_seconds=int(item.get("timeout_seconds", 30)),
        max_workers=int(item.get("max_workers", 5)),
    )


def load_providers() -> list[Provider]:
    global LAST_LOAD_ERROR
    LAST_LOAD_ERROR = None
    if not PROVIDERS_FILE.exists():
        return [default_provider()]

    try:
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        # 损坏的配置先抢救一份，避免后续保存把它永久覆盖
        LAST_LOAD_ERROR = f"providers.json 解析失败（{exc}）"
        try:
            corrupt = PROVIDERS_FILE.with_suffix(".json.corrupt")
            corrupt.write_text(PROVIDERS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
            LAST_LOAD_ERROR += f"，原文件已备份到 {corrupt}"
        except OSError:
            pass
        return []

    providers: list[Provider] = []
    for item in data.get("providers", []):
        try:
            providers.append(_provider_from_item(item))
        except Exception as exc:
            LAST_LOAD_ERROR = f"providers.json 有一条配置无法解析（{exc}），已跳过该条"
    return providers


def save_providers(providers: list[Provider]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"providers": [provider.to_dict() for provider in providers]}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    # 写前备份 + 原子替换：避免写入中途崩溃留下截断文件丢掉全部 API Key
    if PROVIDERS_FILE.exists():
        try:
            PROVIDERS_FILE.with_suffix(".json.bak").write_text(
                PROVIDERS_FILE.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError:
            pass
    fd, tmp_name = tempfile.mkstemp(dir=APP_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, PROVIDERS_FILE)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_default_provider(providers: list[Provider]) -> list[Provider]:
    if not providers:
        return [default_provider()]
    return providers


def provider_dir() -> Path:
    return APP_DIR
