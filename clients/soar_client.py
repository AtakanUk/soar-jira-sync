from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

try:
    import resilient  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    resilient = None

from config import get_settings
from models.soar_models import SoarIncident
from utils.logger import get_logger, safe_dump

log = get_logger("soar_client")


class BaseSoarClient(ABC):
    @abstractmethod
    def get_incident(self, incident_id: str) -> SoarIncident | None:
        raise NotImplementedError

    @abstractmethod
    def get_open_incidents(self) -> list[SoarIncident]:
        raise NotImplementedError

    @abstractmethod
    def update_incident(self, incident_id: str, fields: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def close_incident(self, incident_id: str, reason: str = "") -> None:
        raise NotImplementedError

    @abstractmethod
    def add_comment(self, incident_id: str, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_jira_key(self, incident_id: str, jira_key: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def find_by_jira_key(self, jira_key: str) -> SoarIncident | None:
        raise NotImplementedError

    @abstractmethod
    def test_connection(self) -> bool:
        raise NotImplementedError


class IBMSoarClient(BaseSoarClient):
    def __init__(self) -> None:
        self.settings = get_settings().soar
        self._client = None
        self._connect()

    def _connect(self) -> None:
        if resilient is None:
            raise RuntimeError(
                "IBM SOAR SDK is not installed. Install the resilient package to enable SOAR support."
            )
        options = {
            "host": self.settings.host,
            "port": self.settings.port,
            "org": self.settings.org,
            "api_key_id": self.settings.api_key_id,
            "api_key_secret": self.settings.api_key_secret,
        }
        if self.settings.cafile:
            options["cafile"] = self.settings.cafile
        self._client = resilient.get_client(options)
        log.info(
            "IBM SOAR connected: %s:%s org=%s",
            self.settings.host,
            self.settings.port,
            self.settings.org,
        )

    def _call(self, method: str, uri: str, payload: dict[str, Any] | None = None) -> Any:
        started_at = time.monotonic()
        log.info("SOAR request | method=%s uri=%s payload=%s", method, uri, safe_dump(payload))
        try:
            if payload is None:
                result = getattr(self._client, method)(uri)
            else:
                result = getattr(self._client, method)(uri, payload)
        except Exception as exc:
            log.error("SOAR API error | method=%s uri=%s error=%s", method, uri, exc)
            raise
        elapsed = time.monotonic() - started_at
        log.info(
            "SOAR response | method=%s uri=%s elapsed=%.2fs result=%s",
            method,
            uri,
            elapsed,
            safe_dump(result),
        )
        return result

    def _extract_jira_key(self, properties: dict[str, Any]) -> str | None:
        for field_name in self.settings.jira_field_names:
            value = properties.get(field_name)
            if value:
                return str(value)
        return None

    def _parse_incident(self, data: dict[str, Any]) -> SoarIncident:
        severity_raw = data.get("severity_code")
        severity = severity_raw.get("name") if isinstance(severity_raw, dict) else severity_raw
        plan_status = data.get("plan_status", "A")
        status = (
            "Closed" if plan_status == "C" else "Active" if plan_status == "A" else str(plan_status)
        )
        description = data.get("description")
        if isinstance(description, dict):
            description = description.get("content", "")
        properties = data.get("properties") or {}
        return SoarIncident(
            incident_id=str(data.get("id", "")),
            title=data.get("name", ""),
            status=status,
            severity=str(severity) if severity is not None else None,
            source=str(data.get("incident_type_ids", [None])[0])
            if data.get("incident_type_ids")
            else None,
            description=description,
            owner=str(data.get("owner_id")) if data.get("owner_id") else None,
            alert_id=data.get("alert_id"),
            jira_key=self._extract_jira_key(properties),
            created_at=str(data.get("create_date", "")),
            updated_at=str(data.get("inc_last_modified_date", "")),
            raw=data,
        )

    def get_incident(self, incident_id: str) -> SoarIncident | None:
        try:
            data = self._call("get", f"/incidents/{incident_id}")
            return self._parse_incident(data)
        except Exception as exc:
            if "404" in str(exc):
                return None
            raise

    def get_open_incidents(self) -> list[SoarIncident]:
        query = {
            "filters": [
                {"conditions": [{"field_name": "plan_status", "method": "equals", "value": "A"}]}
            ],
            "sorts": [{"field_name": "create_date", "type": "desc"}],
        }
        results = self._call("post", "/incidents/query?return_level=normal", query)
        items = results if isinstance(results, list) else results.get("data", [])
        return [self._parse_incident(item) for item in items]

    def update_incident(self, incident_id: str, fields: dict[str, Any]) -> None:
        changes = []
        for field_name, new_value in fields.items():
            changes.append(
                {
                    "field": {"name": field_name},
                    "old_value": {"text": ""},
                    "new_value": {
                        "text": str(new_value) if not isinstance(new_value, dict) else new_value
                    },
                }
            )
        self._call("patch", f"/incidents/{incident_id}", {"changes": changes})

    def close_incident(self, incident_id: str, reason: str = "") -> None:
        payload = {
            "changes": [
                {
                    "field": {"name": "plan_status"},
                    "old_value": {"text": "A"},
                    "new_value": {"text": "C"},
                }
            ]
        }
        if reason:
            payload["changes"].append(
                {
                    "field": {"name": "resolution_summary"},
                    "old_value": {"textarea": {"content": "", "format": "text"}},
                    "new_value": {"textarea": {"content": reason, "format": "text"}},
                }
            )
        self._call("patch", f"/incidents/{incident_id}", payload)

    def add_comment(self, incident_id: str, text: str) -> None:
        self._call(
            "post",
            f"/incidents/{incident_id}/comments",
            {"text": {"content": text, "format": "text"}},
        )

    def set_jira_key(self, incident_id: str, jira_key: str) -> None:
        primary_field = (
            self.settings.jira_field_names[0] if self.settings.jira_field_names else "jira_key"
        )
        try:
            self.update_incident(incident_id, {f"properties.{primary_field}": jira_key})
        except Exception:
            self.add_comment(incident_id, f"Linked Jira issue: {jira_key}")

    def find_by_jira_key(self, jira_key: str) -> SoarIncident | None:
        for field_name in self.settings.jira_field_names:
            query = {
                "filters": [
                    {
                        "conditions": [
                            {"field_name": "plan_status", "method": "equals", "value": "A"},
                            {
                                "field_name": f"properties.{field_name}",
                                "method": "equals",
                                "value": jira_key,
                            },
                        ]
                    }
                ],
            }
            try:
                results = self._call("post", "/incidents/query?return_level=normal", query)
                items = results if isinstance(results, list) else results.get("data", [])
                if items:
                    return self._parse_incident(items[0])
            except Exception as exc:
                log.warning("SOAR lookup failed for field %s: %s", field_name, exc)
        return None

    def test_connection(self) -> bool:
        try:
            if not self._client:
                self._connect()
            self._call("get", "/incidents?return_level=partial&length=1")
            return True
        except Exception as exc:
            log.error("SOAR connection error: %s", exc)
            return False
