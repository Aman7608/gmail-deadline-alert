import base64
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.gmail_client import GmailClient, build_query, extract_body, html_to_text, parse_message


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_build_query_single_and_multiple():
    after = datetime(2026, 11, 23, tzinfo=timezone.utc)
    ts = int(after.timestamp())
    assert build_query(["a@x.com"], after) == f"from:a@x.com -in:sent -in:drafts after:{ts}"
    assert build_query(["a@x.com", "b@y.com"], after) == f"from:(a@x.com OR b@y.com) -in:sent -in:drafts after:{ts}"


def test_extract_body_prefers_plain_text():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
            {"mimeType": "text/plain", "body": {"data": b64("Plain version")}},
        ],
    }
    assert extract_body(payload) == "Plain version"


def test_extract_body_falls_back_to_html():
    payload = {"mimeType": "text/html", "body": {"data": b64("<p>Register by <b>25 Nov</b></p><style>x{}</style>")}}
    assert extract_body(payload) == "Register by 25 Nov"


def test_html_to_text_unescapes():
    assert html_to_text("A &amp; B<br>C") == "A & B\nC"


def test_parse_message():
    msg = {
        "id": "abc123",
        "internalDate": str(int(datetime(2026, 11, 23, 4, 30, tzinfo=timezone.utc).timestamp() * 1000)),
        "snippet": "snippet text",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "College Careers <Careers@Example.edu>"},
                {"name": "Subject", "value": "Acme Corp drive"},
            ],
            "body": {"data": b64("Register now")},
        },
    }
    email = parse_message(msg)
    assert email.gmail_id == "abc123"
    assert email.sender == "careers@example.edu"
    assert email.subject == "Acme Corp drive"
    assert email.body == "Register now"
    assert email.received_at == datetime(2026, 11, 23, 4, 30, tzinfo=timezone.utc)


def test_list_new_message_ids_pages_and_orders_oldest_first():
    service = MagicMock()
    list_call = service.users.return_value.messages.return_value.list
    list_call.return_value.execute.side_effect = [
        {"messages": [{"id": "3"}, {"id": "2"}], "nextPageToken": "t"},
        {"messages": [{"id": "1"}]},
    ]
    client = GmailClient(service, ["a@x.com"], lookback_hours=24)
    ids = client.list_new_message_ids(datetime(2026, 11, 23, tzinfo=timezone.utc))
    assert ids == ["1", "2", "3"]
    assert list_call.call_count == 2


def test_client_never_calls_modify():
    """The app must never change the inbox (no modify / trash / delete)."""
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {}
    client = GmailClient(service, ["a@x.com"])
    client.list_new_message_ids(datetime(2026, 11, 23, tzinfo=timezone.utc))
    messages = service.users.return_value.messages.return_value
    assert not messages.modify.called
    assert not messages.trash.called
    assert not messages.delete.called


def test_build_query_with_domain_and_extra_search():
    after = datetime(2026, 11, 23, tzinfo=timezone.utc)
    ts = int(after.timestamp())
    assert build_query(["@company.com"], after) == f"from:company.com -in:sent -in:drafts after:{ts}"
    assert build_query(["hr@a.com", "@b.org"], after, "subject:interview") == \
        f"from:(hr@a.com OR b.org) (subject:interview) -in:sent -in:drafts after:{ts}"


def test_build_query_with_only_a_search():
    after = datetime(2026, 11, 23, tzinfo=timezone.utc)
    ts = int(after.timestamp())
    assert build_query([], after, "label:important is:unread") == f"(label:important is:unread) -in:sent -in:drafts after:{ts}"
