from __future__ import annotations

import os

import pytest

from clients.jira_client import JiraClient
from clients.mail_client import MailClient
from clients.soar_client import IBMSoarClient, resilient
from config import get_settings

pytestmark = pytest.mark.integration


def _integration_enabled() -> bool:
    return os.getenv("RUN_INTEGRATION_TESTS", "").strip() == "1"


@pytest.mark.skipif(
    not _integration_enabled(), reason="Set RUN_INTEGRATION_TESTS=1 to run integration checks"
)
def test_jira_connection() -> None:
    settings = get_settings().jira
    if not (settings.base_url and settings.email and settings.api_token):
        pytest.skip("Jira credentials are not configured")
    assert JiraClient().test_connection() is True


@pytest.mark.skipif(
    not _integration_enabled(), reason="Set RUN_INTEGRATION_TESTS=1 to run integration checks"
)
def test_mail_connection() -> None:
    if not get_settings().mail.configured:
        pytest.skip("Mail credentials are not configured")
    assert MailClient().test_connection() is True


@pytest.mark.skipif(
    not _integration_enabled(), reason="Set RUN_INTEGRATION_TESTS=1 to run integration checks"
)
def test_soar_connection() -> None:
    if resilient is None:
        pytest.skip("IBM SOAR SDK is not installed")
    if not get_settings().soar.configured:
        pytest.skip("SOAR credentials are not configured")
    assert IBMSoarClient().test_connection() is True
