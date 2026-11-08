"""Audit log (CF-10): who proposed, confirmed, rejected, or acknowledged what, and when.

Append-only and in memory, served in order at GET /api/audit. Set FREEBOARD_AUDIT_FILE
to also append each entry as one JSON line to a file (a relative path is under
backend/data/, e.g. FREEBOARD_AUDIT_FILE=audit.jsonl; *.jsonl there is gitignored).

Every entry:
    {"seq": 1, "t": "2026-11-07T07:12:10+00:00", "clock": "01:52", "live_t": 8.0,
     "event": "proposed", "source": "jarvis", "role": "operator", "by": null,
     "asset_id": "VALVE_XO_34", "proposal_id": "act_1", "status": "PENDING", ...}

Other lanes log through app.state.audit.append("approved", source="console",
role=caller.role, plan_id=...). Extra keyword fields are kept as they are.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from .detect import now_iso

log = logging.getLogger("freeboard.audit")
DATA = Path(__file__).resolve().parents[1] / "data"


def _file_from_env() -> Optional[Path]:
    v = os.environ.get("FREEBOARD_AUDIT_FILE", "").strip()
    if not v:
        return None
    p = Path(v)
    return p if p.is_absolute() else DATA / p


class AuditLog:
    def __init__(self, clock: Optional[Callable[[], tuple]] = None, path: Optional[Path] = None) -> None:
        """clock() returns (live clock text, live minutes), e.g. ("01:52", 8.0)."""
        self._clock = clock
        self._entries: list[dict] = []
        self._lock = threading.Lock()   # #64's sync endpoints append from worker threads
        self.path = path if path is not None else _file_from_env()
        self.last_error = ""

    def append(self, event: str, *, source: Optional[str] = None, role: Optional[str] = None,
               by: Optional[str] = None, asset_id: Optional[str] = None, **extra) -> dict:
        """Record one event and return the entry. Never raises for a file problem."""
        clock, live_t = None, None
        if self._clock:
            try:
                clock, live_t = self._clock()
            except Exception:  # noqa: BLE001 - a clock problem must not lose the entry
                pass
        with self._lock:
            entry = {"seq": len(self._entries) + 1, "t": now_iso(), "clock": clock, "live_t": live_t,
                     "event": str(event), "source": source, "role": role, "by": by, "asset_id": asset_id,
                     **extra}
            self._entries.append(entry)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(entry, default=str) + "\n")
                    self.last_error = ""
                except OSError as e:
                    self.last_error = str(e)
                    log.warning("audit file write failed: %s", e)
        return dict(entry)

    def entries(self, since: int = 0) -> list[dict]:
        """Copies, oldest first; `since` skips entries with seq <= since."""
        with self._lock:
            return [dict(e) for e in self._entries[max(0, since):]]

    def __len__(self) -> int:
        return len(self._entries)
