"""IMAP client that reads and parses SOC notification mails.

Notification format::

    Subject: Critical Severity Incident #15730
    Body:    field label on one line, value on the next one
             (Offense ID, Offense Name, Offense Category, Offense Source, ...)

Replies from the customer team carry inline fields::

    Status: Known Offense
    Reason: Planned maintenance on the ISP link
    Responder: Jane Doe
"""

from __future__ import annotations

import contextlib
import email
import imaplib
import re
from dataclasses import dataclass
from email.header import decode_header

import requests

from config import get_settings
from utils.logger import get_logger, safe_dump, truncate_text

log = get_logger("mail_client")

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
IMAP_SCOPE = "offline_access https://outlook.office365.com/IMAP.AccessAsUser.All"


@dataclass
class ParsedEmail:
    message_id: str
    subject: str
    sender: str
    body: str
    date: str
    uid: str

    # notification fields
    offense_id: str | None = None
    severity: str | None = None
    offense_name: str | None = None
    offense_category: str | None = None
    offense_source: str | None = None
    related_log_sources: str | None = None
    triggered_rules: str | None = None
    false_positive: str | None = None
    action_need: str | None = None
    analyst_description: str | None = None
    analyst_suggestion: str | None = None

    # reply fields
    response_status: str | None = None
    response_reason: str | None = None
    responder: str | None = None

    incident_id: str | None = None
    jira_key: str | None = None

    is_new_incident: bool = False
    is_status_update: bool = False
    is_closure: bool = False


