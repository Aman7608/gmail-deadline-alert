import pytest

from app.config import ConfigError, load_settings

ENV_KEYS = [
    "WATCH_SENDER", "TWILIO_WHATSAPP_FROM", "MY_WHATSAPP_TO", "TWILIO_CONTENT_VARIABLES",
    "POLL_SECONDS", "CLAUDE_MODEL", "TIMEZONE", "ANTHROPIC_API_KEY", "TWILIO_ACCOUNT_SID",
    "AI_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "WHATSAPP_ENABLED", "TWILIO_AUTH_TOKEN",
    "TWILIO_VOICE_FROM", "MY_PHONE_NUMBER",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_and_multiple_senders(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("WATCH_SENDER= Careers@Example.edu , jobs@example.com\nMY_WHATSAPP_TO=+919999999999\n")
    s = load_settings(env)
    assert s.watch_senders == ["careers@example.edu", "jobs@example.com"]
    assert s.my_whatsapp_to == "whatsapp:+919999999999"
    assert s.claude_model == "claude-haiku-5-5"
    assert s.poll_seconds == 60
    assert s.no_deadline_call_minutes == 60
    assert str(s.tz) == "Asia/Kolkata"
    assert s.twilio_content_variables == {"1": "summary", "2": "deadline", "3": "action"}


def test_bad_number_raises(tmp_path):
    env = tmp_path / ".env"
    env.write_text("POLL_SECONDS=abc\n")
    with pytest.raises(ConfigError):
        load_settings(env)


def test_bad_content_variables_raises(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TWILIO_CONTENT_VARIABLES=not json\n")
    with pytest.raises(ConfigError):
        load_settings(env)


def test_check_ready_lists_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("WATCH_SENDER=a@b.com\n")
    s = load_settings(env)
    with pytest.raises(ConfigError) as err:
        s.check_ready()
    assert "ANTHROPIC_API_KEY" in str(err.value)
    assert "TWILIO_ACCOUNT_SID" in str(err.value)
    # Dry run does not need Twilio
    s.anthropic_api_key = "x"
    s.check_ready(need_twilio=False)


def test_watch_query_alone_is_enough(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCH_QUERY", raising=False)
    monkeypatch.delenv("ALERT_TITLE", raising=False)
    env = tmp_path / ".env"
    env.write_text("WATCH_QUERY=subject:(interview OR deadline)\nANTHROPIC_API_KEY=x\nALERT_TITLE=Job alert\n")
    s = load_settings(env)
    assert s.watch_senders == []
    assert s.watch_query == "subject:(interview OR deadline)"
    assert s.alert_title == "Job alert"
    s.check_ready(need_twilio=False)  # must not complain about WATCH_SENDER


def test_need_sender_or_query(tmp_path, monkeypatch):
    monkeypatch.delenv("WATCH_QUERY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=x\n")
    with pytest.raises(ConfigError) as err:
        load_settings(env).check_ready(need_twilio=False)
    assert "WATCH_SENDER or WATCH_QUERY" in str(err.value)


def test_gemini_provider_needs_gemini_key_not_claude(tmp_path, monkeypatch):
    for key in ("AI_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "WHATSAPP_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("WATCH_SENDER=a@b.com\nAI_PROVIDER=Gemini\n")
    s = load_settings(env)
    assert s.ai_provider == "gemini"
    assert s.gemini_model == "gemini-2.5-flash"
    with pytest.raises(ConfigError) as err:
        s.check_ready(need_twilio=False)
    assert "GEMINI_API_KEY" in str(err.value)
    assert "ANTHROPIC_API_KEY" not in str(err.value)
    s.gemini_api_key = "g"
    s.check_ready(need_twilio=False)


def test_bad_ai_provider_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    env = tmp_path / ".env"
    env.write_text("AI_PROVIDER=chatgpt\n")
    with pytest.raises(ConfigError):
        load_settings(env)


def test_whatsapp_off_does_not_need_whatsapp_numbers(tmp_path, monkeypatch):
    for key in ("WHATSAPP_ENABLED", "MY_WHATSAPP_TO", "TWILIO_AUTH_TOKEN", "TWILIO_VOICE_FROM", "MY_PHONE_NUMBER"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("WATCH_SENDER=a@b.com\nANTHROPIC_API_KEY=x\nWHATSAPP_ENABLED=false\nTWILIO_ACCOUNT_SID=AC1\n"
                   "TWILIO_AUTH_TOKEN=t\nTWILIO_VOICE_FROM=+1555\nMY_PHONE_NUMBER=+91999\n")
    s = load_settings(env)
    assert s.whatsapp_enabled is False
    s.check_ready()  # no MY_WHATSAPP_TO needed


def test_bad_whatsapp_enabled_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("WHATSAPP_ENABLED", raising=False)
    env = tmp_path / ".env"
    env.write_text("WHATSAPP_ENABLED=maybe\n")
    with pytest.raises(ConfigError):
        load_settings(env)
