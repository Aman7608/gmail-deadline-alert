"""Uses an AI (Claude or Google Gemini) to read an email and pull out the important parts.

The AI returns strict JSON (enforced with a JSON schema):
  summary, topic, action_required, deadline (ISO 8601 or null)

If anything goes wrong (API error, refusal, bad JSON) we fall back to
summary = email subject and deadline = None, so an alert is still sent.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.gmail_client import Email

log = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 300
# Most emails are short. This limit only stops a huge email (like a
# long newsletter) from costing a lot. We log when it happens.
MAX_BODY_CHARS = 30_000

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "topic": {"type": ["string", "null"]},
        "action_required": {"type": ["string", "null"]},
        "deadline": {"type": ["string", "null"]},
    },
    "required": ["summary", "topic", "action_required", "deadline"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You read emails for a busy person and pull out what they need to know and do.
The emails can be about anything: jobs, college, exams, bills, meetings, bookings, government forms and so on.

Return JSON with these fields:
- summary: a short WhatsApp-friendly summary, at most 300 characters. Plain text, no markdown.
- topic: a few words naming what the email is about (for example a company, event, course or bill name), or null.
- action_required: what the person must do, in a few words (for example "Register on portal",
  "Pay the bill", "Join the meeting"), or null if nothing needs doing.
- deadline: the deadline for that action as an ISO 8601 date-time with the timezone offset of {tz_name}
  (for example "2026-11-25T17:00:00{tz_offset}"). Rules:
  * If there are several dates, use the EARLIEST date by which the person must take an action
    (register, apply, pay, submit, attend, reply). Ignore dates that need no action from them.
  * If only a date is given with no time, use 23:59 on that date.
  * Resolve relative dates like "tomorrow" or "by Friday" using the email's received time.
  * If there is no deadline at all, use null. Do not guess a deadline that is not in the email."""


@dataclass
class Extraction:
    summary: str
    topic: str | None
    action_required: str | None
    deadline: datetime | None
    used_fallback: bool = False


def fallback(email: Email, reason: str) -> Extraction:
    log.warning("Using fallback summary for %s: %s", email.gmail_id, reason)
    return Extraction(
        summary=(email.subject or "New email")[:MAX_SUMMARY_CHARS],
        topic=None,
        action_required=None,
        deadline=None,
        used_fallback=True,
    )


