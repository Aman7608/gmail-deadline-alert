import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.ai_extractor import AIExtractor, GeminiExtractor, make_extractor
from app.gmail_client import Email

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=IST)
EMAIL = Email("m1", "careers@example.edu", "Acme Corp hiring drive", "Register by 25 Nov 5 PM", NOW)

GOOD_JSON = json.dumps({
    "summary": "Acme Corp drive for 2027 batch.",
    "topic": "Acme Corp",
    "action_required": "Register on portal",
    "deadline": "2026-11-25T17:00:00+05:30",
})


def http_reply(status=200, data=None, text=""):
    r = SimpleNamespace(status_code=status, text=text or json.dumps(data or {}))
    r.json = lambda: data
    return r


def gemini_ok(text=GOOD_JSON, finish="STOP"):
    return http_reply(200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]})


def extractor_with(*replies):
    session = MagicMock()
    session.post.side_effect = list(replies)
    sleeps = []
    return GeminiExtractor("secret-key", "gemini-2.5-flash", IST, session=session, sleep=sleeps.append), session, sleeps


def test_gemini_reads_summary_and_deadline():
    ex, session, _ = extractor_with(gemini_ok())
    result = ex.extract(EMAIL, NOW)
    assert result.used_fallback is False
    assert result.topic == "Acme Corp"
    assert result.deadline == datetime(2026, 11, 25, 17, 0, tzinfo=IST)


def test_gemini_request_shape_and_key_not_in_url():
    ex, session, _ = extractor_with(gemini_ok())
    ex.extract(EMAIL, NOW)
    args, kwargs = session.post.call_args
    url = args[0]
    assert "gemini-2.5-flash:generateContent" in url
    assert "secret-key" not in url
    assert kwargs["headers"]["x-goog-api-key"] == "secret-key"
    body = kwargs["json"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "Acme Corp hiring drive" in body["contents"][0]["parts"][0]["text"]
    assert "Asia/Kolkata" in body["systemInstruction"]["parts"][0]["text"]


def test_gemini_retries_on_rate_limit_then_works():
    ex, session, sleeps = extractor_with(http_reply(429, text="slow down"), gemini_ok())
    result = ex.extract(EMAIL, NOW)
    assert result.used_fallback is False
    assert session.post.call_count == 2
    assert len(sleeps) == 1


def test_gemini_bad_key_falls_back_to_subject():
    ex, session, _ = extractor_with(http_reply(400, text='{"error": "API key not valid"}'))
    result = ex.extract(EMAIL, NOW)
    assert result.used_fallback is True
    assert result.summary == "Acme Corp hiring drive"
    assert result.deadline is None
    assert session.post.call_count == 1  # a bad key is not retried


def test_gemini_network_error_falls_back_after_retries():
    ex, session, _ = extractor_with(OSError("no internet"), OSError("no internet"), OSError("no internet"))
    result = ex.extract(EMAIL, NOW)
    assert result.used_fallback is True
    assert session.post.call_count == 3


def test_gemini_blocked_or_empty_falls_back():
    ex, _, _ = extractor_with(http_reply(200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    assert ex.extract(EMAIL, NOW).used_fallback is True
    ex, _, _ = extractor_with(http_reply(200, {"candidates": []}))
    assert ex.extract(EMAIL, NOW).used_fallback is True
    ex, _, _ = extractor_with(gemini_ok(finish="MAX_TOKENS"))
    assert ex.extract(EMAIL, NOW).used_fallback is True


def test_gemini_bad_json_falls_back():
    ex, _, _ = extractor_with(gemini_ok(text="not json"))
    assert ex.extract(EMAIL, NOW).used_fallback is True


def test_make_extractor_picks_provider():
    base = dict(tz=IST, anthropic_api_key="a", claude_model="m", gemini_api_key="g", gemini_model="gemini-2.5-flash")
    assert isinstance(make_extractor(SimpleNamespace(ai_provider="gemini", **base)), GeminiExtractor)
    assert isinstance(make_extractor(SimpleNamespace(ai_provider="claude", **base)), AIExtractor)
