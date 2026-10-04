from __future__ import annotations

import re
import time
from typing import Any

import requests

from config import get_settings
from models.jira_models import JiraIssue, JiraTransition
from utils.logger import get_logger, safe_dump, truncate_text
from utils.retry import with_retry

log = get_logger("jira_client")


class JiraClient:
    def __init__(self) -> None:
        settings = get_settings().jira
        self.api_url = settings.api_url
        self.auth = (settings.email, settings.api_token)
        self.timeout = settings.request_timeout
        self.project_key = settings.project_key
        self.issue_type = settings.issue_type
        self.default_labels = list(settings.default_labels)
        self.closed_status_names = [
            name.lower() for name in get_settings().sync.closed_status_names
        ]

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Content-Type": "application/json"}

    @with_retry(exceptions=(requests.RequestException,))
    def _request(self, method: str, endpoint: str, **kwargs: Any) -> requests.Response:
        started_at = time.monotonic()
        url = f"{self.api_url}{endpoint}"
        request_payload = kwargs.get("json", kwargs.get("data"))
        request_params = kwargs.get("params")
        log.info(
            "Jira request | method=%s endpoint=%s params=%s payload=%s",
            method,
            endpoint,
            safe_dump(request_params),
            safe_dump(request_payload),
        )
        response = requests.request(
            method,
            url,
            headers=self._headers(),
            auth=self.auth,
            timeout=self.timeout,
            **kwargs,
        )
        elapsed = time.monotonic() - started_at
        log.info(
            "Jira response | method=%s endpoint=%s status=%s elapsed=%.2fs body=%s",
            method,
            endpoint,
            response.status_code,
            elapsed,
            truncate_text(response.text),
        )
        if response.status_code >= 400:
            log.error(
                "Jira API error: %s %s -> %d %s",
                method,
                endpoint,
                response.status_code,
                response.text[:500],
            )
        return response

    def create_issue(
        self,
        summary: str,
        description: str = "",
        labels: list[str] | None = None,
        priority: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> JiraIssue:
        fields: dict[str, Any] = {
            "project": {"key": self.project_key},
            "issuetype": {"name": self.issue_type},
            "summary": summary[:255],
            "description": self._adf(description[:10000]),
            "labels": list(dict.fromkeys((labels or []) + self.default_labels)),
        }
        if priority:
            fields["priority"] = {"name": priority}
        if extra_fields:
            fields.update(extra_fields)

        response = self._request("POST", "/issue", json={"fields": fields})
        response.raise_for_status()
        data = response.json()
        return self.get_issue(data["key"])

    def get_issue(self, issue_key: str) -> JiraIssue:
        response = self._request("GET", f"/issue/{issue_key}")
        response.raise_for_status()
        return JiraIssue.from_api_response(response.json())

    def update_issue(self, issue_key: str, fields: dict[str, Any]) -> None:
        response = self._request("PUT", f"/issue/{issue_key}", json={"fields": fields})
        response.raise_for_status()

    def assign_issue(self, issue_key: str, account_id: str) -> None:
        response = self._request(
            "PUT", f"/issue/{issue_key}/assignee", json={"accountId": account_id}
        )
        response.raise_for_status()

    def search_issues(
        self, jql: str, max_results: int = 50, fields: list[str] | None = None
    ) -> list[JiraIssue]:
        payload = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields
            or [
                "summary",
                "status",
                "assignee",
                "labels",
                "priority",
                "resolution",
                "description",
                "updated",
            ],
        }
        response = self._request("POST", "/search/jql", json=payload)
        response.raise_for_status()
        return [JiraIssue.from_api_response(item) for item in response.json().get("issues", [])]

    def find_issue_by_label(self, label: str) -> JiraIssue | None:
        safe_label = re.sub(r"[^a-zA-Z0-9_-]", "-", label).strip("-").lower()
        jql = f'project = "{self.project_key}" AND labels = "{safe_label}" ORDER BY created DESC'
        results = self.search_issues(jql, max_results=1)
        return results[0] if results else None

    def find_issue_by_correlation_label(self, correlation_key: str) -> JiraIssue | None:
        return self.find_issue_by_label(self._correlation_label(correlation_key))

    def get_transitions(self, issue_key: str) -> list[JiraTransition]:
        response = self._request("GET", f"/issue/{issue_key}/transitions")
        response.raise_for_status()
        transitions = []
        for item in response.json().get("transitions", []):
            target = item.get("to") or {}
            transitions.append(
                JiraTransition(
                    id=item["id"],
                    name=item["name"],
                    to_status=target.get("name", ""),
                    to_category=(target.get("statusCategory") or {}).get("key", ""),
                )
            )
        return transitions

    def transition_issue(self, issue_key: str, transition_id: str) -> None:
        response = self._request(
            "POST", f"/issue/{issue_key}/transitions", json={"transition": {"id": transition_id}}
        )
        response.raise_for_status()

    def close_issue(self, issue_key: str) -> bool:
        """Apply the first transition that leads to a closed state.

        Preference order: target status category ``done``, a configured closed
        status name, then a transition whose name mentions close/done/resolve.
        """
        transitions = self.get_transitions(issue_key)
        candidates = (
            [t for t in transitions if t.to_category == "done"]
            or [t for t in transitions if t.to_status.lower() in self.closed_status_names]
            or [
                t
                for t in transitions
                if any(k in t.name.lower() for k in ("close", "done", "resolve"))
            ]
        )
        if not candidates:
            log.warning("No closing transition found for %s", issue_key)
            return False
        self.transition_issue(issue_key, candidates[0].id)
        log.info("Closed %s with transition '%s'", issue_key, candidates[0].name)
        return True

    def add_comment(self, issue_key: str, text: str) -> None:
        response = self._request(
            "POST", f"/issue/{issue_key}/comment", json={"body": self._adf(text[:5000])}
        )
        response.raise_for_status()

    def get_fields(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/field")
        response.raise_for_status()
        return response.json()

    def test_connection(self) -> bool:
        try:
            response = self._request("GET", "/myself")
            return response.status_code == 200
        except Exception as exc:
            log.error("Jira connection error: %s", exc)
            return False

    @staticmethod
    def _adf(text: str) -> dict[str, Any]:
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text or "(empty)"}],
                }
            ],
        }

    @staticmethod
    def _correlation_label(correlation_key: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "-", correlation_key).strip("-").lower()
        return f"sync-{safe}"[:255]