def _clean(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_deadline(value, tz: ZoneInfo) -> datetime | None:
    """Turn the deadline string into an aware datetime in `tz`. Bad values become None."""
    value = _clean(value)
    if not value or value.lower() in ("null", "none"):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("The AI returned a deadline that is not ISO 8601: %r", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)  # no offset given: treat it as local (IST) time
    return dt.astimezone(tz)


def parse_ai_response(text: str, email: Email, tz: ZoneInfo) -> Extraction:
    """Parse the AI's JSON text. Falls back if the JSON is invalid or has no summary."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        return fallback(email, f"invalid JSON ({e})")
    if not isinstance(data, dict):
        return fallback(email, "JSON is not an object")

    summary = _clean(data.get("summary"))
    if not summary:
        return fallback(email, "empty summary")
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[: MAX_SUMMARY_CHARS - 3].rstrip() + "..."

    return Extraction(
        summary=summary,
        topic=_clean(data.get("topic")),
        action_required=_clean(data.get("action_required")),
        deadline=parse_deadline(data.get("deadline"), tz),
    )


def build_user_message(email: Email, now: datetime, tz: ZoneInfo) -> str:
    body = email.body
    if len(body) > MAX_BODY_CHARS:
        log.info("Email %s is long (%d chars); sending the first %d to the AI", email.gmail_id, len(body), MAX_BODY_CHARS)
        body = body[:MAX_BODY_CHARS]
    return (
        f"Email received at: {email.received_at.astimezone(tz).isoformat()}\n"
        f"Current time: {now.astimezone(tz).isoformat()}\n"
        f"From: {email.sender}\n"
        f"Subject: {email.subject}\n\n"
        f"Body:\n{body}"
    )


class AIExtractor:
    def __init__(self, api_key: str, model: str, tz: ZoneInfo, client=None):
        if client is None:
            import anthropic

            # max_retries makes the SDK retry 429 / 5xx / connection errors for us
            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120)
        self.client = client
        self.model = model
        self.tz = tz
        # Fill the timezone into the prompt once (it never changes while running)
        offset = datetime.now(tz).strftime("%z")
        self.system_prompt = SYSTEM_PROMPT.format(tz_name=str(tz), tz_offset=f"{offset[:3]}:{offset[3:]}")

    def extract(self, email: Email, now: datetime) -> Extraction:
        import anthropic

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=self.system_prompt,
                messages=[{"role": "user", "content": build_user_message(email, now, self.tz)}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
            )
        except anthropic.AuthenticationError as e:
            return fallback(email, f"Claude API key rejected ({e})")
        except anthropic.RateLimitError as e:
            return fallback(email, f"Claude rate limit ({e})")
        except anthropic.APIStatusError as e:
            return fallback(email, f"Claude API error {e.status_code} ({e})")
        except anthropic.APIConnectionError as e:
            return fallback(email, f"could not reach Claude ({e})")
        except Exception as e:  # never let the AI step crash the app
            return fallback(email, f"unexpected error ({e!r})")

        if response.stop_reason == "refusal":
            return fallback(email, "Claude declined the request")
        if response.stop_reason == "max_tokens":
            return fallback(email, "Claude reply was cut off")

        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            return fallback(email, "no text in Claude reply")
        return parse_ai_response(text, email, self.tz)


# ---------------- Google Gemini (has a free tier) ----------------

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Gemini uses its own (OpenAPI style) schema format
GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "topic": {"type": "STRING", "nullable": True},
        "action_required": {"type": "STRING", "nullable": True},
        "deadline": {"type": "STRING", "nullable": True},
    },
    "required": ["summary", "topic", "action_required", "deadline"],
}

# Errors worth trying again: too many requests, or Google having a problem
RETRY_STATUS = {429, 500, 502, 503, 504}


class GeminiExtractor:
    """Same job as AIExtractor, but uses the Gemini API (free tier from aistudio.google.com)."""

    def __init__(self, api_key: str, model: str, tz: ZoneInfo, session=None, attempts: int = 3,
                 sleep=time.sleep):
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.api_key = api_key
        self.model = model
        self.tz = tz
        self.attempts = attempts
        self.sleep = sleep
        offset = datetime.now(tz).strftime("%z")
        self.system_prompt = SYSTEM_PROMPT.format(tz_name=str(tz), tz_offset=f"{offset[:3]}:{offset[3:]}")

    def request_body(self, email: Email, now: datetime) -> dict:
        return {
            "systemInstruction": {"parts": [{"text": self.system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": build_user_message(email, now, self.tz)}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": GEMINI_SCHEMA,
            },
        }

    def extract(self, email: Email, now: datetime) -> Extraction:
        url = GEMINI_URL.format(model=self.model)
        # The key goes in a header, not the URL, so it never shows up in logs
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}
        body = self.request_body(email, now)

        response = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.session.post(url, headers=headers, json=body, timeout=120)
            except Exception as e:  # network problem
                if attempt == self.attempts:
                    return fallback(email, f"could not reach Gemini ({e!r})")
                self.sleep(2 * attempt)
                continue
            if response.status_code in RETRY_STATUS and attempt < self.attempts:
                log.warning("Gemini returned %s, trying again", response.status_code)
                self.sleep(5 * attempt)
                continue
            break

        if response.status_code != 200:
            detail = (getattr(response, "text", "") or "")[:200]
            if response.status_code in (400, 401, 403) and "API key" in detail:
                return fallback(email, f"Gemini API key rejected ({detail})")
            return fallback(email, f"Gemini API error {response.status_code} ({detail})")

        try:
            data = response.json()
        except Exception as e:
            return fallback(email, f"Gemini reply was not JSON ({e})")

        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            return fallback(email, f"Gemini blocked the request ({block})")
        candidates = data.get("candidates") or []
        if not candidates:
            return fallback(email, "no answer from Gemini")
        finish = candidates[0].get("finishReason")
        if finish == "MAX_TOKENS":
            return fallback(email, "Gemini reply was cut off")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if not text:
            return fallback(email, f"no text in Gemini reply (finishReason={finish})")
        return parse_ai_response(text, email, self.tz)


def make_extractor(settings):
    """Build the extractor chosen by AI_PROVIDER in .env."""
    if settings.ai_provider == "gemini":
        return GeminiExtractor(settings.gemini_api_key, settings.gemini_model, settings.tz)
    return AIExtractor(settings.anthropic_api_key, settings.claude_model, settings.tz)
