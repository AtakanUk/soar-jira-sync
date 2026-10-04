from __future__ import annotations

from unittest.mock import MagicMock, patch

from clients.jira_client import JiraClient


@patch("clients.jira_client.requests.request")
def test_jira_connection(mock_request: MagicMock) -> None:
    response = MagicMock()
    response.status_code = 200
    response.text = "{}"
    mock_request.return_value = response
    assert JiraClient().test_connection() is True


@patch("clients.jira_client.requests.request")
def test_create_issue(mock_request: MagicMock) -> None:
    create_response = MagicMock()
    create_response.status_code = 201
    create_response.text = "{}"
    create_response.raise_for_status = MagicMock()
    create_response.json.return_value = {"id": "10001", "key": "SOC-1"}

    get_response = MagicMock()
    get_response.status_code = 200
    get_response.text = "{}"
    get_response.raise_for_status = MagicMock()
    get_response.json.return_value = {
        "key": "SOC-1",
        "id": "10001",
        "fields": {
            "summary": "Suspicious login",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["soc-alert"],
            "priority": {"name": "Medium"},
            "resolution": None,
            "updated": "2026-01-01T00:00:00.000+0000",
        },
    }

    mock_request.side_effect = [create_response, get_response]
    issue = JiraClient().create_issue("Suspicious login", "Test description")
    assert issue.key == "SOC-1"
    assert issue.status == "To Do"


@patch("clients.jira_client.requests.request")
def test_close_issue_uses_done_transition(mock_request: MagicMock) -> None:
    transitions = MagicMock()
    transitions.status_code = 200
    transitions.text = "{}"
    transitions.raise_for_status = MagicMock()
    transitions.json.return_value = {
        "transitions": [
            {"id": "31", "name": "Done", "to": {"name": "Done", "statusCategory": {"key": "done"}}}
        ]
    }
    apply_response = MagicMock()
    apply_response.status_code = 204
    apply_response.text = "{}"
    apply_response.raise_for_status = MagicMock()
    mock_request.side_effect = [transitions, apply_response]
    assert JiraClient().close_issue("SOC-1") is True
