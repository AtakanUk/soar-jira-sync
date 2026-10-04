from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
from clients.mail_client import ParsedEmail
from models.jira_models import JiraIssue
from models.sync_models import MappingRecord, SyncSource, SyncState
from repositories.mapping_repository import MappingRepository
from services.sync_service import SyncService


@pytest.fixture
def repo(tmp_path: Path) -> MappingRepository:
    db_path = tmp_path / "sync.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    config.get_settings.cache_clear()
    repository = MappingRepository()
    repository.db_path = str(db_path)
    repository._ensure_db()
    return repository


def make_issue(key: str = "SOC-1", status: str = "To Do", category: str = "new") -> JiraIssue:
    return JiraIssue(key=key, id="1", summary="Alert", status=status, status_category=category)


@pytest.fixture
def jira() -> MagicMock:
    client = MagicMock()
    client.project_key = "SOC"
    client._correlation_label.side_effect = lambda key: f"sync-{key}"
    client.create_issue.return_value = make_issue()
    return client


def test_new_alarm_creates_issue_and_links_soar(repo, jira) -> None:
    soar = MagicMock()
    service = SyncService(jira=jira, soar=soar, mail=None, repo=repo)

    result = service.handle_new_alarm(
        {"incident_id": "2001", "title": "Brute force", "severity": "high"}
    )

    assert result["action"] == "created"
    assert jira.create_issue.call_args.kwargs["priority"] == "High"
    soar.set_jira_key.assert_called_once_with("2001", "SOC-1")
    mapping = repo.find_by_jira_key("SOC-1")
    assert mapping.current_sync_state == SyncState.SYNCED


def test_duplicate_alarm_only_comments(repo, jira) -> None:
    service = SyncService(jira=jira, soar=None, mail=None, repo=repo)
    service.handle_new_alarm({"incident_id": "2002", "title": "Port scan"})
    jira.add_comment.side_effect = RuntimeError("Jira down")

    result = service.handle_new_alarm({"incident_id": "2002", "title": "Port scan"})

    assert result["action"] == "duplicate"
    assert jira.create_issue.call_count == 1


def test_failed_soar_close_puts_mapping_in_retry_queue(repo, jira) -> None:
    soar = MagicMock()
    soar.close_incident.side_effect = RuntimeError("SOAR unavailable")
    record = repo.create(
        MappingRecord(
            correlation_key="soar:3001",
            soar_incident_id="3001",
            jira_issue_key="SOC-7",
            last_jira_status="In Progress",
            last_sync_source=SyncSource.JIRA,
        )
    )
    jira.search_issues.return_value = [make_issue("SOC-7", "Done", "done")]
    service = SyncService(jira=jira, soar=soar, mail=None, repo=repo)

    stats = service.poll_jira()

    assert stats["errors"] == 1 and stats["closed"] == 0
    stored = repo.find_by_correlation_key(record.correlation_key)
    assert stored.current_sync_state == SyncState.ERROR
    assert stored.retry_count == 1


def test_status_change_is_mirrored_even_if_soar_update_fails(repo, jira) -> None:
    soar = MagicMock()
    soar.update_incident.side_effect = RuntimeError("timeout")
    repo.create(
        MappingRecord(
            correlation_key="soar:3002",
            soar_incident_id="3002",
            jira_issue_key="SOC-8",
            last_jira_status="To Do",
            last_sync_source=SyncSource.JIRA,
        )
    )
    jira.search_issues.return_value = [make_issue("SOC-8", "In Progress", "indeterminate")]
    service = SyncService(jira=jira, soar=soar, mail=None, repo=repo)

    service.poll_jira()

    assert repo.find_by_jira_key("SOC-8").last_jira_status == "In Progress"


def test_closure_mail_closes_soar_even_if_jira_fails(repo, jira) -> None:
    soar = MagicMock()
    jira.get_transitions.side_effect = RuntimeError("Jira down")
    repo.create(
        MappingRecord(correlation_key="soar:4001", soar_incident_id="4001", jira_issue_key="SOC-9")
    )
    service = SyncService(jira=jira, soar=soar, mail=None, repo=repo)
    parsed = ParsedEmail(
        message_id="<m1>",
        subject="SOC-9 closed",
        sender="team",
        body="",
        date="",
        uid="1",
        jira_key="SOC-9",
        is_closure=True,
    )
    stats = {"closures": 0, "errors": 0}

    service._handle_mail_closure(parsed, stats)

    soar.close_incident.assert_called_once()
    assert stats["closures"] == 1
    assert repo.find_by_jira_key("SOC-9").current_sync_state == SyncState.CLOSED


def test_status_reply_failure_is_counted_not_raised(repo, jira) -> None:
    jira.add_comment.side_effect = RuntimeError("Jira down")
    repo.create(
        MappingRecord(correlation_key="soar:5001", soar_incident_id="5001", jira_issue_key="SOC-10")
    )
    service = SyncService(jira=jira, soar=None, mail=None, repo=repo)
    parsed = ParsedEmail(
        message_id="<m2>",
        subject="Re: incident",
        sender="team",
        body="",
        date="",
        uid="2",
        jira_key="SOC-10",
        response_status="Known Offense",
        is_status_update=True,
    )
    stats = {"updates": 0, "errors": 0}

    service._handle_mail_status_update(parsed, stats)

    assert stats == {"updates": 0, "errors": 1}
