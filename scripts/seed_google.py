"""One-time: mirror ./fixtures into Drive, create the ledger sheet, and (optionally) email the
denial fixtures to yourself so the demo starts from a real inbox.

    python -m scripts.seed_google --drive --sheet --email you@gmail.com

Prints the DRIVE_FOLDER_ID and SHEET_ID to put in .env.
"""
from __future__ import annotations

import argparse
import base64
import csv
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from agent.config import FIXTURES
from agent.tools.google_apps import get_creds


def seed_drive(creds) -> str:
    svc = build("drive", "v3", credentials=creds)
    def folder(name, parent=None):
        body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            body["parents"] = [parent]
        return svc.files().create(body=body, fields="id").execute()["id"]
    root = folder("denial-agent-fixtures")
    for sub in ["claims", "notes", "policies"]:
        sid = folder(sub, root)
        for p in sorted((FIXTURES / sub).iterdir()):
            svc.files().create(body={"name": p.name, "parents": [sid]},
                               media_body=MediaFileUpload(str(p), mimetype="text/plain")).execute()
            print("uploaded", sub, p.name)
    print("\nDRIVE_FOLDER_ID=" + root)
    return root


def seed_sheet(creds) -> str:
    svc = build("sheets", "v4", credentials=creds)
    ss = svc.spreadsheets().create(body={"properties": {"title": "Claim ledger — denial agent"},
                                         "sheets": [{"properties": {"title": "ledger"}}]}).execute()
    sid = ss["spreadsheetId"]
    with (FIXTURES / "ledger.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    svc.spreadsheets().values().update(spreadsheetId=sid, range="ledger!A1", valueInputOption="RAW",
                                       body={"values": rows}).execute()
    print("\nSHEET_ID=" + sid)
    return sid


def send_denials(creds, to: str, only: list[str] | None = None):
    svc = build("gmail", "v1", credentials=creds)
    for p in sorted((FIXTURES / "denials").iterdir()):
        if only and p.stem not in only:
            continue
        text = p.read_text(encoding="utf-8")
        subject = next(l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("Subject:"))
        msg = MIMEText(text)
        msg["to"], msg["subject"] = to, subject
        svc.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}).execute()
        print("sent", p.name)
    print("\nNow create a Gmail filter: subject contains 'Claim Adjustment Notice' OR 'Remittance Advice' -> apply label 'denials'.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--drive", action="store_true")
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--email", help="send the denial fixtures to this address")
    ap.add_argument("--only", nargs="*", help="e.g. --only D-03 D-04")
    a = ap.parse_args()
    creds = get_creds()
    if a.drive:
        seed_drive(creds)
    if a.sheet:
        seed_sheet(creds)
    if a.email:
        send_denials(creds, a.email, a.only)
