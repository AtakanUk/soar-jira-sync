"""SOAR <-> Jira sync engine entry point.

Runs the polling loop and/or the webhook API depending on ``APP_MODE``
(``both`` | ``poll-only`` | ``api-only``)::

    python app.py
"""

from __future__ import annotations

import hmac
import signal
import sys
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import Flask, jsonify, request

from clients.jira_client import JiraClient
from clients.mail_client import MailClient
from clients.soar_client import IBMSoarClient
from config import get_settings
from repositories.mapping_repository import MappingRepository
from services.sync_service import SyncService
from utils.logger import get_logger, safe_dump

settings = get_settings()
log = get_logger("app")
flask_app = Flask(__name__)
_sync: SyncService | None = None
_shutdown = threading.Event()


def _build_sync_service() -> SyncService:
    jira = JiraClient()
    repo = MappingRepository()
    soar = IBMSoarClient() if settings.soar.configured else None
    mail = MailClient() if settings.polling.mail_poll_enabled else None
    return SyncService(jira=jira, soar=soar, mail=mail, repo=repo)


def require_token(view: Callable[..., Any]) -> Callable[..., Any]:
    """Enforce ``Authorization: Bearer <APP_API_TOKEN>`` when a token is configured."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        expected = settings.runtime.api_token
        if expected:
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied, expected):
                log.warning(
                    "Rejected unauthenticated request to %s from %s",
                    request.path,
                    request.remote_addr,
                )
                return jsonify({"error": "unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapper


@flask_app.get("/health")
def health() -> Any:
    return jsonify({"status": "ok", "mappings": _sync.repo.count_all() if _sync else 0})


@flask_app.post("/api/alarm")
@require_token
def receive_alarm() -> Any:
    """New alert from a SOAR playbook or any other source."""
    if _sync is None:
        return jsonify({"error": "service not ready"}), 503
    payload = request.get_json(silent=True) or {}
    log.info("POST /api/alarm payload=%s", safe_dump(payload))
    if not payload:
        return jsonify({"error": "empty payload"}), 400
    try:
        result = _sync.handle_new_alarm(payload)
        status_code = 201 if result["action"] == "created" else 200
        log.info("POST /api/alarm -> %s %s", status_code, safe_dump(result))
        return jsonify(result), status_code
    except Exception as exc:
        log.error("Alert processing failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@flask_app.post("/api/close")
@require_token
def receive_close() -> Any:
    """Close request for a SOAR incident; the linked Jira issue is transitioned to done."""
    if _sync is None:
        return jsonify({"error": "service not ready"}), 503
    payload = request.get_json(silent=True) or {}
    log.info("POST /api/close payload=%s", safe_dump(payload))
    incident_id = payload.get("incident_id") or payload.get("soar_incident_id")
    if not incident_id:
        return jsonify({"error": "incident_id required"}), 400
    try:
        result = _sync.handle_soar_close(str(incident_id))
        log.info("POST /api/close -> %s", safe_dump(result))
        return jsonify(result)
    except Exception as exc:
        log.error("Close processing failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@flask_app.get("/api/status")
def sync_status() -> Any:
    if _sync is None:
        return jsonify({"error": "service not ready"}), 503
    return jsonify(
        {
            "total_mappings": _sync.repo.count_all(),
            "active": len(_sync.repo.find_active()),
            "errors": len(_sync.repo.find_errors()),
        }
    )


def _polling_loop(sync: SyncService) -> None:
    interval = settings.polling.interval_seconds
    log.info("Polling loop started, interval %ds", interval)
    while not _shutdown.is_set():
        try:
            log.info("Poll cycle complete: %s", safe_dump(sync.run_poll_cycle()))
        except Exception as exc:
            log.error("Poll cycle failed: %s", exc)
        _shutdown.wait(timeout=interval)
    log.info("Polling loop stopped")


def main() -> None:
    global _sync
    _sync = _build_sync_service()
    log.info("Sync engine initialized, %d mappings in the database", _sync.repo.count_all())

    def _signal_handler(_sig: int, _frame: Any) -> None:
        log.info("Shutdown signal received")
        _shutdown.set()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    mode = settings.runtime.mode
    if mode == "poll-only":
        log.info("Starting in poll-only mode")
        _polling_loop(_sync)
        return

    if not settings.runtime.api_token:
        log.warning("APP_API_TOKEN is not set; the webhook API accepts unauthenticated requests")
    if mode == "both":
        threading.Thread(target=_polling_loop, args=(_sync,), daemon=True).start()
    log.info("Starting API (%s mode) on %s:%d", mode, settings.runtime.host, settings.runtime.port)
    flask_app.run(host=settings.runtime.host, port=settings.runtime.port)


if __name__ == "__main__":
    main()
