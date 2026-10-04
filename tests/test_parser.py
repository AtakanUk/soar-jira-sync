from __future__ import annotations

from models.jira_models import JiraIssue
from services.parser_service import normalize_alarm, sanitize_label
from utils.correlation import extract_correlation_from_payload, generate_correlation_key


def test_generate_correlation_key_prefers_soar_id() -> None:
    assert generate_correlation_key(soar_incident_id="INC-1", alert_id="ALERT-1") == "soar:INC-1"


def test_generate_correlation_key_falls_back_to_composite() -> None:
    key = generate_correlation_key(subject="test", timestamp="2026-01-01", source="mail")
    assert key.startswith("composite:")
    assert len(key) == len("composite:") + 16


def test_extract_correlation_from_payload() -> None:
    payload = {"incident_id": "INC-42", "alert_id": "ALERT-2", "summary": "Alert"}
    assert extract_correlation_from_payload(payload) == "soar:INC-42"


def test_normalize_alarm() -> None:
    payload = {
        "incident_id": "INC-1",
        "title": "Suspicious Login",
        "severity": "high",
        "source": "siem",
    }
    normalized = normalize_alarm(payload)
    assert normalized["summary"] == "Suspicious Login"
    assert normalized["event_type"] == "create"
    assert normalized["severity"] == "high"


def test_sanitize_label() -> None:
    assert sanitize_label("SOC Alert #123") == "soc-alert--123"
    assert sanitize_label("") == "unknown"


def test_jira_issue_model_parses_status() -> None:
    issue = JiraIssue.from_api_response(
        {
            "key": "SOC-1",
            "id": "10001",
            "fields": {
                "summary": "Test issue",
                "status": {"name": "Done", "statusCategory": {"key": "done"}},
                "assignee": {"accountId": "user-1"},
                "labels": ["soc-alert"],
                "priority": {"name": "High"},
                "resolution": {"name": "Resolved"},
                "updated": "2026-01-01T00:00:00.000+0000",
            },
        }
    )
    assert issue.is_closed is True
    assert issue.assignee == "user-1"
