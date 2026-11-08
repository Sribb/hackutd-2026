"""Realtime hub: every console and phone connects to /ws and gets the same messages.

Ported from draft PR #21 (backend/app/bus.py, itself adapted from CauldronWatch's
WebSocketManager). Each message is one JSON object:

    {"type": "state",    "seq": n, "state": {...}}       same body as GET /api/live/state
    {"type": "alert",    "seq": n, "alert": {...}}
    {"type": "proposal", "seq": n, "proposal": {...}}
    {"type": "ui",       "seq": n, "action": "focus", "asset_id": "PUMP_3", ...}
"""
from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass, field
import time

from fastapi import WebSocket

ROLES = {"web", "phone", "device", "other"}


@dataclass
class Client:
    id: str
    ws: WebSocket
    role: str = "web"
    name: str = ""
    connected_at: float = field(default_factory=time.time)


class Hub:
    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}
        self._ids = itertools.count(1)
        self.seq = 0

    async def connect(self, ws: WebSocket, role: str = "web", name: str = "") -> Client:
        await ws.accept()
        role = role if role in ROLES else "other"
        c = Client(id=f"c{next(self._ids)}", ws=ws, role=role, name=name or role)
        self.clients[c.id] = c
        return c

    def disconnect(self, c: Client) -> None:
        self.clients.pop(c.id, None)

    def message(self, typ: str, payload: dict) -> str:
        self.seq += 1
        return json.dumps({"type": typ, "seq": self.seq, **payload}, default=str)

    async def send(self, c: Client, typ: str, payload: dict) -> None:
        try:
            await c.ws.send_text(self.message(typ, payload))
        except Exception:  # noqa: BLE001 - a dead socket just leaves
            self.clients.pop(c.id, None)

    async def broadcast(self, typ: str, payload: dict) -> None:
        if not self.clients:
            return
        text = self.message(typ, payload)
        targets = list(self.clients.values())
        results = await asyncio.gather(*(c.ws.send_text(text) for c in targets), return_exceptions=True)
        for c, r in zip(targets, results):
            if isinstance(r, Exception):
                self.clients.pop(c.id, None)
