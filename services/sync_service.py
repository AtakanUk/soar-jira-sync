"""Synchronization engine.

Orchestrates every flow between IBM SOAR, Jira Cloud and the notification mailbox:

1. New alert from SOAR (webhook or polling) -> create a Jira issue, or comment on
   the existing one if the alert is a duplicate.
2. Jira polling -> mirror status changes back to SOAR.
3. Jira issue closed -> close the SOAR incident.
4. Close request from SOAR -> transition the Jira issue to a done status.
5. Mailbox polling -> new SOC notifications, status replies and closure signals.

Jira is the master system: when both sides change, the Jira state wins. Loops are
avoided by remembering which side made the last change (``last_sync_source``).
"""

from __future__ import annotations

import hashlib
from typing import Any

from clients.jira_client import JiraClient
from clients.mail_client import MailClient, ParsedEmail
from clients.soar_client import BaseSoarClient
from config import get_settings
from models.jira_models import JiraIssue
from models.soar_models import SoarIncident
from models.sync_models import MappingRecord, SyncSource, SyncState
from repositories.mapping_repository import MappingRepository
from services.mapping_service import MappingService
from services.parser_service import normalize_alarm, sanitize_label
from services.transition_service import TransitionService
from utils.correlation import extract_correlation_from_payload, generate_correlation_key
from utils.logger import get_logger, safe_dump, truncate_text

log = get_logger("sync_engine")

CLOSE_EVENT_TYPES = {"close", "closed", "resolve", "resolved"}
SEVERITY_TO_PRIORITY = {
    "critical": "Highest",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "info": "Lowest",
    "informational": "Lowest",
}


