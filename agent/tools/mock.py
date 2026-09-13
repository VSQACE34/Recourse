"""Mocks of the five apps, file-backed by ./fixtures.

Design borrowed from the "stateful replica" idea: each mock holds real state that actions
mutate, supports fault injection (fail the next N calls), and can be snapshotted/reset, so
every eval scenario runs against a clean, identical world.
"""
from __future__ import annotations

import copy
import csv
import io
from pathlib import Path
from typing import Any, Optional

from ..config import FIXTURES
from ..models import LedgerRow
from .base import Message, Toolbox


class Faults:
    """Shared fault injector: faults.arm('mail.send', n) makes the next n calls raise."""
    def __init__(self):
        self.counters: dict[str, int] = {}
        self.calls: list[str] = []

    def arm(self, op: str, n: int = 1) -> None:
        self.counters[op] = n

    def check(self, op: str) -> None:
        self.calls.append(op)
        if self.counters.get(op, 0) > 0:
            self.counters[op] -= 1
            raise ConnectionError(f"injected fault: {op}")


class MockMail:
    def __init__(self, faults: Faults, inbox: Optional[list[Message]] = None):
        self.faults = faults
        self.inbox: list[Message] = inbox or []
        self.processed: set[str] = set()
        self.sent: list[dict[str, Any]] = []
        self._sent_keys: set[str] = set()

    def list_unread(self) -> list[Message]:
        self.faults.check("mail.list")
        return [m for m in self.inbox if m.id not in self.processed]

    def mark_processed(self, msg_id: str) -> None:
        self.processed.add(msg_id)

    def send(self, to, subject, body, idempotency_key):
        self.faults.check("mail.send")
        if idempotency_key in self._sent_keys:          # replica enforces idempotency like a real API with a key would
            return {"id": f"sent-{idempotency_key}", "deduped": True}
        self._sent_keys.add(idempotency_key)
        rec = {"id": f"sent-{len(self.sent)+1}", "to": to, "subject": subject, "body": body, "key": idempotency_key}
        self.sent.append(rec)
        return rec


class MockDocs:
    """Reads come from ./fixtures; writes land in memory so a scenario never mutates the fixtures."""
    def __init__(self, faults: Faults, root: Path = FIXTURES):
        self.faults = faults
        self.root = root
        self.written: dict[str, str] = {}

    def read_text(self, path: str) -> str:
        self.faults.check("docs.read")
        if path in self.written:
            return self.written[path]
        p = self.root / path
        if not p.exists():
            raise FileNotFoundError(path)
        return p.read_text(encoding="utf-8")

    def exists(self, path: str) -> bool:
        return path in self.written or (self.root / path).exists()

    def write_text(self, path: str, content: str, idempotency_key: str = ""):
        self.faults.check("docs.write")
        deduped = path in self.written
        self.written[path] = content
        return {"path": path, "deduped": deduped}


class MockLedger:
    def __init__(self, faults: Faults, csv_path: Path = FIXTURES / "ledger.csv"):
        self.faults = faults
        with csv_path.open(encoding="utf-8", newline="") as f:
            self.rows: dict[str, LedgerRow] = {r["claim_id"]: LedgerRow(**r) for r in csv.DictReader(f)}
        self._snapshot = copy.deepcopy(self.rows)
        self.updates: list[LedgerRow] = []

    def get(self, claim_id: str) -> Optional[LedgerRow]:
        self.faults.check("ledger.get")
        return copy.deepcopy(self.rows.get(claim_id))

    def update(self, row: LedgerRow) -> None:
        self.faults.check("ledger.update")
        self.rows[row.claim_id] = copy.deepcopy(row)
        self.updates.append(row)

    def all(self) -> list[LedgerRow]:
        return list(self.rows.values())

    def reset(self) -> None:
        self.rows = copy.deepcopy(self._snapshot)
        self.updates = []

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(LedgerRow.model_fields))
        w.writeheader()
        for r in self.rows.values():
            w.writerow(r.model_dump())
        return buf.getvalue()


class MockCalendar:
    def __init__(self, faults: Faults):
        self.faults = faults
        self.events: list[dict[str, Any]] = []
        self._keys: set[str] = set()

    def create_event(self, title, day, description, idempotency_key):
        self.faults.check("calendar.create")
        if idempotency_key in self._keys:
            return {"id": f"evt-{idempotency_key}", "deduped": True}
        self._keys.add(idempotency_key)
        ev = {"id": f"evt-{len(self.events)+1}", "title": title, "day": day, "description": description, "key": idempotency_key}
        self.events.append(ev)
        return ev


class MockChat:
    def __init__(self, faults: Faults):
        self.faults = faults
        self.messages: list[dict[str, Any]] = []

    def post(self, channel, text, blocks=None, idempotency_key=""):
        self.faults.check("chat.post")
        m = {"ts": f"{len(self.messages)+1}.000", "channel": channel, "text": text, "blocks": blocks or [], "key": idempotency_key,
             "reactions": set()}
        self.messages.append(m)
        return m

    # --- reaction-based approval fallback (mirrors Slack.reactions / Slack.update) ---
    def react(self, ts: str, name: str) -> None:
        """Test helper: simulate a human adding an emoji reaction."""
        for m in self.messages:
            if m["ts"] == ts:
                m["reactions"].add(name)

    def reactions(self, channel, ts) -> set[str]:
        for m in self.messages:
            if m["ts"] == ts:
                return set(m["reactions"])
        return set()

    def update(self, channel, ts, text):
        for m in self.messages:
            if m["ts"] == ts:
                m["text"], m["blocks"] = text, []
                return m


def mock_toolbox(inbox: Optional[list[Message]] = None) -> tuple[Toolbox, Faults]:
    faults = Faults()
    tb = Toolbox(
        mail=MockMail(faults, inbox),
        docs=MockDocs(faults),
        ledger=MockLedger(faults),
        calendar=MockCalendar(faults),
        chat=MockChat(faults),
        meta={"kind": "mock"},
    )
    return tb, faults


def load_denial_message(name: str) -> Message:
    """fixtures/denials/D-03.txt -> Message"""
    p = FIXTURES / "denials" / name
    text = p.read_text(encoding="utf-8")
    subject = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("Subject:")), name)
    sender = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("From:")), "")
    return Message(id=p.stem, subject=subject, body=text, sender=sender)
