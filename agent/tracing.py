"""Minimal structured tracing: one JSONL line per step, per run.

Judges care about "show how you know it works". Every LLM call, rule firing and
tool call lands here with timing, so a failing scenario can be read step by step.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from .config import ROOT

TRACE_DIR = ROOT / "traces"
TRACE_DIR.mkdir(exist_ok=True)


class Tracer:
    def __init__(self, run_id: Optional[str] = None, sink: Optional[Path] = None, echo: bool = False):
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.sink = sink or (TRACE_DIR / f"{self.run_id}.jsonl")
        self.echo = echo
        self.events: list[dict[str, Any]] = []

    def log(self, step: str, **data: Any) -> None:
        ev = {"run_id": self.run_id, "ts": time.time(), "step": step, **_jsonable(data)}
        self.events.append(ev)
        with self.sink.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, default=str) + "\n")
        if self.echo:
            print(f"[{self.run_id}] {step}: {json.dumps(_jsonable(data), default=str)[:200]}")

    @contextmanager
    def span(self, step: str, **data: Any):
        t0 = time.time()
        try:
            yield
            self.log(step, status="ok", ms=round((time.time() - t0) * 1000), **data)
        except Exception as e:  # noqa: BLE001
            self.log(step, status="error", error=str(e), ms=round((time.time() - t0) * 1000), **data)
            raise


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        if hasattr(v, "model_dump"):
            out[k] = v.model_dump(mode="json")
        else:
            out[k] = v
    return out


NULL_TRACER = Tracer(run_id="null", sink=Path("/dev/null"))
