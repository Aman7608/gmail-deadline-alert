"""Email alert system: watch Gmail, summarise with Claude, send WhatsApp, call if unread.

Usage:
  python -m app.main               run forever (checks every POLL_SECONDS)
  python -m app.main --once        run one check and exit
  python -m app.main --dry-run     read real mail, print what would be sent, send nothing
  python -m app.main --test-alert  send one test WhatsApp and one test call
  python -m app.main --auth        only do the Gmail login and save token.json
  python -m app.main --status      show the saved alerts
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime

from app.ai_extractor import make_extractor
from app.config import ConfigError, Settings, load_settings
from app.db import Alert, Database
from app.gmail_client import Email, GmailAuthError, GmailClient, load_gmail_service
from app.logging_setup import setup_logging
from app.notifier import FAILED_STATUSES, READ_STATUS, AlertContent, TwilioNotifier, build_whatsapp_text
from app.timing import compute_call_time, is_call_due

log = logging.getLogger("app")

MAX_WHATSAPP_ATTEMPTS = 3
MAX_CALL_ATTEMPTS = 3


def _is_twilio_rejection(error: Exception) -> bool:
    """True when Twilio answered with an error, so we know no call was created."""
    try:
        from twilio.base.exceptions import TwilioRestException
    except ImportError:
        return False
    return isinstance(error, TwilioRestException)


def content_from_alert(alert: Alert) -> AlertContent:
    return AlertContent(
        subject=alert.subject or "",
        summary=alert.summary or alert.subject or "New email",
        topic=alert.topic,
        action_required=alert.action_required,
        deadline=alert.deadline,
        sender=alert.sender or "",
    )


class AlertApp:
    def __init__(self, settings: Settings, db: Database, gmail_factory, extractor, notifier,
                 dry_run: bool = False, now_fn=None, out=print, fail_fast: bool = False):
        self.s = settings
        self.fail_fast = fail_fast  # stop on Gmail login problems (used by --once and --dry-run)
        self.db = db
        self.gmail_factory = gmail_factory
        self.gmail = None
        self.extractor = extractor
        self.notifier = notifier
        self.dry_run = dry_run
        self._now_fn = now_fn
        self.out = out

    def now(self) -> datetime:
        if self._now_fn:
            return self._now_fn()
        return datetime.now(self.s.tz)

    # ---------- step 1: new mail ----------

    def check_new_mail(self) -> int:
        if self.gmail is None:
            self.gmail = self.gmail_factory()
        new_count = 0
        for gmail_id in self.gmail.list_new_message_ids(self.now()):
            if self.db.has_email(gmail_id):
                continue
            try:
                email = self.gmail.get_email(gmail_id)
            except Exception as e:
                log.error("Could not download email %s (will retry next time): %s", gmail_id, e)
                continue
            if self.process_email(email):
                new_count += 1
        return new_count

    def process_email(self, email: Email) -> bool:
        """Summarise one email, plan the call and send WhatsApp. Returns False for duplicates."""
        if self.db.has_email(email.gmail_id):
            return False
        log.info("New email %s: %r", email.gmail_id, email.subject)

        processed_at = self.now()
        extraction = self.extractor.extract(email, processed_at)
        plan = compute_call_time(extraction.deadline, processed_at, self.s.no_deadline_call_minutes)

        alert_id = self.db.add_alert(
            gmail_id=email.gmail_id,
            sender=email.sender,
            subject=email.subject,
            summary=extraction.summary,
            topic=extraction.topic,
            action_required=extraction.action_required,
            deadline=extraction.deadline,
            received_at=email.received_at,
            processed_at=processed_at,
            call_at=plan.call_at,
            call_status="pending" if plan.should_call else "none",
            note=plan.reason + (" (AI fallback used)" if extraction.used_fallback else ""),
        )
        if alert_id is None:  # another loop already saved it
            return False

        alert = self.db.get(alert_id)
        log.info("Alert %d planned: %s, call_at=%s", alert_id, plan.reason, plan.call_at)

        if self.dry_run:
            self._print_dry_run(alert, plan.reason)
            return True

        self.send_whatsapp(alert)
        return True

    def _print_dry_run(self, alert: Alert, reason: str) -> None:
        text = build_whatsapp_text(content_from_alert(alert), self.s.tz, alert.call_at,
                                   getattr(self.s, "alert_title", "Email alert"))
        call = alert.call_at.astimezone(self.s.tz).strftime("%a, %d %b %Y, %I:%M %p %Z") if alert.call_at else "no call"
        self.out("=" * 60)
        self.out(f"[DRY RUN] Email: {alert.subject}")
        if self._whatsapp_on():
            self.out("WhatsApp text that would be sent:")
        else:
            self.out("WhatsApp is turned off (WHATSAPP_ENABLED=false). Summary:")
        self.out(text)
        self.out(f"Call would be placed at: {call}  (rule: {reason})")

    def _whatsapp_on(self) -> bool:
        return getattr(self.s, "whatsapp_enabled", True)

    def send_whatsapp(self, alert: Alert) -> None:
        if not self._whatsapp_on():
            # Calls only: mark it so it is never retried. The call still happens on time.
            self.db.set_whatsapp_status(alert.id, "disabled", self.now())
            return
        try:
            sid, status = self.notifier.send_whatsapp(content_from_alert(alert), alert.call_at)
            self.db.set_whatsapp_sent(alert.id, sid, status, self.now())
            if status in FAILED_STATUSES:
                log.warning("WhatsApp for alert %d failed right away (%s); the call will still happen", alert.id, status)
        except Exception as e:
            log.error("Sending WhatsApp for alert %d failed: %s", alert.id, e)
            self.db.set_whatsapp_send_failed(alert.id, MAX_WHATSAPP_ATTEMPTS, f"whatsapp: {e}", self.now())

    def retry_unsent_whatsapp(self) -> None:
        if not self._whatsapp_on():
            return
        for alert in self.db.unsent_whatsapp(MAX_WHATSAPP_ATTEMPTS):
            log.info("Retrying WhatsApp for alert %d", alert.id)
            self.send_whatsapp(alert)

    # ---------- step 2: read status ----------

    def check_read_status(self) -> None:
        for alert in self.db.pending_calls():
            if not alert.whatsapp_sid or alert.whatsapp_status in FAILED_STATUSES:
                continue  # never sent or failed -> treat as unread
            try:
                status = self.notifier.get_message_status(alert.whatsapp_sid)
            except Exception as e:
                log.warning("Could not check WhatsApp status for alert %d: %s", alert.id, e)
                continue
            if status != alert.whatsapp_status:
                log.info("Alert %d WhatsApp status: %s -> %s", alert.id, alert.whatsapp_status, status)
                self.db.set_whatsapp_status(alert.id, status, self.now())
            if status == READ_STATUS:
                log.info("Alert %d was read on WhatsApp, cancelling the call", alert.id)
                self.db.set_call_status(alert.id, "cancelled_read", self.now())

    # ---------- step 3: calls ----------

    def place_due_calls(self) -> None:
        now = self.now()
        for alert in self.db.pending_calls():
            if alert.call_at is None or not is_call_due(alert.call_at, now):
                continue
            if alert.deadline is not None and now >= alert.deadline:
                # The program was probably off at call time; calling now is useless
                log.warning("Alert %d: call time was missed and the deadline has passed", alert.id)
                self.db.set_call_status(alert.id, "expired", now)
                continue
            if not self.db.claim_call(alert.id, now):
                continue  # someone else already took it
            try:
                call_sid = self.notifier.place_call(content_from_alert(alert))
                self.db.set_call_placed(alert.id, call_sid, self.now())
            except Exception as e:
                attempts = alert.call_attempts + 1
                if _is_twilio_rejection(e) and attempts < MAX_CALL_ATTEMPTS:
                    # Twilio said no, so no call exists yet. Safe to try again later.
                    log.error("Call for alert %d rejected by Twilio (try %d): %s", alert.id, attempts, e)
                    self.db.set_call_status(alert.id, "pending", self.now(), note=f"call: {e}")
                else:
                    # We do not know if the call went out (e.g. network cut). Do not risk calling twice.
                    log.error("Call for alert %d failed, giving up: %s", alert.id, e)
                    self.db.set_call_status(alert.id, "failed", self.now(), note=f"call: {e}")

    # ---------- loop ----------

    def run_once(self) -> None:
        steps = [("check new mail", self.check_new_mail)]
        if not self.dry_run:
            steps += [
                ("retry unsent WhatsApp", self.retry_unsent_whatsapp),
                ("check read status", self.check_read_status),
                ("place due calls", self.place_due_calls),
            ]
        for name, step in steps:
            try:
                step()
            except GmailAuthError as e:
                if self.fail_fast:
                    raise
                # Keep running: pending calls for older alerts must still happen
                log.error("Gmail login problem, new mail cannot be checked: %s", e)
                self.gmail = None
            except Exception as e:
                log.exception("Step '%s' failed, will try again next round: %s", name, e)
                if name == "check new mail":
                    self.gmail = None  # rebuild the Gmail connection next time

    def run_forever(self) -> None:
        watching = ", ".join(self.s.watch_senders) or "any sender"
        if getattr(self.s, "watch_query", ""):
            watching += f" matching '{self.s.watch_query}'"
        log.info("Watching Gmail for %s every %ss", watching, self.s.poll_seconds)
        while True:
            started = time.monotonic()
            self.run_once()
            time.sleep(max(1.0, self.s.poll_seconds - (time.monotonic() - started)))


# ---------- command line ----------

def make_gmail_factory(settings: Settings):
    allow_browser = os.getenv("HEADLESS", "").strip() not in ("1", "true", "yes")

    def factory():
        service = load_gmail_service(settings.gmail_credentials_file, settings.gmail_token_file, allow_browser)
        return GmailClient(service, settings.watch_senders, settings.lookback_hours, settings.watch_query)

    return factory


def run_test_alert(settings: Settings, notifier) -> None:
    now = datetime.now(settings.tz)
    content = AlertContent(
        subject="Test alert",
        summary="This is a test from your email alert system. If you can read and hear this, your setup works.",
        topic="Test",
        action_required="Nothing, this is only a test",
        deadline=None,
    )
    if not getattr(settings, "whatsapp_enabled", True):
        print("WhatsApp is turned off (WHATSAPP_ENABLED=false), skipping it.")
    else:
        _test_whatsapp(notifier, content)
    print("Placing test call...")
    call_sid = notifier.place_call(content)
    print(f"  Call sid={call_sid}. Your phone should ring now ({now:%I:%M %p}).")


def _test_whatsapp(notifier, content: AlertContent) -> None:
    print("Sending test WhatsApp...")
    try:
        sid, status = notifier.send_whatsapp(content)
        print(f"  WhatsApp sid={sid} status={status}")
        time.sleep(5)
        try:
            print(f"  Status after 5 seconds: {notifier.get_message_status(sid)}")
        except Exception as e:
            print(f"  Could not read status: {e}")
    except Exception as e:
        # Same rule as the real app: if WhatsApp fails, still make the call.
        print(f"  WhatsApp FAILED: {e}")
        print("  Tip: send the sandbox 'join ...' code (or 'hi') from your WhatsApp to the sandbox number, then try again.")
        print("  On a Twilio 'Limited trial' account custom WhatsApp text is blocked; upgrade to send real alerts.")


def print_status(db: Database, settings: Settings) -> None:
    alerts = db.all_alerts()
    if not alerts:
        print("No alerts saved yet.")
        return
    fmt = "%d %b %I:%M %p"
    for a in alerts:
        deadline = a.deadline.astimezone(settings.tz).strftime(fmt) if a.deadline else "-"
        call_at = a.call_at.astimezone(settings.tz).strftime(fmt) if a.call_at else "-"
        print(f"#{a.id} {(a.subject or '')[:50]!r}\n    deadline={deadline} whatsapp={a.whatsapp_status} "
              f"call_at={call_at} call={a.call_status}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gmail-deadline-alert", description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="read mail and print what would be sent; send nothing")
    group.add_argument("--test-alert", action="store_true", help="send one test WhatsApp and one test call")
    group.add_argument("--once", action="store_true", help="run one check and exit")
    group.add_argument("--auth", action="store_true", help="only log in to Gmail and save token.json")
    group.add_argument("--status", action="store_true", help="list saved alerts")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        setup_logging(settings.log_file, settings.log_level)

        if args.auth:
            load_gmail_service(settings.gmail_credentials_file, settings.gmail_token_file, allow_browser=True)
            print(f"Gmail login OK. Token saved to {settings.gmail_token_file}")
            return 0

        if args.status:
            print_status(Database(settings.db_path), settings)
            return 0

        if args.test_alert:
            settings.check_ready(need_gmail=False, need_ai=False, need_twilio=True)
            run_test_alert(settings, TwilioNotifier(settings))
            return 0

        if args.dry_run:
            settings.check_ready(need_twilio=False)
            # In-memory database, so a dry run never blocks real alerts later
            app = AlertApp(settings, Database(":memory:"), make_gmail_factory(settings),
                           make_extractor(settings),
                           notifier=None, dry_run=True, fail_fast=True)
            app.run_once()
            print("Dry run finished. Nothing was sent.")
            return 0

        settings.check_ready()
        app = AlertApp(settings, Database(settings.db_path), make_gmail_factory(settings),
                       make_extractor(settings),
                       TwilioNotifier(settings), fail_fast=args.once)
        if args.once:
            app.run_once()
        else:
            app.run_forever()
        return 0
    except ConfigError as e:
        print(f"Config problem: {e}", file=sys.stderr)
        return 2
    except GmailAuthError as e:
        print(f"Gmail login problem: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
