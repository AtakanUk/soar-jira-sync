from __future__ import annotations

import sqlite3

from models.sync_models import MappingRecord, SyncSource, SyncState
from repositories.mapping_repository import MappingRepository
from utils.logger import get_logger
from utils.time_utils import utcnow

log = get_logger("mapping_service")


class MappingService:
    def __init__(self, repo: MappingRepository) -> None:
        self.repo = repo

    def find_or_create(
        self,
        correlation_key: str,
        soar_incident_id: str | None = None,
        jira_issue_key: str | None = None,
        jira_issue_id: str | None = None,
        source_alarm_id: str | None = None,
        email_message_id: str | None = None,
        source: SyncSource = SyncSource.SYSTEM,
    ) -> tuple[MappingRecord, bool]:
        existing = self.repo.find_by_correlation_key(correlation_key)
        if existing:
            changed = False
            for attr, value in {
                "soar_incident_id": soar_incident_id,
                "jira_issue_key": jira_issue_key,
                "jira_issue_id": jira_issue_id,
                "source_alarm_id": source_alarm_id,
                "email_message_id": email_message_id,
            }.items():
                if value and not getattr(existing, attr):
                    setattr(existing, attr, value)
                    changed = True
            if changed:
                self.repo.update(existing)
            return existing, False

        record = MappingRecord(
            correlation_key=correlation_key,
            soar_incident_id=soar_incident_id,
            jira_issue_key=jira_issue_key,
            jira_issue_id=jira_issue_id,
            source_alarm_id=source_alarm_id,
            email_message_id=email_message_id,
            last_sync_source=source,
        )
        try:
            return self.repo.create(record), True
        except sqlite3.IntegrityError:
            existing = self.repo.find_by_correlation_key(correlation_key)
            if existing:
                return existing, False
            raise

    def mark_synced(
        self,
        record: MappingRecord,
        source: SyncSource,
        jira_status: str | None = None,
        soar_status: str | None = None,
    ) -> None:
        record.current_sync_state = SyncState.SYNCED
        record.last_sync_source = source
        record.last_synced_at = utcnow()
        record.error_message = None
        record.retry_count = 0
        if jira_status:
            record.last_jira_status = jira_status
        if soar_status:
            record.last_soar_status = soar_status
        self.repo.update(record)

    def mark_closed(self, record: MappingRecord, source: SyncSource) -> None:
        record.current_sync_state = SyncState.CLOSED
        record.last_sync_source = source
        record.last_synced_at = utcnow()
        record.error_message = None
        self.repo.update(record)

    def mark_error(self, record: MappingRecord, error: str) -> None:
        record.current_sync_state = SyncState.ERROR
        record.error_message = error
        record.retry_count += 1
        self.repo.update(record)
        log.error("Mapping %s error #%s: %s", record.correlation_key, record.retry_count, error)