class MailClient:
    def __init__(self) -> None:
        self.settings = get_settings().mail
        self._conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    @property
    def host(self) -> str:
        return self.settings.imap_host

    # ------------------------------------------------------------------ connection

    def _access_token(self) -> str:
        response = requests.post(
            TOKEN_URL.format(tenant=self.settings.oauth_tenant_id),
            data={
                "client_id": self.settings.oauth_client_id,
                "client_secret": self.settings.oauth_client_secret,
                "refresh_token": self.settings.oauth_refresh_token,
                "grant_type": "refresh_token",
                "scope": IMAP_SCOPE,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def connect(self) -> None:
        if not self.settings.configured:
            log.warning("Mail settings are incomplete, skipping mailbox connection")
            return
        try:
            if self.settings.use_ssl:
                self._conn = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port)
            else:
                self._conn = imaplib.IMAP4(self.settings.imap_host, self.settings.imap_port)

            if self.settings.auth_method == "oauth2":
                auth_string = (
                    f"user={self.settings.username}\x01auth=Bearer {self._access_token()}\x01\x01"
                )
                self._conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
            else:
                self._conn.login(self.settings.username, self.settings.password)
            log.info("Connected to mailbox %s", self.settings.username)
        except Exception as exc:
            log.error("Mailbox connection failed: %s", exc)
            self._conn = None
            raise

    def disconnect(self) -> None:
        if self._conn is None:
            return
        with contextlib.suppress(Exception):
            self._conn.logout()
        self._conn = None

    def test_connection(self) -> bool:
        if not self.host:
            log.warning("Mail host is not configured")
            return False
        try:
            self.connect()
            return self._conn is not None
        except Exception:
            return False
        finally:
            self.disconnect()

    # ------------------------------------------------------------------ reading

    def fetch_unread(self) -> list[ParsedEmail]:
        if self._conn is None:
            log.warning("Mailbox not connected, skipping fetch")
            return []
        try:
            self._conn.select(self.settings.folder)
            _, data = self._conn.uid("search", None, "UNSEEN")
            uids = data[0].split() if data and data[0] else []
        except Exception as exc:
            log.error("Mailbox search failed: %s", exc)
            return []

        if uids:
            log.info("Unread UIDs: %s", safe_dump([uid.decode(errors="ignore") for uid in uids]))
        messages: list[ParsedEmail] = []
        for uid in uids:
            try:
                parsed = self._fetch_one(uid.decode())
                if parsed:
                    messages.append(parsed)
            except Exception as exc:
                log.error("Could not parse mail UID %s: %s", uid, exc)
        return messages

    def _fetch_one(self, uid: str) -> ParsedEmail | None:
        _, data = self._conn.uid("fetch", uid, "(RFC822)")  # type: ignore[union-attr]
        if not data or not data[0]:
            return None
        msg = email.message_from_bytes(data[0][1])
        parsed = ParsedEmail(
            message_id=msg.get("Message-ID", ""),
            subject=self._decode_header(msg.get("Subject", "")),
            sender=self._decode_header(msg.get("From", "")),
            body=self._extract_body(msg),
            date=msg.get("Date", ""),
            uid=uid,
        )
        self.parse_notification(parsed)
        log.info(
            "Mail parsed | uid=%s subject=%s offense_id=%s jira_key=%s new=%s update=%s closure=%s",
            uid,
            truncate_text(parsed.subject, 200),
            parsed.offense_id,
            parsed.jira_key,
            parsed.is_new_incident,
            parsed.is_status_update,
            parsed.is_closure,
        )
        return parsed

    def mark_processed(self, uid: str) -> None:
        """Flag the message as seen and move it to the processed folder."""
        if self._conn is None:
            return
        try:
            self._conn.uid("store", uid, "+FLAGS", "\\Seen")
            if self.settings.processed_folder:
                self._conn.uid("copy", uid, self.settings.processed_folder)
                self._conn.uid("store", uid, "+FLAGS", "\\Deleted")
                self._conn.expunge()
        except Exception as exc:
            log.warning("Could not mark mail %s as processed: %s", uid, exc)

    # ------------------------------------------------------------------ parsing

    def parse_notification(self, parsed: ParsedEmail) -> None:
        subject_match = re.search(
            r"(\w+)\s+Severity\s+Incident\s+#(\d+)", parsed.subject, re.IGNORECASE
        )
        if subject_match:
            parsed.severity = subject_match.group(1)
            parsed.offense_id = subject_match.group(2)
        if not parsed.offense_id:
            parsed.offense_id = self._extract_field(parsed.body, "Offense ID")
        parsed.incident_id = parsed.offense_id

        parsed.offense_name = self._extract_field(parsed.body, "Offense Name")
        parsed.offense_category = self._extract_field(parsed.body, "Offense Category")
        parsed.offense_source = self._extract_field(parsed.body, "Offense Source")
        parsed.related_log_sources = self._extract_field(parsed.body, "Related Log Sources")
        parsed.triggered_rules = self._extract_field(parsed.body, "Triggered Rules")
        parsed.false_positive = self._extract_field(parsed.body, "False-Positive")
        parsed.action_need = self._extract_field(parsed.body, "Action Need")
        parsed.analyst_description = self._extract_field(parsed.body, "Analyst Description")
        parsed.analyst_suggestion = self._extract_field(parsed.body, "Analyst Suggestion")
        if not parsed.severity:
            parsed.severity = self._extract_field(parsed.body, "Severity")

        parsed.response_status = self._extract_inline_field(parsed.body, "Status")
        parsed.response_reason = self._extract_inline_field(parsed.body, "Reason")
        parsed.responder = self._extract_inline_field(parsed.body, "Responder")

        jira_match = re.search(self.settings.jira_key_pattern, f"{parsed.subject}\n{parsed.body}")
        if jira_match:
            parsed.jira_key = jira_match.group(0)

        self._classify(parsed)

    def _classify(self, parsed: ParsedEmail) -> None:
        text = f"{parsed.subject}\n{parsed.body}".lower()
        parsed.is_closure = any(
            keyword.lower() in text for keyword in self.settings.closure_keywords
        )
        parsed.is_status_update = bool(parsed.response_status)

        if parsed.offense_id and not parsed.is_status_update:
            senders = [sender.lower() for sender in self.settings.notification_senders]
            # Without a sender allow-list every mail carrying an offense ID counts as a notification.
            parsed.is_new_incident = not senders or any(
                sender in parsed.sender.lower() for sender in senders
            )

        # A forwarded notification that already contains a reply is a status update.
        if parsed.offense_id and parsed.response_status:
            parsed.is_new_incident = False
            parsed.is_status_update = True

    @staticmethod
    def _extract_field(body: str, field_name: str) -> str | None:
        """Value of a ``label`` line followed by a ``value`` line (table rendered as plain text)."""
        match = re.search(
            rf"(?:^|\n)\s*{re.escape(field_name)}\s*\n+\s*(.+?)(?:\n|$)", body, re.IGNORECASE
        )
        if not match:
            return None
        value = match.group(1).strip()
        return value if value and value.lower() != field_name.lower() else None

    @staticmethod
    def _extract_inline_field(body: str, field_name: str) -> str | None:
        """Value of a ``Label: value`` line."""
        match = re.search(
            rf"(?:^|\n)\s*{re.escape(field_name)}\s*:\s*(.+?)(?:\n|$)", body, re.IGNORECASE
        )
        if not match:
            return None
        return match.group(1).strip() or None

    @staticmethod
    def _decode_header(value: str) -> str:
        decoded = []
        for part, charset in decode_header(value):
            decoded.append(
                part.decode(charset or "utf-8", errors="replace")
                if isinstance(part, bytes)
                else part
            )
        return " ".join(decoded)

    @staticmethod
    def _extract_body(message: email.message.Message) -> str:
        """Plain-text body; for multipart mails the first text/plain part."""
        if not message.is_multipart():
            payload = message.get_payload(decode=True)
            return (
                payload.decode(message.get_content_charset() or "utf-8", errors="replace")
                if payload
                else ""
            )
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
