from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from utils.time_utils import utcnow


class SyncState(StrEnum):
    NEW = "new"
    SYNCED = "synced"
    JIRA_AHEAD = "jira_ahead"
    SOAR_AHEAD = "soar_ahead"
    CONFLICT = "conflict"
    CLOSED = "closed"
    ERROR = "error"


class SyncSource(StrEnum):
    SOAR = "soar"
    JIRA = "jira"
    EMAIL = "email"
    SYSTEM = "system"


@dataclass
class MappingRecord:
    id: int | None = None
    soar_incident_id: str | None = None
    jira_issue_key: str | None = None
    jira_issue_id: str | None = None
    source_alarm_id: str | None = None
    email_message_id: str | None = None
    correlation_key: str = ""
    current_sync_state: SyncState = SyncState.NEW
    last_sync_source: SyncSource = SyncSource.SYSTEM
    last_jira_status: str | None = None
    last_soar_status: str | None = None
    last_synced_at: datetime | None = None
    error_message: str | None = None
    retry_count: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
