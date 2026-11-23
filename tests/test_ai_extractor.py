import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import anthropic
import httpx2 as httpx

from app.ai_extractor import AIExtractor, parse_ai_response, parse_deadline
from app.gmail_client import Email

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=IST)
EMAIL = Email("m1", "careers@example.edu", "Acme Corp hiring drive", "Register by 25 Nov 5 PM", NOW)


def reply(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


def extractor_with(response=None, error=None):
    client = MagicMock()
    if error:
        client.messages.create.side_effect = error
    else:
        client.messages.create.return_value = response
    return AIExtractor("key", "claude-haiku-5-5", IST, client=client), client


# ---------- parsing ----------

def test_valid_json_is_parsed():
    text = json.dumps({
        "summary": "Acme Corp drive for 2027 batch.",
        "topic": "Acme Corp",
        "action_required": "Register on portal",
        "deadline": "2026-11-25T17:00:00+05:30",
    })
    result = parse_ai_response(text, EMAIL, IST)
    assert result.used_fallback is False
    assert result.topic == "Acme Corp"
    assert result.action_required == "Register on portal"
    assert result.deadline == datetime(2026, 11, 25, 17, 0, tzinfo=IST)


def test_invalid_json_falls_back_to_subject():
    result = parse_ai_response("not json {", EMAIL, IST)
    assert result.used_fallback is True
    assert result.summary == "Acme Corp hiring drive"
    assert result.deadline is None


def test_json_that_is_not_object_falls_back():
    assert parse_ai_response("[1, 2]", EMAIL, IST).used_fallback is True


def test_empty_summary_falls_back():
    text = json.dumps({"summary": "  ", "topic": None, "action_required": None, "deadline": None})
    assert parse_ai_response(text, EMAIL, IST).used_fallback is True


def test_long_summary_is_cut_to_300():
    text = json.dumps({"summary": "x" * 500, "topic": None, "action_required": None, "deadline": None})
    result = parse_ai_response(text, EMAIL, IST)
    assert len(result.summary) <= 300


def test_null_deadline_and_empty_strings():
    text = json.dumps({"summary": "ok", "topic": "", "action_required": None, "deadline": None})
    result = parse_ai_response(text, EMAIL, IST)
    assert result.deadline is None
    assert result.topic is None


def test_bad_deadline_becomes_none_but_summary_kept():
    text = json.dumps({"summary": "ok", "topic": None, "action_required": None, "deadline": "next Friday"})
    result = parse_ai_response(text, EMAIL, IST)
    assert result.used_fallback is False
    assert result.deadline is None


def test_deadline_without_offset_is_treated_as_ist():
    assert parse_deadline("2026-11-25T17:00:00", IST) == datetime(2026, 11, 25, 17, 0, tzinfo=IST)


def test_utc_deadline_is_converted_to_ist():
    dt = parse_deadline("2026-11-25T11:30:00Z", IST)
    assert dt == datetime(2026, 11, 25, 17, 0, tzinfo=IST)
    assert dt.utcoffset().total_seconds() == 5.5 * 3600


# ---------- API calls (mocked, no real network) ----------

def test_extract_calls_claude_with_schema():
    text = json.dumps({"summary": "s", "topic": "Globex", "action_required": None, "deadline": None})
    ex, client = extractor_with(reply(text))
    result = ex.extract(EMAIL, NOW)
    assert result.topic == "Globex"
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert "Acme Corp hiring drive" in kwargs["messages"][0]["content"]


def test_extract_api_error_falls_back():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(request=request)
    ex, _ = extractor_with(error=error)
    result = ex.extract(EMAIL, NOW)
    assert result.used_fallback is True
    assert result.summary == "Acme Corp hiring drive"


def test_extract_status_error_falls_back():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(500, request=request)
    error = anthropic.InternalServerError("boom", response=response, body=None)
    ex, _ = extractor_with(error=error)
    assert ex.extract(EMAIL, NOW).used_fallback is True


def test_extract_refusal_falls_back():
    ex, _ = extractor_with(reply("", stop_reason="refusal"))
    assert ex.extract(EMAIL, NOW).used_fallback is True


def test_extract_unexpected_error_falls_back():
    ex, _ = extractor_with(error=ValueError("weird"))
    assert ex.extract(EMAIL, NOW).used_fallback is True


def test_received_time_is_sent_in_ist():
    text = json.dumps({"summary": "s", "topic": None, "action_required": None, "deadline": None})
    ex, client = extractor_with(reply(text))
    email = Email("m2", "a@x.com", "subj", "body", datetime(2026, 11, 23, 4, 30, tzinfo=timezone.utc))
    ex.extract(email, NOW)
    assert "2026-11-23T10:00:00+05:30" in client.messages.create.call_args.kwargs["messages"][0]["content"]


def test_prompt_is_general_and_uses_timezone():
    ex, _ = extractor_with(reply("{}"))
    assert "placement" not in ex.system_prompt.lower()
    assert "Asia/Kolkata" in ex.system_prompt
    assert "+05:30" in ex.system_prompt
    other_tz = AIExtractor("key", "m", ZoneInfo("Europe/London"), client=MagicMock())
    assert "Europe/London" in other_tz.system_prompt
