from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ModelItem:
    id: str
    vendor: str
    name: str
    selected: bool = True
    status: str = "pending"
    latency_ms: int | None = None
    streaming_supported: bool | None = None
    last_error: str | None = None
    response_preview: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
