from __future__ import annotations

import re
from typing import Any


def sanitize_label(value: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9_-]", "-", value).strip("-").lower()
    return label[:255] if label else "unknown"


def normalize_alarm(payload: dict[str, Any]) -> dict[str, Any]:
    incident_id = str(payload.get("incident_id") or payload.get("inc_id") or "")
    alert_id = str(payload.get("alert_id") or payload.get("id") or payload.get("uuid") or "")
    summary = (
        payload.get("summary")
        or payload.get("title")
        or payload.get("name")
        or f"SOAR alert {alert_id or incident_id or 'unknown'}"
    )
    description = payload.get("description") or payload.get("details") or ""
    severity = str(payload.get("severity") or payload.get("priority") or "medium")
    source = str(payload.get("source") or payload.get("origin") or "soar")
    event_type = str(payload.get("event_type") or payload.get("action") or "create").lower()
    assignee = payload.get("assignee") or payload.get("owner")
    tags = payload.get("tags") or payload.get("labels") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    return {
        "incident_id": incident_id,
        "alert_id": alert_id,
        "summary": summary,
        "description": description,
        "severity": severity,
        "source": source,
        "event_type": event_type,
        "assignee": assignee,
        "tags": tags,
        "raw": payload,
    }
