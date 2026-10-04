from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SoarIncident:
    incident_id: str
    title: str
    status: str
    severity: str | None = None
    source: str | None = None
    description: str | None = None
    owner: str | None = None
    alert_id: str | None = None
    jira_key: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_closed(self) -> bool:
        return self.status.lower() in {"closed", "resolved"}
