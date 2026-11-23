import json
import xml.etree.ElementTree as ET
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.notifier import (
    AlertContent,
    TwilioNotifier,
    build_call_text,
    build_content_variables,
    build_twiml,
    build_whatsapp_text,
    format_deadline,
)

IST = ZoneInfo("Asia/Kolkata")
DEADLINE = datetime(2026, 11, 25, 17, 0, tzinfo=IST)
CONTENT = AlertContent("Acme Corp drive", "Acme Corp is hiring 2027 grads.", "Acme Corp", "Register on portal", DEADLINE)


def settings(**kw):
    base = dict(
        tz=IST, twilio_whatsapp_from="whatsapp:+14155238886", my_whatsapp_to="whatsapp:+919999999999",
        twilio_voice_from="+15005550006", my_phone_number="+919999999999", twilio_content_sid="",
        twilio_content_variables={"1": "summary", "2": "deadline", "3": "action"},
        twilio_voice="Polly.Aditi", twilio_voice_language="en-IN",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_format_deadline():
    assert format_deadline(DEADLINE, IST) == "Wed, 25 Nov 2026, 05:00 PM IST"
    assert format_deadline(None, IST) == "No deadline mentioned"


def test_whatsapp_text_has_all_parts():
    text = build_whatsapp_text(CONTENT, IST, call_at=datetime(2026, 11, 25, 5, 0, tzinfo=IST))
    assert "Acme Corp is hiring" in text
    assert "Register on portal" in text
    assert "25 Nov 2026, 05:00 PM IST" in text
    assert "05:00 AM" in text


def test_twiml_is_valid_xml_and_repeats_twice():
    twiml = build_twiml(build_call_text(CONTENT, IST), "Polly.Aditi", "en-IN")
    root = ET.fromstring(twiml)
    says = root.findall("Say")
    assert len(says) == 2
    assert says[0].get("voice") == "Polly.Aditi"
    assert says[0].get("language") == "en-IN"
    assert "Acme Corp" in says[1].text


def test_twiml_escapes_special_characters():
    content = AlertContent("x", 'R&D <drive> "now" & </Say><Hangup/>', "AT&T", None, None)
    twiml = build_twiml(build_call_text(content, IST), "Polly.Aditi", "en-IN")
    root = ET.fromstring(twiml)  # would fail if not escaped
    assert root.find("Hangup") is None
    assert "R&D <drive>" in root.findall("Say")[0].text
    assert "&amp;" in twiml and "&lt;drive&gt;" in twiml


def test_call_text_without_deadline():
    content = AlertContent("x", "Pre-placement talk", None, None, None)
    assert "No deadline was mentioned" in build_call_text(content, IST)


def test_content_variables():
    data = json.loads(build_content_variables({"1": "summary", "2": "deadline"}, CONTENT, IST))
    assert data == {"1": "Acme Corp is hiring 2027 grads.", "2": "Wed, 25 Nov 2026, 05:00 PM IST"}
    with pytest.raises(ValueError):
        build_content_variables({"1": "salary"}, CONTENT, IST)


# ---------- Twilio client is mocked: no real messages or calls ----------

def test_send_whatsapp_freeform_in_sandbox():
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(sid="SM1", status="queued")
    sid, status = TwilioNotifier(settings(), client=client).send_whatsapp(CONTENT)
    assert (sid, status) == ("SM1", "queued")
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["to"] == "whatsapp:+919999999999"
    assert "body" in kwargs and "content_sid" not in kwargs


def test_send_whatsapp_with_template():
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(sid="SM2", status="queued")
    TwilioNotifier(settings(twilio_content_sid="HX123"), client=client).send_whatsapp(CONTENT)
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["content_sid"] == "HX123"
    assert json.loads(kwargs["content_variables"])["3"] == "Register on portal"
    assert "body" not in kwargs


def test_get_message_status():
    client = MagicMock()
    client.messages.return_value.fetch.return_value = SimpleNamespace(status="read")
    assert TwilioNotifier(settings(), client=client).get_message_status("SM1") == "read"
    client.messages.assert_called_with("SM1")


def test_place_call_sends_twiml():
    client = MagicMock()
    client.calls.create.return_value = SimpleNamespace(sid="CA1")
    assert TwilioNotifier(settings(), client=client).place_call(CONTENT) == "CA1"
    kwargs = client.calls.create.call_args.kwargs
    assert kwargs["to"] == "+919999999999"
    assert kwargs["from_"] == "+15005550006"
    ET.fromstring(kwargs["twiml"])


def test_custom_title_and_sender_in_messages():
    content = AlertContent("Bill", "Electricity bill is due.", "Electricity bill", "Pay the bill", DEADLINE,
                           sender="billing@power.in")
    text = build_whatsapp_text(content, IST, title="Bill alert")
    assert text.startswith("*Bill alert*")
    assert "*From:* billing@power.in" in text
    assert "*About:* Electricity bill" in text
    assert build_call_text(content, IST, title="Bill alert").startswith("Hello. This is your Bill alert.")


def test_notifier_uses_alert_title_setting():
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(sid="SM1", status="queued")
    TwilioNotifier(settings(alert_title="Exam alert"), client=client).send_whatsapp(CONTENT)
    assert client.messages.create.call_args.kwargs["body"].startswith("*Exam alert*")


def test_sender_template_field():
    content = AlertContent("s", "sum", None, None, None, sender="a@b.com")
    assert json.loads(build_content_variables({"1": "sender"}, content, IST)) == {"1": "a@b.com"}


def test_place_call_uses_url_on_limited_trial():
    client = MagicMock()
    client.calls.create.return_value = SimpleNamespace(sid="CA2")
    url = "https://webhooks.twilio.com/v1/Voice/Template/voice_speech_recognition"
    assert TwilioNotifier(settings(twilio_call_url=url), client=client).place_call(CONTENT) == "CA2"
    kwargs = client.calls.create.call_args.kwargs
    assert kwargs["url"] == url
    assert "twiml" not in kwargs
