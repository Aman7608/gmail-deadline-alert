"""Sends the WhatsApp message and places the phone call using Twilio."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# Twilio message statuses that mean the message will never be read
FAILED_STATUSES = {"failed", "undelivered", "canceled"}
READ_STATUS = "read"


@dataclass
class AlertContent:
    """What we tell the user, for both WhatsApp and the phone call."""
    subject: str
    summary: str
    topic: str | None
    action_required: str | None
    deadline: datetime | None
    sender: str = ""


def format_deadline(deadline: datetime | None, tz: ZoneInfo) -> str:
    if deadline is None:
        return "No deadline mentioned"
    return deadline.astimezone(tz).strftime("%a, %d %b %Y, %I:%M %p %Z")


def spoken_deadline(deadline: datetime | None, tz: ZoneInfo) -> str:
    if deadline is None:
        return "No deadline was mentioned."
    d = deadline.astimezone(tz)
    hour = d.strftime("%I").lstrip("0")
    return f"The deadline is {d.strftime('%A')}, {d.day} {d.strftime('%B')}, at {hour}:{d.strftime('%M %p')}."


def build_whatsapp_text(content: AlertContent, tz: ZoneInfo, call_at: datetime | None = None,
                        title: str = "Email alert") -> str:
    lines = [f"*{title}*"]
    if content.sender:
        lines.append(f"*From:* {content.sender}")
    if content.topic:
        lines.append(f"*About:* {content.topic}")
    lines.append(f"*Summary:* {content.summary}")
    if content.action_required:
        lines.append(f"*Action:* {content.action_required}")
    lines.append(f"*Deadline:* {format_deadline(content.deadline, tz)}")
    if call_at is not None:
        lines.append(f"_If this stays unread, I will call you at {call_at.astimezone(tz).strftime('%d %b, %I:%M %p')}._")
    return "\n".join(lines)


def build_call_text(content: AlertContent, tz: ZoneInfo, title: str = "Email alert") -> str:
    parts = [f"Hello. This is your {title.rstrip('.')}."]
    if content.topic:
        parts.append(f"About: {content.topic}.")
    parts.append(content.summary.rstrip(".") + ".")
    if content.action_required:
        parts.append(f"Action required: {content.action_required.rstrip('.')}.")
    parts.append(spoken_deadline(content.deadline, tz))
    return " ".join(parts)


def build_twiml(text: str, voice: str, language: str) -> str:
    """TwiML that reads the text twice. All text is XML-escaped."""
    attrs = f"voice={quoteattr(voice)} language={quoteattr(language)}"
    safe = escape(text)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say {attrs}>{safe}</Say>"
        '<Pause length="2"/>'
        f"<Say {attrs}>I repeat. {safe}</Say>"
        "</Response>"
    )


def build_content_variables(mapping: dict[str, str], content: AlertContent, tz: ZoneInfo) -> str:
    """JSON for a WhatsApp template, e.g. {"1": "<summary>", "2": "<deadline>"}."""
    fields = {
        "summary": content.summary,
        "topic": content.topic or "-",
        "action": content.action_required or "-",
        "deadline": format_deadline(content.deadline, tz),
        "subject": content.subject or "-",
        "sender": content.sender or "-",
    }
    values = {}
    for placeholder, field_name in mapping.items():
        if field_name not in fields:
            raise ValueError(f"Unknown template field {field_name!r}. Use one of: {', '.join(fields)}")
        values[placeholder] = fields[field_name]
    return json.dumps(values)


class TwilioNotifier:
    def __init__(self, settings, client=None):
        if client is None:
            from twilio.rest import Client

            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self.client = client
        self.s = settings
        self._title = getattr(settings, "alert_title", "Email alert")

    def send_whatsapp(self, content: AlertContent, call_at: datetime | None = None) -> tuple[str, str]:
        """Send the WhatsApp alert. Returns (message sid, status)."""
        if self.s.twilio_content_sid:
            # Production: approved template
            msg = self.client.messages.create(
                from_=self.s.twilio_whatsapp_from,
                to=self.s.my_whatsapp_to,
                content_sid=self.s.twilio_content_sid,
                content_variables=build_content_variables(self.s.twilio_content_variables, content, self.s.tz),
            )
        else:
            # Sandbox / inside the 24 hour window: free-form text
            msg = self.client.messages.create(
                from_=self.s.twilio_whatsapp_from,
                to=self.s.my_whatsapp_to,
                body=build_whatsapp_text(content, self.s.tz, call_at, self._title),
            )
        log.info("WhatsApp sent: sid=%s status=%s", msg.sid, msg.status)
        return msg.sid, msg.status

    def get_message_status(self, sid: str) -> str:
        return self.client.messages(sid).fetch().status

    def place_call(self, content: AlertContent) -> str:
        """Call my phone and read the alert aloud. Returns the call sid."""
        call_url = getattr(self.s, "twilio_call_url", "")
        if call_url:
            # Twilio "Limited trial" only allows its own voice URLs, not custom speech.
            # The phone still rings, but it can't read the alert aloud.
            call = self.client.calls.create(to=self.s.my_phone_number, from_=self.s.twilio_voice_from, url=call_url)
        else:
            twiml = build_twiml(build_call_text(content, self.s.tz, self._title), self.s.twilio_voice, self.s.twilio_voice_language)
            call = self.client.calls.create(to=self.s.my_phone_number, from_=self.s.twilio_voice_from, twiml=twiml)
        log.info("Call placed: sid=%s", call.sid)
        return call.sid
