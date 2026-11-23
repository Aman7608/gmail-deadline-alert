"""SQLite storage for alerts. One row per Gmail message.

gmail_id is UNIQUE, so the same email can never create two alerts,
even after the program restarts.

call_status values:
  pending         - a call is planned at call_at
  calling         - we are placing the call right now (set before calling)
  placed          - Twilio accepted the call
  failed          - the call could not be placed
  cancelled_read  - WhatsApp was read before call_at, so no call
  none            - no call needed (deadline already passed)
  expired         - call time was missed and the deadline is now over
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_id           TEXT NOT NULL UNIQUE,
    sender             TEXT,
    subject            TEXT,
    summary            TEXT,
    topic            TEXT,
    action_required    TEXT,
    deadline           TEXT,
    received_at        TEXT,
    processed_at       TEXT NOT NULL,
    whatsapp_sid       TEXT,
    whatsapp_status    TEXT NOT NULL DEFAULT 'not_sent',
    whatsapp_attempts  INTEGER NOT NULL DEFAULT 0,
    call_at            TEXT,
    call_status        TEXT NOT NULL,
    call_sid           TEXT,
    call_attempts      INTEGER NOT NULL DEFAULT 0,
    note               TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_call_status ON alerts(call_status);
"""


@dataclass
class Alert:
    id: int
    gmail_id: str
    sender: str | None
    subject: str | None
    summary: str | None
    topic: str | None
    action_required: str | None
    deadline: datetime | None
    received_at: datetime | None
    processed_at: datetime
    whatsapp_sid: str | None
    whatsapp_status: str
    whatsapp_attempts: int
    call_at: datetime | None
    call_status: str
    call_sid: str | None
    call_attempts: int
    note: str | None


def _to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _to_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: Path | str):
        # ":memory:" is used for dry runs and tests
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._upgrade_old_database()

    def _upgrade_old_database(self):
        """Databases made by older versions called the 'topic' column 'company'."""
        columns = [row[1] for row in self._conn.execute("PRAGMA table_info(alerts)")]
        if "company" in columns and "topic" not in columns:
            self._conn.execute("ALTER TABLE alerts RENAME COLUMN company TO topic")

    def close(self):
        self._conn.close()

    # ---------- reads ----------

    def has_email(self, gmail_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM alerts WHERE gmail_id = ?", (gmail_id,)).fetchone()
        return row is not None

    def get(self, alert_id: int) -> Alert | None:
        row = self._conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return self._row_to_alert(row) if row else None

    def get_by_gmail_id(self, gmail_id: str) -> Alert | None:
        row = self._conn.execute("SELECT * FROM alerts WHERE gmail_id = ?", (gmail_id,)).fetchone()
        return self._row_to_alert(row) if row else None

    def pending_calls(self) -> list[Alert]:
        rows = self._conn.execute(
            "SELECT * FROM alerts WHERE call_status = 'pending' ORDER BY call_at"
        ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def unsent_whatsapp(self, max_attempts: int) -> list[Alert]:
        rows = self._conn.execute(
            "SELECT * FROM alerts WHERE whatsapp_status = 'not_sent' AND whatsapp_attempts < ?",
            (max_attempts,),
        ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def all_alerts(self) -> list[Alert]:
        rows = self._conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
        return [self._row_to_alert(r) for r in rows]

    # ---------- writes ----------

    def add_alert(
        self,
        *,
        gmail_id: str,
        sender: str | None,
        subject: str | None,
        summary: str | None,
        topic: str | None,
        action_required: str | None,
        deadline: datetime | None,
        received_at: datetime | None,
        processed_at: datetime,
        call_at: datetime | None,
        call_status: str,
        note: str | None = None,
    ) -> int | None:
        """Save a new alert. Returns the new id, or None if this email was already saved."""
        now = _to_text(processed_at)
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO alerts
                   (gmail_id, sender, subject, summary, topic, action_required, deadline,
                    received_at, processed_at, call_at, call_status, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    gmail_id, sender, subject, summary, topic, action_required,
                    _to_text(deadline), _to_text(received_at), now, _to_text(call_at),
                    call_status, note, now, now,
                ),
            )
            if cur.rowcount == 0:
                return None
            return cur.lastrowid

    def _update(self, alert_id: int, now: datetime, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [(_to_text(v) if isinstance(v, datetime) else v) for v in fields.values()]
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE alerts SET {cols}, updated_at = ? WHERE id = ?",
                (*values, _to_text(now), alert_id),
            )

    def set_whatsapp_sent(self, alert_id: int, sid: str, status: str, now: datetime) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE alerts SET whatsapp_sid = ?, whatsapp_status = ?,
                   whatsapp_attempts = whatsapp_attempts + 1, updated_at = ? WHERE id = ?""",
                (sid, status, _to_text(now), alert_id),
            )

    def set_whatsapp_send_failed(self, alert_id: int, max_attempts: int, error: str, now: datetime) -> None:
        """Count a failed send. After max_attempts we give up and mark it 'error'."""
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE alerts SET whatsapp_attempts = whatsapp_attempts + 1,
                   whatsapp_status = CASE WHEN whatsapp_attempts + 1 >= ? THEN 'error' ELSE 'not_sent' END,
                   note = ?, updated_at = ? WHERE id = ?""",
                (max_attempts, error[:500], _to_text(now), alert_id),
            )

    def set_whatsapp_status(self, alert_id: int, status: str, now: datetime) -> None:
        self._update(alert_id, now, whatsapp_status=status)

    def set_call_status(self, alert_id: int, status: str, now: datetime, note: str | None = None) -> None:
        if note is None:
            self._update(alert_id, now, call_status=status)
        else:
            self._update(alert_id, now, call_status=status, note=note[:500])

    def claim_call(self, alert_id: int, now: datetime) -> bool:
        """Move a call from 'pending' to 'calling'.

        Only one caller can win this, so the same email is never called twice.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE alerts SET call_status = 'calling', call_attempts = call_attempts + 1,
                   updated_at = ? WHERE id = ? AND call_status = 'pending'""",
                (_to_text(now), alert_id),
            )
            return cur.rowcount == 1

    def set_call_placed(self, alert_id: int, call_sid: str, now: datetime) -> None:
        self._update(alert_id, now, call_status="placed", call_sid=call_sid)

    @staticmethod
    def _row_to_alert(row: sqlite3.Row) -> Alert:
        return Alert(
            id=row["id"],
            gmail_id=row["gmail_id"],
            sender=row["sender"],
            subject=row["subject"],
            summary=row["summary"],
            topic=row["topic"],
            action_required=row["action_required"],
            deadline=_to_dt(row["deadline"]),
            received_at=_to_dt(row["received_at"]),
            processed_at=_to_dt(row["processed_at"]),
            whatsapp_sid=row["whatsapp_sid"],
            whatsapp_status=row["whatsapp_status"],
            whatsapp_attempts=row["whatsapp_attempts"],
            call_at=_to_dt(row["call_at"]),
            call_status=row["call_status"],
            call_sid=row["call_sid"],
            call_attempts=row["call_attempts"],
            note=row["note"],
        )
