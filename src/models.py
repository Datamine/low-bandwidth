from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any


@dataclass(slots=True)
class ProcessUsage:
    pid: int | None
    name: str
    display_name: str
    command: str | None
    executable: str | None
    bundle_name: str | None
    ports: list[str]
    download_bytes: int
    upload_bytes: int
    total_bytes: int
    download_rate_bps: float
    upload_rate_bps: float
    total_rate_bps: float
    is_background: bool
    recipe_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Recipe:
    recipe_id: str
    title: str
    summary: str
    instructions: str
    command_preview: str
    admin_required: bool
    temporary: bool
    disruptive: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ActionResult:
    ok: bool
    title: str
    detail: str
    command: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Snapshot:
    supported: bool
    platform: str
    collector: str
    sample_seconds: int
    averaging_window_seconds: int | None
    processes: list[ProcessUsage]
    notices: list[str]
    collected_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["processes"] = [process.to_dict() for process in self.processes]
        return payload
