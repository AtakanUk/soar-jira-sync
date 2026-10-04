from __future__ import annotations

import os
from pathlib import Path

import config
from models.sync_models import MappingRecord, SyncSource, SyncState
from repositories.mapping_repository import MappingRepository
from services.mapping_service import MappingService


def _make_repo(base_dir: Path) -> MappingRepository:
    db_path = base_dir / "test_sync.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    config.get_settings.cache_clear()
    repo = MappingRepository()
    repo.db_path = str(db_path)
    repo._ensure_db()
    return repo


def test_mapping_repository_crud(local_test_db_dir: Path) -> None:
    repo = _make_repo(local_test_db_dir)
    record = repo.create(
        MappingRecord(correlation_key="alert:1", soar_incident_id="INC-1", jira_issue_key="SOC-1")
    )
    found = repo.find_by_correlation_key("alert:1")
    assert record.id is not None
    assert found is not None
    assert found.soar_incident_id == "INC-1"
    assert found.jira_issue_key == "SOC-1"


def test_mapping_service_updates_existing_record(local_test_db_dir: Path) -> None:
    repo = _make_repo(local_test_db_dir)
    service = MappingService(repo)
    created, is_new = service.find_or_create(correlation_key="alert:2", soar_incident_id="INC-2")
    assert is_new is True
    reused, is_new = service.find_or_create(correlation_key="alert:2", jira_issue_key="SOC-2")
    assert is_new is False
    assert reused.jira_issue_key == "SOC-2"
    assert created.id == reused.id


def test_mark_synced_and_error(local_test_db_dir: Path) -> None:
    repo = _make_repo(local_test_db_dir)
    service = MappingService(repo)
    record, _ = service.find_or_create(correlation_key="alert:3")
    service.mark_synced(record, SyncSource.JIRA, jira_status="In Progress")
    synced = repo.find_by_correlation_key("alert:3")
    assert synced is not None
    assert synced.current_sync_state == SyncState.SYNCED
    service.mark_error(record, "boom")
    errored = repo.find_by_correlation_key("alert:3")
    assert errored is not None
    assert errored.current_sync_state == SyncState.ERROR
    assert errored.retry_count == 1