class SyncService:
    def __init__(
        self,
        jira: JiraClient,
        soar: BaseSoarClient | None,
        mail: MailClient | None,
        repo: MappingRepository,
    ) -> None:
        self.settings = get_settings()
        self.jira = jira
        self.soar = soar
        self.mail = mail
        self.repo = repo
        self.mapping_service = MappingService(repo)
        self.transition_service = TransitionService(jira)

    # ------------------------------------------------------------------ fingerprints
    # Fingerprints let the seen-cache skip entities that have not changed since the
    # previous poll, which keeps API usage low on busy instances.

    @staticmethod
    def _hash_text(parts: list[str]) -> str:
        raw = "|".join(part or "" for part in parts)
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]

    def _jira_fingerprint(self, issue: JiraIssue) -> str:
        return self._hash_text(
            [issue.key, issue.status, issue.updated or "", ",".join(sorted(issue.labels or []))]
        )

    def _soar_fingerprint(self, incident: SoarIncident) -> str:
        return self._hash_text(
            [
                incident.incident_id,
                incident.status,
                incident.severity or "",
                incident.updated_at or "",
                incident.jira_key or "",
            ]
        )

    def _mail_fingerprint(self, parsed: ParsedEmail) -> str:
        return self._hash_text(
            [
                parsed.message_id,
                parsed.uid,
                parsed.subject,
                parsed.date,
                parsed.offense_id or "",
                parsed.response_status or "",
                str(parsed.is_closure),
            ]
        )

    # ------------------------------------------------------------------ flow 1: new alert

    def handle_new_alarm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a Jira issue for a new alert, or comment on the existing one.

        Returns ``{"action": "created" | "duplicate" | ..., "jira_key": ...}``.
        """
        log.info("New alert payload=%s", safe_dump(payload))
        alarm = normalize_alarm(payload)
        correlation_key = extract_correlation_from_payload(payload)
        log.info(
            "Processing alert | corr=%s incident=%s alert=%s type=%s",
            correlation_key,
            alarm["incident_id"],
            alarm["alert_id"],
            alarm["event_type"],
        )

        if alarm["event_type"] in CLOSE_EVENT_TYPES:
            return self._handle_soar_close_request(correlation_key)

        mapping, created = self.mapping_service.find_or_create(
            correlation_key=correlation_key,
            soar_incident_id=alarm["incident_id"] or None,
            source_alarm_id=alarm["alert_id"] or None,
            source=SyncSource.SOAR,
        )

        if not created and mapping.jira_issue_key:
            log.info("Duplicate alert for existing issue %s", mapping.jira_issue_key)
            note = (
                f"Repeated alert received (correlation: {correlation_key})\n"
                f"Severity: {alarm['severity']}\n"
                f"{alarm['description'][:2000]}"
            )
            try:
                self.jira.add_comment(mapping.jira_issue_key, note)
            except Exception as exc:
                log.warning(
                    "Could not comment on duplicate issue %s: %s", mapping.jira_issue_key, exc
                )
            return {
                "action": "duplicate",
                "jira_key": mapping.jira_issue_key,
                "mapping_id": mapping.id,
            }

        try:
            labels = [self.jira._correlation_label(correlation_key)]
            labels.extend(sanitize_label(tag) for tag in alarm["tags"])
            description = (
                f"Source: {alarm['source']}\n"
                f"Severity: {alarm['severity']}\n"
                f"Incident ID: {alarm['incident_id'] or 'N/A'}\n"
                f"Alert ID: {alarm['alert_id'] or 'N/A'}\n"
                f"Correlation: {correlation_key}\n\n"
                f"{alarm['description']}"
            )
            issue = self.jira.create_issue(
                summary=alarm["summary"],
                description=description,
                labels=labels,
                priority=self._map_severity_to_priority(alarm["severity"]),
            )

            mapping.jira_issue_key = issue.key
            mapping.jira_issue_id = issue.id
            mapping.last_jira_status = issue.status
            self.mapping_service.mark_synced(mapping, SyncSource.SOAR, jira_status=issue.status)

            if self.soar and alarm["incident_id"]:
                try:
                    self.soar.set_jira_key(alarm["incident_id"], issue.key)
                except Exception as exc:
                    log.warning("Could not write Jira key %s back to SOAR: %s", issue.key, exc)

            log.info(
                "Created %s for corr=%s summary=%s",
                issue.key,
                correlation_key,
                truncate_text(issue.summary, 200),
            )
            return {"action": "created", "jira_key": issue.key, "mapping_id": mapping.id}
        except Exception as exc:
            self.mapping_service.mark_error(mapping, str(exc))
            raise

    # ------------------------------------------------------------------ flow 2: Jira polling

    def poll_jira(self) -> dict[str, int]:
        """Check recently updated Jira issues and mirror status changes to SOAR."""
        stats = {"checked": 0, "updated": 0, "closed": 0, "errors": 0, "skipped_cached": 0}
        jql = (
            self.settings.polling.jira_jql_filter
            or f'project = "{self.jira.project_key}" ORDER BY updated DESC'
        )

        try:
            issues = self.jira.search_issues(jql, max_results=self.settings.polling.max_results)
            log.info("Jira poll: %d issues", len(issues))
        except Exception as exc:
            log.error("Jira poll failed: %s", exc)
            return stats

        for issue in issues:
            stats["checked"] += 1
            try:
                if self.settings.cache.enabled:
                    fingerprint = self._jira_fingerprint(issue)
                    if self.repo.is_seen("jira_issue", issue.key, fingerprint):
                        stats["skipped_cached"] += 1
                        continue
                    self.repo.remember_seen(
                        "jira_issue", issue.key, fingerprint, len(safe_dump(issue.raw or {}))
                    )
                self._sync_jira_issue(issue, stats)
            except Exception as exc:
                stats["errors"] += 1
                log.error("Error syncing issue %s: %s", issue.key, exc)

        log.info(
            "Jira poll done: checked=%d updated=%d closed=%d errors=%d skipped_cached=%d",
            stats["checked"],
            stats["updated"],
            stats["closed"],
            stats["errors"],
            stats["skipped_cached"],
        )
        return stats

    def _sync_jira_issue(self, issue: JiraIssue, stats: dict[str, int]) -> None:
        mapping = self.repo.find_by_jira_key(issue.key)
        if not mapping:
            # Issues created before the mapping row was written can still be matched by label.
            for label in issue.labels:
                if label.startswith("sync-"):
                    mapping = self.repo.find_by_correlation_key(label.replace("sync-", "", 1))
                    if mapping:
                        break
        if not mapping:
            log.debug("Skipping %s: not managed by the sync engine", issue.key)
            return

        if mapping.last_jira_status == issue.status:
            return

        log.info(
            "Issue %s status changed: %s -> %s", issue.key, mapping.last_jira_status, issue.status
        )

        # Loop protection: this change is the echo of our own SOAR -> Jira update.
        if (
            mapping.last_sync_source == SyncSource.SOAR
            and mapping.current_sync_state == SyncState.SYNCED
        ):
            mapping.last_jira_status = issue.status
            self.repo.update(mapping)
            return

        if issue.is_closed:
            self._handle_jira_closed(mapping, issue, stats)
            return

        if self.soar and mapping.soar_incident_id:
            soar_status = self.settings.sync.status_mapping.get(issue.status)
            if soar_status:
                try:
                    self.soar.update_incident(mapping.soar_incident_id, {"status": soar_status})
                    stats["updated"] += 1
                except Exception as exc:
                    log.error(
                        "Failed to update SOAR incident %s: %s", mapping.soar_incident_id, exc
                    )

        self.mapping_service.mark_synced(mapping, SyncSource.JIRA, jira_status=issue.status)

    # ------------------------------------------------------------------ flow 3: Jira closed

    def _handle_jira_closed(
        self, mapping: MappingRecord, issue: JiraIssue, stats: dict[str, int]
    ) -> None:
        log.info("Issue %s closed, closing SOAR incident %s", issue.key, mapping.soar_incident_id)
        if self.soar and mapping.soar_incident_id:
            try:
                self.soar.close_incident(
                    mapping.soar_incident_id, reason=f"Jira issue {issue.key} was closed"
                )
            except Exception as exc:
                log.error("Failed to close SOAR incident %s: %s", mapping.soar_incident_id, exc)
                self.mapping_service.mark_error(mapping, str(exc))
                stats["errors"] += 1
                return

        self.mapping_service.mark_closed(mapping, SyncSource.JIRA)
        stats["closed"] += 1

    # ------------------------------------------------------------------ flow 4: close request from SOAR

    def _handle_soar_close_request(self, correlation_key: str) -> dict[str, Any]:
        mapping = self.repo.find_by_correlation_key(correlation_key)
        if not mapping or not mapping.jira_issue_key:
            log.warning("Close request without a mapping: %s", correlation_key)
            return {"action": "no_mapping", "correlation_key": correlation_key}
        result = self._close_jira_for(mapping)
        result.setdefault("jira_key", mapping.jira_issue_key)
        return result

    def handle_soar_close(self, soar_incident_id: str) -> dict[str, Any]:
        """Close request addressed by SOAR incident ID (``POST /api/close``)."""
        mapping = self.repo.find_by_soar_id(soar_incident_id)
        if not mapping or not mapping.jira_issue_key:
            log.warning("Close request without a mapping for SOAR incident %s", soar_incident_id)
            return {"action": "no_mapping", "soar_id": soar_incident_id}
        return self._close_jira_for(mapping)

    def _close_jira_for(self, mapping: MappingRecord) -> dict[str, Any]:
        issue_key = mapping.jira_issue_key
        log.info("Closing Jira issue %s on SOAR request", issue_key)
        try:
            if self.transition_service.close_issue(issue_key):
                self.mapping_service.mark_closed(mapping, SyncSource.SOAR)
                return {"action": "closed", "jira_key": issue_key}
            log.warning("No closing transition available for %s", issue_key)
            return {"action": "no_transition", "jira_key": issue_key}
        except Exception as exc:
            self.mapping_service.mark_error(mapping, str(exc))
            return {"action": "error", "jira_key": issue_key, "error": str(exc)}

    # ------------------------------------------------------------------ flow 5: mailbox

    def poll_mail(self) -> dict[str, int]:
        """Process unread notification mails: new incidents, status replies and closures."""
        stats = {
            "processed": 0,
            "new_issues": 0,
            "updates": 0,
            "closures": 0,
            "errors": 0,
            "skipped_cached": 0,
        }
        if not self.mail or not self.mail.host:
            return stats

        try:
            self.mail.connect()
            emails = self.mail.fetch_unread()
            log.info("Mail poll: %d unread messages", len(emails))
        except Exception as exc:
            log.error("Mail fetch failed: %s", exc)
            self.mail.disconnect()
            return stats

        for parsed in emails:
            stats["processed"] += 1
            try:
                if self.settings.cache.enabled:
                    mail_key = parsed.message_id or f"uid:{parsed.uid}"
                    fingerprint = self._mail_fingerprint(parsed)
                    if self.repo.is_seen("email", mail_key, fingerprint):
                        stats["skipped_cached"] += 1
                        self.mail.mark_processed(parsed.uid)
                        continue
                    self.repo.remember_seen("email", mail_key, fingerprint, len(parsed.body or ""))

                if parsed.is_closure:
                    self._handle_mail_closure(parsed, stats)
                elif parsed.is_status_update:
                    self._handle_mail_status_update(parsed, stats)
                elif parsed.is_new_incident:
                    self._handle_mail_new_incident(parsed, stats)
                else:
                    log.debug("No action for mail: %s", parsed.subject)
                self.mail.mark_processed(parsed.uid)
            except Exception as exc:
                stats["errors"] += 1
                log.error("Error processing mail %s: %s", parsed.message_id, exc)

        self.mail.disconnect()
        log.info(
            "Mail poll done: processed=%d new=%d updates=%d closures=%d errors=%d skipped_cached=%d",
            stats["processed"],
            stats["new_issues"],
            stats["updates"],
            stats["closures"],
            stats["errors"],
            stats["skipped_cached"],
        )
        return stats

    def _handle_mail_new_incident(self, parsed: ParsedEmail, stats: dict[str, int]) -> None:
        """A SOC notification mail ("<Severity> Severity Incident #<id>") becomes a Jira issue."""
        log.info(
            "Mail notification | offense_id=%s sender=%s subject=%s",
            parsed.offense_id,
            truncate_text(parsed.sender, 160),
            truncate_text(parsed.subject, 250),
        )
        if not parsed.offense_id:
            log.warning("Notification mail without an offense ID: %s", parsed.subject)
            return

        correlation_key = generate_correlation_key(
            soar_incident_id=parsed.offense_id, email_message_id=parsed.message_id
        )
        mapping, created = self.mapping_service.find_or_create(
            correlation_key=correlation_key,
            soar_incident_id=parsed.offense_id,
            source_alarm_id=None,
            source=SyncSource.EMAIL,
        )

        if not created and mapping.jira_issue_key:
            log.info(
                "Duplicate notification for %s (offense #%s)",
                mapping.jira_issue_key,
                parsed.offense_id,
            )
            try:
                self.jira.add_comment(
                    mapping.jira_issue_key,
                    f"Repeated SOC notification received by mail: {parsed.subject}",
                )
            except Exception as exc:
                log.warning(
                    "Could not comment on duplicate issue %s: %s", mapping.jira_issue_key, exc
                )
            return

        summary = f"SOC #{parsed.offense_id}: {parsed.offense_name or parsed.subject}"
        if len(summary) > 250:
            summary = summary[:247] + "..."

        description = "\n".join(
            [
                f"Offense ID: {parsed.offense_id}",
                f"Severity: {parsed.severity or 'N/A'}",
                f"Offense Name: {parsed.offense_name or 'N/A'}",
                f"Offense Category: {parsed.offense_category or 'N/A'}",
                f"Offense Source: {parsed.offense_source or 'N/A'}",
                f"Related Log Sources: {parsed.related_log_sources or 'N/A'}",
                f"Triggered Rules: {parsed.triggered_rules or 'N/A'}",
                f"False-Positive: {parsed.false_positive or 'N/A'}",
                f"Action Need: {parsed.action_need or 'N/A'}",
                "",
                "-- Analyst Description --",
                parsed.analyst_description or "N/A",
                "",
                "-- Analyst Suggestion --",
                parsed.analyst_suggestion or "N/A",
                "",
                f"Source: Mail ({parsed.sender})",
                f"Date: {parsed.date}",
            ]
        )

        try:
            issue = self.jira.create_issue(
                summary=summary,
                description=description,
                labels=[
                    self.jira._correlation_label(correlation_key),
                    sanitize_label(f"soc-{parsed.severity or 'unknown'}"),
                ],
                priority=self._map_severity_to_priority(parsed.severity or "medium"),
            )
            mapping.jira_issue_key = issue.key
            mapping.jira_issue_id = issue.id
            mapping.last_jira_status = issue.status
            mapping.email_message_id = parsed.message_id
            self.mapping_service.mark_synced(mapping, SyncSource.EMAIL, jira_status=issue.status)

            if self.soar:
                try:
                    self.soar.set_jira_key(parsed.offense_id, issue.key)
                except Exception as exc:
                    log.warning("Could not write Jira key %s back to SOAR: %s", issue.key, exc)

            log.info("Created %s from mail (offense #%s)", issue.key, parsed.offense_id)
            stats["new_issues"] += 1
        except Exception as exc:
            self.mapping_service.mark_error(mapping, str(exc))
            raise

    def _handle_mail_status_update(self, parsed: ParsedEmail, stats: dict[str, int]) -> None:
        """A reply such as ``Status: Known Offense`` / ``Reason: ...`` is added to the issue as a comment."""
        mapping: MappingRecord | None = None
        if parsed.offense_id:
            mapping = self.repo.find_by_correlation_key(
                generate_correlation_key(soar_incident_id=parsed.offense_id)
            )
        if not mapping and parsed.jira_key:
            mapping = self.repo.find_by_jira_key(parsed.jira_key)
        if not mapping or not mapping.jira_issue_key:
            log.warning(
                "Status reply without a mapping | subject=%s offense_id=%s",
                parsed.subject,
                parsed.offense_id,
            )
            return

        lines = [
            f"Mail status update ({parsed.sender}):",
            f"Status: {parsed.response_status or 'N/A'}",
        ]
        if parsed.response_reason:
            lines.append(f"Reason: {parsed.response_reason}")
        if parsed.responder:
            lines.append(f"Responder: {parsed.responder}")

        try:
            self.jira.add_comment(mapping.jira_issue_key, "\n".join(lines))
            self.mapping_service.mark_synced(mapping, SyncSource.EMAIL)
            log.info("Status reply added to %s: %s", mapping.jira_issue_key, parsed.response_status)
            stats["updates"] += 1
        except Exception as exc:
            log.error("Could not add status reply to %s: %s", mapping.jira_issue_key, exc)
            stats["errors"] += 1

    def _handle_mail_closure(self, parsed: ParsedEmail, stats: dict[str, int]) -> None:
        """A closure mail closes both sides; each side is attempted independently."""
        mapping: MappingRecord | None = None
        if parsed.jira_key:
            mapping = self.repo.find_by_jira_key(parsed.jira_key)
        if not mapping and parsed.incident_id:
            mapping = self.repo.find_by_soar_id(parsed.incident_id)
        if not mapping and parsed.message_id:
            mapping = self.repo.find_by_correlation_key(
                generate_correlation_key(email_message_id=parsed.message_id)
            )
        if not mapping and parsed.jira_key:
            mapping = self._create_mapping_from_jira(parsed)
        if not mapping:
            log.warning(
                "Closure mail without a mapping | subject=%s jira_key=%s incident_id=%s",
                parsed.subject,
                parsed.jira_key,
                parsed.incident_id,
            )
            return

        log.info(
            "Closure mail for jira=%s soar=%s", mapping.jira_issue_key, mapping.soar_incident_id
        )
        if mapping.jira_issue_key:
            try:
                self.transition_service.close_issue(mapping.jira_issue_key)
            except Exception as exc:
                log.error(
                    "Closure mail: closing Jira issue %s failed: %s", mapping.jira_issue_key, exc
                )

        if self.soar and mapping.soar_incident_id:
            try:
                self.soar.close_incident(
                    mapping.soar_incident_id, reason=f"Closed by mail: {parsed.subject[:100]}"
                )
            except Exception as exc:
                log.error(
                    "Closure mail: closing SOAR incident %s failed: %s",
                    mapping.soar_incident_id,
                    exc,
                )

        self.mapping_service.mark_closed(mapping, SyncSource.EMAIL)
        stats["closures"] += 1

    def _create_mapping_from_jira(self, parsed: ParsedEmail) -> MappingRecord | None:
        """No mapping exists yet but the mail references a Jira key: link it now."""
        try:
            issue = self.jira.get_issue(parsed.jira_key)
        except Exception as exc:
            log.error(
                "Jira issue %s referenced by mail could not be loaded: %s", parsed.jira_key, exc
            )
            return None

        correlation_key = generate_correlation_key(
            soar_incident_id=parsed.incident_id, email_message_id=parsed.message_id
        )
        mapping, _ = self.mapping_service.find_or_create(
            correlation_key=correlation_key,
            soar_incident_id=parsed.incident_id or None,
            source_alarm_id=None,
            source=SyncSource.EMAIL,
        )
        mapping.jira_issue_key = issue.key
        mapping.jira_issue_id = issue.id
        mapping.last_jira_status = issue.status
        mapping.email_message_id = parsed.message_id
        self.repo.update(mapping)
        log.info("Linked mail to existing issue %s (corr=%s)", issue.key, correlation_key)

        if self.soar and parsed.incident_id:
            try:
                self.soar.set_jira_key(parsed.incident_id, issue.key)
            except Exception as exc:
                log.warning("Could not write Jira key %s back to SOAR: %s", issue.key, exc)
        return mapping

    # ------------------------------------------------------------------ SOAR polling

    def poll_soar(self) -> dict[str, int]:
        """Open a Jira issue for every open SOAR incident that has no mapping yet."""
        stats = {"checked": 0, "new_issues": 0, "closed": 0, "errors": 0, "skipped_cached": 0}
        if not self.soar:
            return stats

        try:
            incidents = self.soar.get_open_incidents()
            log.info("SOAR poll: %d open incidents", len(incidents))
        except Exception as exc:
            log.error("SOAR poll failed: %s", exc)
            return stats

        for incident in incidents:
            stats["checked"] += 1
            try:
                if self.settings.cache.enabled:
                    fingerprint = self._soar_fingerprint(incident)
                    if self.repo.is_seen("soar_incident", incident.incident_id, fingerprint):
                        stats["skipped_cached"] += 1
                        continue
                    self.repo.remember_seen(
                        "soar_incident",
                        incident.incident_id,
                        fingerprint,
                        len(safe_dump(incident.raw or {})),
                    )
                self._sync_soar_incident(incident, stats)
            except Exception as exc:
                stats["errors"] += 1
                log.error("Error syncing SOAR incident %s: %s", incident.incident_id, exc)

        if self.settings.cache.enabled:
            self.repo.prune_seen_cache()
        return stats

    def _sync_soar_incident(self, incident: SoarIncident, stats: dict[str, int]) -> None:
        mapping = self.repo.find_by_correlation_key(
            generate_correlation_key(soar_incident_id=incident.incident_id)
        )
        if mapping:
            return
        result = self.handle_new_alarm(
            {
                "incident_id": incident.incident_id,
                "summary": incident.title,
                "description": incident.description or "",
                "severity": incident.severity or "medium",
                "source": incident.source or "soar",
            }
        )
        if result["action"] == "created":
            stats["new_issues"] += 1

    # ------------------------------------------------------------------ retry queue

    def retry_errors(self, max_retry_count: int = 5) -> dict[str, int]:
        """Re-check mappings in ERROR state; give up after ``max_retry_count`` attempts."""
        stats = {"retried": 0, "resolved": 0, "given_up": 0}
        for mapping in self.repo.find_errors():
            if mapping.retry_count >= max_retry_count:
                stats["given_up"] += 1
                log.warning(
                    "Giving up on %s after %d retries", mapping.correlation_key, mapping.retry_count
                )
                continue

            stats["retried"] += 1
            try:
                if mapping.jira_issue_key:
                    issue = self.jira.get_issue(mapping.jira_issue_key)
                    self.mapping_service.mark_synced(
                        mapping, SyncSource.SYSTEM, jira_status=issue.status
                    )
                    stats["resolved"] += 1
                else:
                    log.warning(
                        "Mapping %s is in error state without a Jira key", mapping.correlation_key
                    )
            except Exception as exc:
                self.mapping_service.mark_error(mapping, str(exc))
        return stats

    # ------------------------------------------------------------------ poll cycle

    def run_poll_cycle(self) -> dict[str, Any]:
        results: dict[str, Any] = {"jira": self.poll_jira()}
        if self.settings.polling.soar_poll_enabled:
            results["soar"] = self.poll_soar()
        if self.settings.polling.mail_poll_enabled:
            results["mail"] = self.poll_mail()
        results["retry"] = self.retry_errors()
        return results

    @staticmethod
    def _map_severity_to_priority(severity: str) -> str | None:
        return SEVERITY_TO_PRIORITY.get((severity or "").lower())
