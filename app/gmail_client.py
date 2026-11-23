"""Reads emails from Gmail using the read-only API scope.

This module never changes the inbox: it does not mark mail as read,
move it or delete it. The only scope requested is gmail.readonly.
"""

import base64
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

from app.retry import with_retry

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class Email:
    gmail_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime  # timezone-aware


class GmailAuthError(Exception):
    pass


def build_query(senders: list[str], after: datetime, extra_query: str = "") -> str:
    """Gmail search query: mail from any of the senders, matching extra_query, newer than `after`.

    A sender can be a full address (hr@company.com) or a whole domain (@company.com).
    extra_query is any normal Gmail search, e.g. 'subject:(interview OR deadline)'.
    """
    parts = []
    senders = [s.lstrip("@") for s in senders]  # "@company.com" -> "company.com" (Gmail matches the domain)
    if len(senders) == 1:
        parts.append(f"from:{senders[0]}")
    elif senders:
        parts.append("from:(" + " OR ".join(senders) + ")")
    if extra_query.strip():
        parts.append(f"({extra_query.strip()})")
    parts.append("-in:sent -in:drafts")  # never alert on mail you wrote yourself
    parts.append(f"after:{int(after.timestamp())}")
    return " ".join(parts)


def _decode(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_body(payload: dict) -> str:
    """Find the email text. Prefers text/plain, falls back to text/html."""
    plain, html_parts = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html_parts.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html_parts:
        return html_to_text("\n".join(html_parts))
    return ""


def parse_message(msg: dict) -> Email:
    """Turn a Gmail API message (format=full) into an Email."""
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    received = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc)
    body = extract_body(msg.get("payload", {})) or msg.get("snippet", "")
    return Email(
        gmail_id=msg["id"],
        sender=parseaddr(headers.get("from", ""))[1].lower() or headers.get("from", ""),
        subject=headers.get("subject", "(no subject)"),
        body=body,
        received_at=received,
    )


def load_gmail_service(credentials_file: Path, token_file: Path, allow_browser: bool = True):
    """Log in with OAuth and return a Gmail API service object.

    The first time this opens a browser window so you can log in. After that
    the saved token.json is used (and refreshed automatically).
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing Gmail login token")
            from google.auth.exceptions import RefreshError

            try:
                creds.refresh(Request())
            except RefreshError as e:
                # Happens when the token expired or was revoked (see README: 7 day limit in Testing mode)
                raise GmailAuthError(
                    f"Gmail token could not be refreshed ({e}). Delete {token_file.name} and run "
                    "'python -m app.main --auth' again."
                )
        else:
            if not allow_browser:
                raise GmailAuthError(
                    f"No valid Gmail token at {token_file}. Run 'python -m app.main --auth' on a "
                    "computer with a browser first, then copy token.json here."
                )
            if not credentials_file.exists():
                raise GmailAuthError(
                    f"{credentials_file} not found. Download the OAuth client file from Google "
                    "Cloud Console (see README) and save it with this name."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        log.info("Saved Gmail token to %s", token_file)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


class GmailClient:
    def __init__(self, service, senders: list[str], lookback_hours: int = 24, extra_query: str = ""):
        self.service = service
        self.senders = senders
        self.lookback_hours = lookback_hours
        self.extra_query = extra_query

    def list_new_message_ids(self, now: datetime) -> list[str]:
        """Ids of messages from the watched senders inside the lookback window (oldest first)."""
        query = build_query(self.senders, now - timedelta(hours=self.lookback_hours), self.extra_query)
        ids, page_token = [], None
        while True:
            def call():
                return self.service.users().messages().list(
                    userId="me", q=query, pageToken=page_token, maxResults=100
                ).execute()

            resp = with_retry(call, what="Gmail list")
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        ids.reverse()  # Gmail returns newest first
        return ids

    def get_email(self, gmail_id: str) -> Email:
        def call():
            return self.service.users().messages().get(userId="me", id=gmail_id, format="full").execute()

        return parse_message(with_retry(call, what="Gmail get"))
