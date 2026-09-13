"""Real Google adapters. Same interfaces as the mocks. Untested in the sandbox — test on your machine
with `python -m scripts.smoke_google` after OAuth (see README).

pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
"""
from __future__ import annotations

import base64
import io
import json
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

import threading

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

from .. import config
from ..models import LedgerRow
from .base import Message

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",          # read fixtures + write appeal letters
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.events",
]


_tls = threading.local()


def _http(creds):
    """httplib2 connections are NOT thread-safe and FastAPI runs each request on a worker thread.
    Give every thread its own authorised Http and pass it to every .execute(http=...)."""
    h = getattr(_tls, "http", None)
    if h is None:
        h = AuthorizedHttp(creds, http=httplib2.Http(timeout=60))
        _tls.http = h
    return h


def get_creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    tok = Path(config.GOOGLE_TOKEN)
    creds = Credentials.from_authorized_user_file(str(tok), SCOPES) if tok.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(config.GOOGLE_CREDENTIALS, SCOPES).run_local_server(port=0)
        tok.write_text(creds.to_json(), encoding="utf-8")
    return creds


class KeyStore:
    """Local idempotency memory for APIs without a native idempotency key (Gmail)."""
    def __init__(self, path: Path = config.ROOT / ".sent_keys.json"):
        self.path = path
        self.keys: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get(self, k: str):
        return self.keys.get(k)

    def put(self, k: str, v: Any):
        self.keys[k] = v
        self.path.write_text(json.dumps(self.keys, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- Gmail

class Gmail:
    """Pending = has the denials label and NOT the '<label>-processed' label. We don't rely on UNREAD
    because mail you send to yourself is often already read, and a human opening a denial must not
    hide it from the agent."""
    def __init__(self, creds, label: str = config.GMAIL_LABEL):
        self.creds = creds
        self.svc = build("gmail", "v1", credentials=creds)
        self.label = label
        self.done_label = f"{label}-processed"
        self.keys = KeyStore()
        self._done_id = self._ensure_label(self.done_label)

    def _ensure_label(self, name: str) -> str:
        labels = self.svc.users().labels().list(userId="me").execute(http=_http(self.creds)).get("labels", [])
        for l in labels:
            if l["name"].lower() == name.lower():
                return l["id"]
        return self.svc.users().labels().create(userId="me", body={"name": name, "labelListVisibility": "labelShow",
                                                                  "messageListVisibility": "show"}).execute(http=_http(self.creds))["id"]

    def list_unread(self) -> list[Message]:
        q = f"label:{self.label} -label:{self.done_label}" if self.label else f"is:unread -label:{self.done_label}"
        res = self.svc.users().messages().list(userId="me", q=q, maxResults=20).execute(http=_http(self.creds))
        out = []
        for m in res.get("messages", []):
            full = self.svc.users().messages().get(userId="me", id=m["id"], format="full").execute(http=_http(self.creds))
            headers = {h["name"].lower(): h["value"] for h in full["payload"].get("headers", [])}
            out.append(Message(id=m["id"], subject=headers.get("subject", ""), sender=headers.get("from", ""),
                               received=headers.get("date", ""), body=_body(full["payload"])))
        return out

    def mark_processed(self, msg_id: str) -> None:
        if msg_id.startswith("fx-"):            # synthetic message from /ingest-fixture, not in Gmail
            return
        self.svc.users().messages().modify(userId="me", id=msg_id,
                                           body={"addLabelIds": [self._done_id], "removeLabelIds": ["UNREAD"]}).execute(http=_http(self.creds))

    def send(self, to, subject, body, idempotency_key):
        if (prev := self.keys.get(idempotency_key)):
            return {**prev, "deduped": True}
        msg = MIMEText(body)
        msg["to"], msg["subject"] = to, subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = self.svc.users().messages().send(userId="me", body={"raw": raw}).execute(http=_http(self.creds))
        rec = {"id": sent["id"], "to": to, "subject": subject}
        self.keys.put(idempotency_key, rec)
        return rec


def _body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
    for part in payload.get("parts", []) or []:
        t = _body(part)
        if t:
            return t
    return ""


# ---------------------------------------------------------------- Drive

class Drive:
    """Maps 'notes/C-1003.md' to <root folder>/notes/C-1003.md on Drive."""
    def __init__(self, creds, root_folder_id: str = config.DRIVE_FOLDER_ID):
        self.creds = creds
        self.svc = build("drive", "v3", credentials=creds)
        self.root = root_folder_id
        self._cache: dict[str, Optional[dict]] = {}

    def _find(self, path: str) -> Optional[dict]:
        if path in self._cache:
            return self._cache[path]
        parent = self.root
        node = None
        for part in path.split("/"):
            q = f"name = '{part}' and '{parent}' in parents and trashed = false"
            res = self.svc.files().list(q=q, fields="files(id,name,mimeType)", pageSize=1).execute(http=_http(self.creds)).get("files", [])
            if not res:
                self._cache[path] = None
                return None
            node = res[0]
            parent = node["id"]
        self._cache[path] = node
        return node

    def exists(self, path: str) -> bool:
        return self._find(path) is not None

    def _ensure_folder(self, parts: list[str]) -> str:
        parent = self.root
        for part in parts:
            q = f"name = '{part}' and '{parent}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            res = self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute(http=_http(self.creds)).get("files", [])
            parent = res[0]["id"] if res else self.svc.files().create(
                body={"name": part, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]}, fields="id").execute(http=_http(self.creds))["id"]
        return parent

    def write_text(self, path: str, content: str, idempotency_key: str = ""):
        """Create or overwrite <root>/<path> as a plain-text file. Same path twice = overwrite, so it's idempotent."""
        from googleapiclient.http import MediaIoBaseUpload
        *folders, name = path.split("/")
        parent = self._ensure_folder(folders)
        media = MediaIoBaseUpload(io.BytesIO(content.encode()), mimetype="text/plain", resumable=False)
        existing = self._find(path)
        if existing:
            f = self.svc.files().update(fileId=existing["id"], media_body=media, fields="id,webViewLink").execute(http=_http(self.creds))
            return {"id": f["id"], "link": f.get("webViewLink"), "deduped": True}
        f = self.svc.files().create(body={"name": name, "parents": [parent]}, media_body=media, fields="id,webViewLink").execute(http=_http(self.creds))
        self._cache[path] = {"id": f["id"], "name": name, "mimeType": "text/plain"}
        return {"id": f["id"], "link": f.get("webViewLink")}

    def read_text(self, path: str) -> str:
        f = self._find(path)
        if not f:
            raise FileNotFoundError(path)
        if f["mimeType"] == "application/vnd.google-apps.document":
            req = self.svc.files().export_media(fileId=f["id"], mimeType="text/plain")
        else:
            req = self.svc.files().get_media(fileId=f["id"])
        data = req.execute(http=_http(self.creds))          # bytes; small files, no chunked download needed
        return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- Sheets

class Sheets:
    def __init__(self, creds, sheet_id: str = config.SHEET_ID, tab: str = config.SHEET_TAB):
        self.creds = creds
        self.svc = build("sheets", "v4", credentials=creds)
        self.sheet_id, self.tab = sheet_id, tab
        self.cols = list(LedgerRow.model_fields)

    def _rows(self) -> list[list[str]]:
        res = self.svc.spreadsheets().values().get(spreadsheetId=self.sheet_id, range=f"{self.tab}!A1:N1000").execute(http=_http(self.creds))
        return res.get("values", [])

    def all(self) -> list[LedgerRow]:
        rows = self._rows()
        if not rows:
            return []
        hdr = rows[0]
        return [LedgerRow(**{h: (r[i] if i < len(r) else "") for i, h in enumerate(hdr) if h in self.cols}) for r in rows[1:] if r]

    def get(self, claim_id: str) -> Optional[LedgerRow]:
        return next((r for r in self.all() if r.claim_id == claim_id), None)

    def update(self, row: LedgerRow) -> None:
        rows = self._rows()
        hdr = rows[0]
        idx = next((i for i, r in enumerate(rows[1:], start=2) if r and r[0] == row.claim_id), None)
        dump = row.model_dump()
        values = [["" if dump.get(h) is None else str(dump.get(h, "")) for h in hdr]]
        if idx is None:   # append new
            self.svc.spreadsheets().values().append(spreadsheetId=self.sheet_id, range=f"{self.tab}!A1",
                                                    valueInputOption="RAW", body={"values": values}).execute(http=_http(self.creds))
        else:
            self.svc.spreadsheets().values().update(spreadsheetId=self.sheet_id, range=f"{self.tab}!A{idx}",
                                                    valueInputOption="RAW", body={"values": values}).execute(http=_http(self.creds))


# ---------------------------------------------------------------- Calendar

class GCalendar:
    def __init__(self, creds, calendar_id: str = config.CALENDAR_ID):
        self.creds = creds
        self.svc = build("calendar", "v3", credentials=creds)
        self.cal = calendar_id

    def create_event(self, title, day, description, idempotency_key):
        # Native idempotency: look up by private extended property before inserting.
        existing = self.svc.events().list(calendarId=self.cal, privateExtendedProperty=f"key={idempotency_key}",
                                          maxResults=1).execute(http=_http(self.creds)).get("items", [])
        if existing:
            return {"id": existing[0]["id"], "deduped": True}
        body = {"summary": title, "description": description,
                "start": {"date": day}, "end": {"date": day},
                "extendedProperties": {"private": {"key": idempotency_key}},
                "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 24 * 60}]}}
        ev = self.svc.events().insert(calendarId=self.cal, body=body).execute(http=_http(self.creds))
        return {"id": ev["id"], "htmlLink": ev.get("htmlLink")}