from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import Database

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=IST)


def add(db, gmail_id="m1", **kw):
    data = dict(
        gmail_id=gmail_id, sender="a@x.com", subject="Drive", summary="Sum", topic="Acme Corp",
        action_required="Register", deadline=NOW + timedelta(days=1), received_at=NOW,
        processed_at=NOW, call_at=NOW + timedelta(hours=12), call_status="pending",
    )
    data.update(kw)
    return db.add_alert(**data)


def test_duplicate_email_is_ignored():
    db = Database(":memory:")
    first = add(db)
    second = add(db)
    assert first is not None
    assert second is None
    assert len(db.all_alerts()) == 1


def test_duplicate_protection_survives_restart(tmp_path):
    path = tmp_path / "alerts.db"
    db = Database(path)
    add(db)
    db.close()
    db2 = Database(path)  # like restarting the program
    assert db2.has_email("m1")
    assert add(db2) is None


def test_times_round_trip_with_timezone():
    db = Database(":memory:")
    alert = db.get(add(db))
    assert alert.deadline == NOW + timedelta(days=1)
    assert alert.deadline.tzinfo is not None


def test_claim_call_only_once():
    db = Database(":memory:")
    alert_id = add(db)
    assert db.claim_call(alert_id, NOW) is True
    assert db.claim_call(alert_id, NOW) is False
    assert db.get(alert_id).call_status == "calling"


def test_whatsapp_failed_sends_give_up_after_max():
    db = Database(":memory:")
    alert_id = add(db)
    db.set_whatsapp_send_failed(alert_id, 3, "network", NOW)
    assert db.get(alert_id).whatsapp_status == "not_sent"
    db.set_whatsapp_send_failed(alert_id, 3, "network", NOW)
    db.set_whatsapp_send_failed(alert_id, 3, "network", NOW)
    assert db.get(alert_id).whatsapp_status == "error"
    assert db.unsent_whatsapp(3) == []


def test_pending_calls_list():
    db = Database(":memory:")
    a = add(db, "m1")
    add(db, "m2", call_status="none", call_at=None)
    assert [x.id for x in db.pending_calls()] == [a]


def test_old_database_with_company_column_is_upgraded(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, gmail_id TEXT NOT NULL UNIQUE, "
                 "sender TEXT, subject TEXT, summary TEXT, company TEXT, action_required TEXT, deadline TEXT, "
                 "received_at TEXT, processed_at TEXT NOT NULL, whatsapp_sid TEXT, "
                 "whatsapp_status TEXT NOT NULL DEFAULT 'not_sent', whatsapp_attempts INTEGER NOT NULL DEFAULT 0, "
                 "call_at TEXT, call_status TEXT NOT NULL, call_sid TEXT, call_attempts INTEGER NOT NULL DEFAULT 0, "
                 "note TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                 "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    db = Database(path)
    alert_id = add(db)
    assert db.get(alert_id).topic == "Acme Corp"
