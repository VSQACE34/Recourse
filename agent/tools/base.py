"""Interfaces the agent talks to. Real adapters (Google, Slack) and mocks both implement these.

Keeping the surface tiny is deliberate: the fewer verbs the agent has, the easier it is to
mock them faithfully and to reason about what an action can do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from ..models import LedgerRow


@dataclass
class Message:
    id: str
    subject: str
    body: str
    sender: str = ""
    received: str = ""


class Mail(Protocol):
    """Gmail: incoming denial notices, outgoing appeals / resubmissions."""
    def list_unread(self) -> list[Message]: ...
    def mark_processed(self, msg_id: str) -> None: ...
    def send(self, to: str, subject: str, body: str, idempotency_key: str) -> dict[str, Any]: ...


class Docs(Protocol):
    """Google Drive: clinical notes, claims, payer policies, patient records."""
    def read_text(self, path: str) -> str: ...
    def exists(self, path: str) -> bool: ...
    def write_text(self, path: str, content: str, idempotency_key: str = "") -> dict[str, Any]: ...


class Ledger(Protocol):
    """Google Sheets: the claim ledger the agent reads and updates."""
    def get(self, claim_id: str) -> Optional[LedgerRow]: ...
    def update(self, row: LedgerRow) -> None: ...
    def all(self) -> list[LedgerRow]: ...


class Calendar(Protocol):
    """Google Calendar: appeal deadlines."""
    def create_event(self, title: str, day: str, description: str, idempotency_key: str) -> dict[str, Any]: ...


class Chat(Protocol):
    """Slack: approval gate + escalations."""
    def post(self, channel: str, text: str, blocks: Optional[list[dict]] = None, idempotency_key: str = "") -> dict[str, Any]: ...


@dataclass
class Toolbox:
    mail: Mail
    docs: Docs
    ledger: Ledger
    calendar: Calendar
    chat: Chat
    meta: dict[str, Any] = field(default_factory=dict)
