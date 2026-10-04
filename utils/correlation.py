from __future__ import annotations

import hashlib
from typing import Any


def generate_correlation_key(
    soar_incident_id: str | None = None,
    alert_id: str | None = None,
    email_message_id: str | None = None,
    subject: str | None = None,
    timestamp: str | None = None,
    source: str | None = None,
) -> str:
    if soar_incident_id:
        return f"soar:{soar_incident_id}"
    if alert_id:
        return f"alert:{alert_id}"
    if email_message_id:
        return f"email:{email_message_id}"

    composite = f"{subject or ''}|{timestamp or ''}|{source or ''}"
    digest = hashlib.sha256(composite.encode("utf-8")).hexdigest()[:16]
    return f"composite:{digest}"


def extract_correlation_from_payload(payload: dict[str, Any]) -> str:
    return generate_correlation_key(
        soar_incident_id=str(payload.get("incident_id") or ""),
        alert_id=str(payload.get("alert_id") or payload.get("id") or ""),
        subject=str(payload.get("summary") or payload.get("title") or ""),
        timestamp=str(payload.get("created_at") or payload.get("timestamp") or ""),
        source=str(payload.get("source") or payload.get("origin") or ""),
    )
