from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from config import get_settings
from models.sync_models import MappingRecord, SyncSource, SyncState
from utils.logger import get_logger
from utils.time_utils import utcnow

log = get_logger("mapping_repo")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    soar_incident_id TEXT,
    jira_issue_key TEXT,
    jira_issue_id TEXT,
    source_alarm_id TEXT,
    email_message_id TEXT,
    correlation_key TEXT NOT NULL UNIQUE,
    current_sync_state TEXT NOT NULL DEFAULT 'new',
    last_sync_source TEXT NOT NULL DEFAULT 'system',
    last_jira_status TEXT,
    last_soar_status TEXT,
    last_synced_at TEXT,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mapping_correlation ON mapping(correlation_key);
CREATE INDEX IF NOT EXISTS idx_mapping_jira_key ON mapping(jira_issue_key);
CREATE INDEX IF NOT EXISTS idx_mapping_soar_id ON mapping(soar_incident_id);
CREATE INDEX IF NOT EXISTS idx_mapping_state ON mapping(current_sync_state);
CREATE TABLE IF NOT EXISTS seen_entity (
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    fingerprint TEXT,
    payload_size INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (entity_type, entity_key)
);
CREATE INDEX IF NOT EXISTS idx_seen_last_seen ON seen_entity(last_seen_at);
"""


class MappingRepository:
    def __init__(self) -> None:
        settings = get_settings()
        self.db_path = settings.database.url.replace("sqlite:///", "")
        self.cache_settings = settings.cache
        self._seen_writes = 0
        self._ensure_db()

    def _ensure_db(self) -> None:
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_CREATE_TABLE)
        log.info("Mapping DB ready: %s", self.db_path)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MappingRecord:
        return MappingRecord(
            id=row["id"],
            soar_incident_id=row["soar_incident_id"],
            jira_issue_key=row["jira_issue_key"],
            jira_issue_id=row["jira_issue_id"],
            source_alarm_id=row["source_alarm_id"],
            email_message_id=row["email_message_id"],
            correlation_key=row["correlation_key"],
            current_sync_state=SyncState(row["current_sync_state"]),
            last_sync_source=SyncSource(row["last_sync_source"]),
            last_jira_status=row["last_jira_status"],
            last_soar_status=row["last_soar_status"],
            last_synced_at=datetime.fromisoformat(row["last_synced_at"])
            if row["last_synced_at"]
            else None,
            error_message=row["error_message"],
            retry_count=row["retry_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def create(self, record: MappingRecord) -> MappingRecord:
        now = utcnow().isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO mapping (
                    soar_incident_id, jira_issue_key, jira_issue_id,
                    source_alarm_id, email_message_id, correlation_key,
                    current_sync_state, last_sync_source,
                    last_jira_status, last_soar_status, last_synced_at,
                    error_message, retry_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.soar_incident_id,
                    record.jira_issue_key,
                    record.jira_issue_id,
                    record.source_alarm_id,
                    record.email_message_id,
                    record.correlation_key,
                    record.current_sync_state.value,
                    record.last_sync_source.value,
                    record.last_jira_status,
                    record.last_soar_status,
                    record.last_synced_at.isoformat() if record.last_synced_at else None,
                    record.error_message,
                    record.retry_count,
                    now,
                    now,
                ),
            )
            record.id = cursor.lastrowid
        record.created_at = datetime.fromisoformat(now)
        record.updated_at = datetime.fromisoformat(now)
        return record

    def update(self, record: MappingRecord) -> None:
        now = utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE mapping SET
                    soar_incident_id = ?,
                    jira_issue_key = ?,
                    jira_issue_id = ?,
                    source_alarm_id = ?,
                    email_message_id = ?,
                    current_sync_state = ?,
                    last_sync_source = ?,
                    last_jira_status = ?,
                    last_soar_status = ?,
                    last_synced_at = ?,
                    error_message = ?,
                    retry_count = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    record.soar_incident_id,
                    record.jira_issue_key,
                    record.jira_issue_id,
                    record.source_alarm_id,
                    record.email_message_id,
                    record.current_sync_state.value,
                    record.last_sync_source.value,
                    record.last_jira_status,
                    record.last_soar_status,
                    record.last_synced_at.isoformat() if record.last_synced_at else None,
                    record.error_message,
                    record.retry_count,
                    now,
                    record.id,
                ),
            )
        record.updated_at = datetime.fromisoformat(now)

    def find_by_correlation_key(self, key: str) -> MappingRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mapping WHERE correlation_key = ?", (key,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_jira_key(self, jira_key: str) -> MappingRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mapping WHERE jira_issue_key = ?", (jira_key,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_soar_id(self, soar_id: str) -> MappingRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mapping WHERE soar_incident_id = ?", (soar_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def find_active(self) -> list[MappingRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mapping WHERE current_sync_state != ?",
                (SyncState.CLOSED.value,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def find_by_state(self, state: SyncState) -> list[MappingRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mapping WHERE current_sync_state = ?",
                (state.value,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def find_errors(self) -> list[MappingRecord]:
        """Mappings in ERROR state, least retried first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mapping WHERE current_sync_state = ? ORDER BY retry_count ASC",
                (SyncState.ERROR.value,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def count_all(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS cnt FROM mapping").fetchone()
        return row["cnt"] if row else 0

    def is_seen(self, entity_type: str, entity_key: str, fingerprint: str | None = None) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fingerprint FROM seen_entity WHERE entity_type = ? AND entity_key = ?",
                (entity_type, entity_key),
            ).fetchone()
        if not row:
            return False
        return fingerprint is None or (row["fingerprint"] or "") == fingerprint

    def remember_seen(
        self,
        entity_type: str,
        entity_key: str,
        fingerprint: str | None = None,
        payload_size: int = 0,
    ) -> None:
        now = utcnow().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO seen_entity (
                    entity_type, entity_key, fingerprint, payload_size,
                    first_seen_at, last_seen_at, hit_count
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(entity_type, entity_key) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    payload_size = excluded.payload_size,
                    last_seen_at = excluded.last_seen_at,
                    hit_count = hit_count + 1
                """,
                (entity_type, entity_key, fingerprint, payload_size, now, now),
            )
        self._seen_writes += 1
        if self._seen_writes >= self.cache_settings.prune_every_n_writes:
            self.prune_seen_cache()
            self._seen_writes = 0

    def prune_seen_cache(self) -> None:
        deleted_total = 0
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS cnt FROM seen_entity").fetchone()
            count = row["cnt"] if row else 0
            overflow = max(0, count - self.cache_settings.max_seen_rows)
            while overflow > 0:
                batch = min(self.cache_settings.prune_batch_size, overflow)
                connection.execute(
                    "DELETE FROM seen_entity WHERE rowid IN (SELECT rowid FROM seen_entity ORDER BY last_seen_at ASC LIMIT ?)",
                    (batch,),
                )
                deleted_total += batch
                overflow -= batch

        try:
            db_file = Path(self.db_path)
            while db_file.exists() and db_file.stat().st_size > self.cache_settings.max_db_bytes:
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM seen_entity WHERE rowid IN (SELECT rowid FROM seen_entity ORDER BY last_seen_at ASC LIMIT ?)",
                        (self.cache_settings.prune_batch_size,),
                    )
                    deleted_total += self.cache_settings.prune_batch_size
                connection = sqlite3.connect(self.db_path)
                try:
                    connection.execute("VACUUM")
                finally:
                    connection.close()
        except Exception as exc:
            log.warning("Seen cache prune failed: %s", exc)

        if deleted_total:
            log.info("Seen cache pruned: %s records removed", deleted_total)
