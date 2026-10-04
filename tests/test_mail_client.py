from __future__ import annotations

from clients.mail_client import MailClient, ParsedEmail


def test_mail_parser_extracts_fields() -> None:
    client = MailClient()
    parsed = ParsedEmail(
        message_id="<message-1>",
        subject="Critical Severity Incident #15730",
        sender="alerts",
        body=(
            "Offense ID\n15730\n"
            "Offense Name\nImpossible travel detection\n"
            "Offense Category\nAuthentication\n"
            "Severity\nCritical\n"
            "Status: Known Offense\n"
            "Reason: Under investigation\n"
            "Responder: Analyst\n"
        ),
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        uid="1",
    )
    client.parse_notification(parsed)
    assert parsed.offense_id == "15730"
    assert parsed.offense_name == "Impossible travel detection"
    assert parsed.response_status == "Known Offense"
    assert parsed.responder == "Analyst"
