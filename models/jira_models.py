from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class JiraIssue:
    key: str
    id: str
    summary: str
    status: str
    status_category: str
    assignee: str | None = None
    labels: list[str] = field(default_factory=list)
    priority: str | None = None
    resolution: str | None = None
    description: str | None = None
    updated: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> JiraIssue:
        fields = data.get("fields", {}) or {}
        status_data = fields.get("status", {}) or {}
        assignee_data = fields.get("assignee") or {}
        priority_data = fields.get("priority") or {}
        resolution_data = fields.get("resolution") or {}
        return cls(
            key=data.get("key", ""),
            id=str(data.get("id", "")),
            summary=fields.get("summary", ""),
            status=status_data.get("name", ""),
            status_category=(status_data.get("statusCategory") or {}).get("key", ""),
            assignee=assignee_data.get("accountId"),
            labels=list(fields.get("labels") or []),
            priority=priority_data.get("name"),
            resolution=resolution_data.get("name") if resolution_data else None,
            description=_extract_text(fields.get("description")),
            updated=fields.get("updated"),
            raw=data,
        )

    @property
    def is_closed(self) -> bool:
        return self.status_category == "done"


@dataclass
class JiraTransition:
    id: str
    name: str
    to_status: str
    to_category: str


def _extract_text(adf: Any) -> str:
    if not isinstance(adf, dict):
        return ""
    parts = [
        inline.get("text", "")
        for block in adf.get("content", [])
        for inline in block.get("content", [])
        if inline.get("type") == "text"
    ]
    return "\n".join(parts)
