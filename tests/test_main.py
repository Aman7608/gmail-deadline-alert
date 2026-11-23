"""End-to-end tests of the main loop with fake Gmail, Claude and Twilio (no real APIs)."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from twilio.base.exceptions import TwilioRestException

from app.ai_extractor import Extraction
from app.db import Database
from app.gmail_client import Email
from app.main import MAX_CALL_ATTEMPTS, AlertApp

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 11, 23, 10, 0, tzinfo=IST)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeGmail:
    def __init__(self):
        self.emails = {}

    def add(self, gmail_id, subject="Drive"):
        self.emails[gmail_id] = Email(gmail_id, "careers@example.edu", subject, "body", START)

    def list_new_message_ids(self, now):
        return list(self.emails)

    def get_email(self, gmail_id):
        return self.emails[gmail_id]


class FakeExtractor:
    def __init__(self, deadline):
        self.deadline = deadline
        self.calls = 0

    def extract(self, email, now):
        self.calls += 1
        return Extraction("Summary of " + email.subject, "Acme Corp", "Register", self.deadline)


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.calls = []
        self.status = "delivered"
        self.send_error = None
        self.call_error = None

    def send_whatsapp(self, content, call_at=None):
        if self.send_error:
            raise self.send_error
        self.sent.append(content)
        return f"SM{len(self.sent)}", "queued"

    def get_message_status(self, sid):
        return self.status

    def place_call(self, content):
        if self.call_error:
            raise self.call_error
        self.calls.append(content)
        return f"CA{len(self.calls)}"


def make_app(deadline, dry_run=False, out=print):
    settings = SimpleNamespace(tz=IST, no_deadline_call_minutes=60, watch_senders=["careers@example.edu"], poll_seconds=60)
    clock = Clock(START)
    gmail = FakeGmail()
    notifier = FakeNotifier()
    extractor = FakeExtractor(deadline)
    app = AlertApp(settings, Database(":memory:"), lambda: gmail, extractor, notifier,
                   dry_run=dry_run, now_fn=clock, out=out)
    return app, clock, gmail, notifier, extractor


def only_alert(app):
    alerts = app.db.all_alerts()
    assert len(alerts) == 1
    return alerts[0]


# ---------- duplicates ----------

def test_same_email_is_processed_once():
    app, clock, gmail, notifier, extractor = make_app(START + timedelta(days=1))
    gmail.add("m1")
    app.run_once()
    app.run_once()
    clock.advance(minutes=5)
    app.run_once()
    assert extractor.calls == 1
    assert len(notifier.sent) == 1
    assert len(app.db.all_alerts()) == 1


def test_process_email_twice_returns_false():
    app, _, gmail, _, _ = make_app(None)
    gmail.add("m1")
    email = gmail.get_email("m1")
    assert app.process_email(email) is True
    assert app.process_email(email) is False


# ---------- main flows ----------

def test_unread_message_gets_called_at_planned_time():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(hours=24))
    gmail.add("m1")
    app.run_once()
    alert = only_alert(app)
    assert alert.call_at == START + timedelta(hours=12)
    assert alert.whatsapp_sid == "SM1"

    clock.advance(hours=11, minutes=59)
    app.run_once()
    assert notifier.calls == []

    clock.advance(minutes=1)
    app.run_once()
    assert len(notifier.calls) == 1
    assert only_alert(app).call_status == "placed"


def test_read_message_cancels_call():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(hours=24))
    gmail.add("m1")
    app.run_once()
    notifier.status = "read"
    clock.advance(hours=1)
    app.run_once()
    assert only_alert(app).call_status == "cancelled_read"
    clock.advance(hours=12)
    app.run_once()
    assert notifier.calls == []


def test_never_calls_twice():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(minutes=30))
    gmail.add("m1")
    app.run_once()  # under 1 hour left: call right away
    for _ in range(5):
        clock.advance(minutes=1)
        app.run_once()
    assert len(notifier.calls) == 1


def test_deadline_passed_sends_whatsapp_only():
    app, clock, gmail, notifier, _ = make_app(START - timedelta(hours=1))
    gmail.add("m1")
    app.run_once()
    clock.advance(hours=5)
    app.run_once()
    assert len(notifier.sent) == 1
    assert notifier.calls == []
    assert only_alert(app).call_status == "none"


def test_no_deadline_calls_after_60_minutes():
    app, clock, gmail, notifier, _ = make_app(None)
    gmail.add("m1")
    app.run_once()
    clock.advance(minutes=59)
    app.run_once()
    assert notifier.calls == []
    clock.advance(minutes=1)
    app.run_once()
    assert len(notifier.calls) == 1


def test_failed_whatsapp_still_calls():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(hours=3))
    notifier.send_error = ConnectionError("no internet")
    gmail.add("m1")
    app.run_once()
    alert = only_alert(app)
    assert alert.whatsapp_sid is None  # never sent
    assert alert.call_status == "pending"
    clock.advance(hours=1)  # call_at = deadline - 2h = START + 1h
    app.run_once()
    assert len(notifier.calls) == 1


def test_undelivered_whatsapp_status_still_calls():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(hours=3))
    notifier.status = "undelivered"
    gmail.add("m1")
    app.run_once()
    clock.advance(hours=1)
    app.run_once()
    assert len(notifier.calls) == 1
    assert only_alert(app).whatsapp_status == "undelivered"


def test_whatsapp_retry_after_temporary_error():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(days=2))
    notifier.send_error = ConnectionError("blip")
    gmail.add("m1")
    app.check_new_mail()  # first send fails
    assert only_alert(app).whatsapp_status == "not_sent"
    notifier.send_error = None
    app.retry_unsent_whatsapp()
    assert only_alert(app).whatsapp_sid == "SM1"


def test_missed_call_after_deadline_is_expired():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(hours=13))
    gmail.add("m1")
    app.run_once()
    clock.advance(hours=14)  # program was "off" past the deadline
    app.run_once()
    assert notifier.calls == []
    assert only_alert(app).call_status == "expired"


def test_twilio_rejection_retries_then_gives_up():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(minutes=30))
    notifier.call_error = TwilioRestException(status=400, uri="/Calls", msg="bad number")
    gmail.add("m1")
    for _ in range(MAX_CALL_ATTEMPTS + 2):
        app.run_once()
        clock.advance(minutes=1)
    alert = only_alert(app)
    assert alert.call_status == "failed"
    assert alert.call_attempts == MAX_CALL_ATTEMPTS


def test_unknown_call_error_is_not_retried():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(minutes=30))
    notifier.call_error = TimeoutError("network cut mid-request")
    gmail.add("m1")
    app.run_once()
    notifier.call_error = None
    clock.advance(minutes=1)
    app.run_once()
    assert notifier.calls == []  # we may have called already, so we never risk a second call
    assert only_alert(app).call_status == "failed"


# ---------- robustness ----------

def test_loop_survives_gmail_errors():
    app, _, gmail, notifier, _ = make_app(None)

    def broken():
        raise ConnectionError("offline")

    gmail.list_new_message_ids = lambda now: broken()
    app.run_once()  # must not raise
    assert app.gmail is None  # will reconnect next round


def test_status_check_error_does_not_crash():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(hours=24))
    gmail.add("m1")
    app.run_once()

    def broken(sid):
        raise ConnectionError("offline")

    notifier.get_message_status = broken
    app.run_once()
    assert only_alert(app).call_status == "pending"


# ---------- dry run ----------

def test_dry_run_prints_and_sends_nothing():
    lines = []
    app, clock, gmail, notifier, _ = make_app(START + timedelta(hours=5), dry_run=True, out=lines.append)
    gmail.add("m1", subject="Wipro drive")
    app.run_once()
    printed = "\n".join(lines)
    assert "[DRY RUN]" in printed
    assert "Summary of Wipro drive" in printed
    assert "01:00 PM" in printed  # 5h left -> call at deadline - 2h = 13:00
    assert notifier.sent == []
    assert notifier.calls == []


def test_gmail_login_problem_does_not_stop_calls():
    from app.gmail_client import GmailAuthError

    app, clock, gmail, notifier, _ = make_app(START + timedelta(minutes=30))
    gmail.add("m1")
    app.run_once()  # alert created and call placed right away
    assert len(notifier.calls) == 1

    def expired():
        raise GmailAuthError("token expired")

    app.gmail_factory = expired
    app.gmail = None
    app.run_once()  # must keep running in normal mode


def test_gmail_login_problem_stops_in_fail_fast_mode():
    from app.gmail_client import GmailAuthError

    app, _, _, _, _ = make_app(None)
    app.fail_fast = True

    def expired():
        raise GmailAuthError("token expired")

    app.gmail_factory = expired
    with pytest.raises(GmailAuthError):
        app.run_once()


def test_test_alert_still_calls_when_whatsapp_fails(monkeypatch, capsys):
    from app import main as main_module

    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)
    notifier = FakeNotifier()
    notifier.send_error = TwilioRestException(400, "uri", "Unable to create record: ContentSid Required")
    main_module.run_test_alert(SimpleNamespace(tz=IST), notifier)
    assert len(notifier.calls) == 1
    assert "WhatsApp FAILED" in capsys.readouterr().out


def test_whatsapp_off_still_calls_on_time():
    app, clock, gmail, notifier, _ = make_app(START + timedelta(hours=24))
    app.s.whatsapp_enabled = False
    gmail.add("m1")
    app.run_once()
    alert = only_alert(app)
    assert notifier.sent == []
    assert alert.whatsapp_status == "disabled"
    clock.advance(hours=12)
    app.run_once()
    app.run_once()
    assert len(notifier.calls) == 1  # called once at deadline - 12h, never twice
    assert notifier.sent == []


def test_test_alert_skips_whatsapp_when_off(monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)
    notifier = FakeNotifier()
    main_module.run_test_alert(SimpleNamespace(tz=IST, whatsapp_enabled=False), notifier)
    assert notifier.sent == []
    assert len(notifier.calls) == 1
