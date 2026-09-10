from dataclasses import dataclass, asdict
from pathlib import Path
import json
from typing import Any


APP_DIR = Path.home() / ".nvidia_model_tester"
PROVIDERS_FILE = APP_DIR / "providers.json"


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


def load_providers() -> list[Provider]:
    if not PROVIDERS_FILE.exists():
        return [default_provider()]

    try:
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        providers = [Provider(**item) for item in data.get("providers", [])]
        return providers
    except Exception:
        return []


def save_providers(providers: list[Provider]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"providers": [provider.to_dict() for provider in providers]}
    PROVIDERS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_default_provider(providers: list[Provider]) -> list[Provider]:
    if not providers:
        return [default_provider()]
    return providers


def provider_dir() -> Path:
    return APP_DIR
